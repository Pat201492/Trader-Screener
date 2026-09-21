#!/usr/bin/env python3
"""Fill a brief template's slots from a pull's rows, using a local model.

The harness carries the correctness burden so a small local model can do the
work. Three things make that true:

  * the model sees ONE slot at a time, and only the columns that slot declares.
    It is never handed the row set and asked to be sensible about it;
  * every answer comes back schema-constrained, so a malformed one is the API's
    problem rather than something parsed hopefully here;
  * every answer is then checked against the rows it was given, and anything
    that cannot be traced back to them is dropped and reported.

That last rule is the one worth stating plainly. A prose slot naming a number
the rows do not contain is dropped whole -- not trimmed, not patched. This is
`claude_template.py`'s rule applied to prose: a value nothing can check is not
evidence, and a paragraph is not improved by deleting the digits out of the
middle of it. What is left is a brief whose every claim is checkable against the
rows sitting next to it in the store.

Local only. The same client class will happily talk to a billed endpoint when
pointed at one, so this module refuses a non-local base URL rather than letting
a "free research" run quietly cost money.
"""

import json
import re
import urllib.parse

from tools.edgar_scrubber.ollama_client import OllamaClient, OllamaConfig
from tools.free_research import template as template_mod

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal")

# A number as a model writes one: 1,234  12.5  -3  2025
NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

SCHEMAS = {
    "prose": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    "table": {
        "type": "object",
        "properties": {"columns": {"type": "array", "items": {"type": "string"}}},
        "required": ["columns"],
        "additionalProperties": False,
    },
    "chart": {
        "type": "object",
        "properties": {"x": {"type": "string"}, "y": {"type": "string"}},
        "required": ["x", "y"],
        "additionalProperties": False,
    },
}


class FillError(Exception):
    """The brief could not be filled as asked."""


class NotLocalError(FillError):
    """The configured model endpoint is not local, so this would cost money."""


def assert_local(base_url):
    """Refuse anything but a local endpoint.

    Free Research is free in the sense of costing nothing per run. The same
    client class talks to Anthropic when pointed at it, so the check belongs
    here, at the point of use, rather than in a comment asking people not to.
    """
    host = (urllib.parse.urlparse(base_url).hostname or "").lower()
    if host not in LOCAL_HOSTS:
        raise NotLocalError(
            "model endpoint %r is not local (host %r). Free Research runs on a "
            "local model; pointing it at a billed endpoint is not free research."
            % (base_url, host))
    return base_url


def local_client(config=None):
    cfg = config or OllamaConfig.from_env()
    assert_local(cfg.base_url)
    return OllamaClient(cfg)


# ------------------------------------------------------------------- rows

def project(rows, columns):
    """Each row cut down to the slot's declared columns, order preserved."""
    out = []
    for row in rows:
        out.append({c: row.get(c) for c in columns if c in row})
    return out


def present_columns(rows):
    seen = set()
    for row in rows:
        seen.update(row)
    return seen


def _norm_number(text):
    return text.replace(",", "").lstrip("+")


def _haystack(projected):
    """Every value in the projected rows, as one string to look numbers up in."""
    parts = []
    for row in projected:
        for value in row.values():
            parts.append(_norm_number(str(value)))
    return " ".join(parts)


def ungrounded_numbers(text, projected):
    """Numbers in `text` that appear nowhere in the rows the slot was given.

    Deliberately permissive: a number is grounded if it appears anywhere in the
    row values, including inside a longer string such as a date. The cost of a
    false drop -- deleting a true sentence -- is higher than the cost of letting
    a coincidental match through, and the rows sit beside the brief either way.
    """
    hay = _haystack(projected)
    bad = []
    for match in NUMBER_RE.findall(text or ""):
        norm = _norm_number(match)
        if norm and norm not in hay:
            bad.append(match)
    return bad


# ------------------------------------------------------------------ model

def _prompt(slot, projected):
    ask = slot.get("prompt") or "Fill this slot."
    return [
        {"role": "system",
         "content": "You fill one slot of a research brief. Use only the rows "
                    "given. Never state a number that is not in them."},
        {"role": "user",
         "content": "%s\n\nColumns you may read: %s\n\nRows:\n%s"
                    % (ask, ", ".join(slot["columns"]),
                       json.dumps(projected, indent=1, sort_keys=True))},
    ]


def _ask(client, slot, projected, model):
    messages = _prompt(slot, projected)
    resp = client.chat_completion(
        messages=messages,
        model=model,
        response_format={"type": "json_schema",
                         "json_schema": {"name": slot["kind"],
                                         "schema": SCHEMAS[slot["kind"]]}},
    )
    content = resp["choices"][0]["message"]["content"]
    if isinstance(content, str):
        try:
            return json.loads(content)
        except ValueError as exc:
            raise FillError("model returned non-JSON for slot %r: %s"
                            % (slot["name"], exc))
    return content


# ------------------------------------------------------------------ slots

def _check_prose(slot, answer, projected):
    text = (answer or {}).get("text", "")
    if not text.strip():
        return None, "model returned no text"
    words = len(text.split())
    if words > slot["max_words"]:
        return None, ("%d words, over the slot's maximum of %d"
                      % (words, slot["max_words"]))
    bad = ungrounded_numbers(text, projected)
    if bad:
        return None, ("states %s, which %s nowhere in the rows this slot was given"
                      % (", ".join(bad), "appear" if len(bad) > 1 else "appears"))
    return text, None


def _check_table(slot, answer, projected, available):
    chosen = [c for c in (answer or {}).get("columns", []) if isinstance(c, str)]
    if not chosen:
        return None, "model chose no columns"
    missing = [c for c in chosen if c not in available]
    if missing:
        return None, ("names %s, absent from the pull's rows"
                      % ", ".join(sorted(set(missing))))
    return {"columns": chosen}, None


def _check_chart(slot, answer, projected, available):
    answer = answer or {}
    x, y = answer.get("x"), answer.get("y")
    if not isinstance(x, str) or not isinstance(y, str):
        return None, "model did not name both axes"
    missing = [c for c in (x, y) if c not in available]
    if missing:
        return None, ("names %s, absent from the pull's rows"
                      % ", ".join(sorted(set(missing))))
    return {"mark": slot.get("mark", "bar"), "x": x, "y": y}, None


# ------------------------------------------------------------------- fill

def fill(store, client, pull_id, template, model=None):
    """Fill every slot of `template` from `pull_id`'s rows. Returns the brief.

    Slots that cannot be grounded are left out of `slots` and listed in
    `dropped`, each with the reason -- a brief that quietly omitted them would
    be a brief you could not tell was incomplete.
    """
    if not store.has_pull(pull_id):
        raise FillError("no such pull: %r" % pull_id)
    template_mod.validate(template)

    rows = store.read_rows(pull_id)
    available = present_columns(rows)
    model_id = model or getattr(client, "config", None) and client.config.model

    slots, dropped = {}, []
    for slot in template_mod.slots(template):
        projected = project(rows, slot["columns"])
        answer = _ask(client, slot, projected, model)

        if slot["kind"] == "prose":
            value, why = _check_prose(slot, answer, projected)
        elif slot["kind"] == "table":
            value, why = _check_table(slot, answer, projected, available)
        else:
            value, why = _check_chart(slot, answer, projected, available)

        if why:
            dropped.append({"slot": slot["name"], "kind": slot["kind"], "reason": why})
        else:
            slots[slot["name"]] = value

    return store.create_brief(
        pull_id=pull_id,
        template_id=template["id"],
        slots=slots,
        model=model_id or "unknown",
        dropped=dropped,
    )
