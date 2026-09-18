#!/usr/bin/env python3
"""The brief template: a fixed skeleton with named slots a model fills.

The point of the template is that the model never decides the document's shape.
It is handed one slot at a time, told which columns of the pull it may read, and
asked for that slot's value and nothing else. A 7B model can do that; the same
model asked to "write a brief about these filings" cannot, reliably.

Three slot kinds:

  prose   free text, bounded by a stated maximum word count
  table   named columns of the pull's rows
  chart   a mark type plus the row columns for its x and y

Every slot declares the columns it may read, and a chart names columns only --
never a literal number. That rule is the whole reason a chart here can be
trusted: the model chooses which column to plot, and the renderer reads the
values out of the rows. A model that can write a number into a chart can write
a number that is not in the data, and then the picture is fiction.

Validation is strict and says what it rejected. A template is read once and
filled many times; a rule that fires at fill time, per slot, is a rule that
fires in front of a user.
"""

import json
import numbers
import os

SLOT_KINDS = ("prose", "table", "chart")
TEMPLATES_DIRNAME = "templates"


class TemplateError(Exception):
    """A template is not shaped the way the filler requires."""


def templates_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), TEMPLATES_DIRNAME)


def load(path):
    """Load and validate a template file. Returns the template dict."""
    with open(path, encoding="utf-8") as fh:
        try:
            raw = json.load(fh)
        except ValueError as exc:
            raise TemplateError("%s is not valid JSON: %s" % (path, exc))
    return validate(raw)


def load_by_id(template_id, directory=None):
    path = os.path.join(directory or templates_dir(), "%s.json" % template_id)
    if not os.path.exists(path):
        raise TemplateError("no template %r in %s" % (template_id, directory or templates_dir()))
    tpl = load(path)
    if tpl["id"] != template_id:
        raise TemplateError(
            "template in %s.json declares id %r" % (template_id, tpl["id"]))
    return tpl


def list_templates(directory=None):
    """Every template in the directory that validates, by id.

    A file that does not validate is left out rather than raising: one broken
    template should not hide the working ones from a caller listing them.
    """
    d = directory or templates_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            out.append(load(os.path.join(d, name)))
        except TemplateError:
            continue
    return out


def slots(template):
    """The template's slots, in document order."""
    return list(template["slots"])


def validate(raw):
    """Check a template and return it, or raise TemplateError saying why."""
    if not isinstance(raw, dict):
        raise TemplateError("a template must be an object")
    for key in ("id", "slots"):
        if not raw.get(key):
            raise TemplateError("a template needs a non-empty %r" % key)
    if not isinstance(raw["slots"], list):
        raise TemplateError("a template's 'slots' must be a list, in document order")

    seen = set()
    for i, slot in enumerate(raw["slots"]):
        where = "slot %d" % i
        if not isinstance(slot, dict):
            raise TemplateError("%s must be an object" % where)

        name = slot.get("name")
        if not name:
            raise TemplateError("%s needs a name" % where)
        where = "slot %r" % name
        if name in seen:
            raise TemplateError("%s appears twice; slot names address values" % where)
        seen.add(name)

        kind = slot.get("kind")
        if kind not in SLOT_KINDS:
            raise TemplateError(
                "%s has unknown kind %r; the kinds are %s"
                % (where, kind, ", ".join(SLOT_KINDS)))

        columns = slot.get("columns")
        if not isinstance(columns, list) or not columns:
            raise TemplateError(
                "%s declares no readable columns; every slot names the columns "
                "it may read" % where)
        if not all(isinstance(c, str) and c for c in columns):
            raise TemplateError("%s has a column that is not a name" % where)

        if kind == "prose":
            _validate_prose(slot, where)
        elif kind == "chart":
            _validate_chart(slot, where, columns)

    return raw


def _validate_prose(slot, where):
    max_words = slot.get("max_words")
    if max_words is None:
        raise TemplateError(
            "%s is prose with no max_words; an unbounded prose slot is how a "
            "brief turns into an essay" % where)
    if not isinstance(max_words, int) or isinstance(max_words, bool) or max_words < 1:
        raise TemplateError("%s has max_words %r; it must be a positive integer"
                            % (where, max_words))


def _validate_chart(slot, where, columns):
    if not slot.get("mark"):
        raise TemplateError("%s is a chart with no mark type" % where)
    for axis in ("x", "y"):
        value = slot.get(axis)
        if value is None:
            raise TemplateError("%s names no %s column" % (where, axis))
        if isinstance(value, bool) or isinstance(value, numbers.Number):
            raise TemplateError(
                "%s has a literal value %r on %s; a chart slot names a column, "
                "never a number -- a number here is one the data need not "
                "contain" % (where, value, axis))
        if not isinstance(value, str):
            raise TemplateError("%s has a non-name %r on %s" % (where, value, axis))
        if value not in columns:
            raise TemplateError(
                "%s plots %r on %s but does not declare it readable; declared: %s"
                % (where, value, axis, ", ".join(columns)))
