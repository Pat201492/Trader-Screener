#!/usr/bin/env python3
"""The local store for Free Research pulls and the briefs built from them.

Two record kinds, and one rule about each:

  * a `pull` is what a source returned -- its query parameters, when it ran, and
    where the raw rows landed. The rows themselves are newline-delimited JSON,
    written a line at a time, so a pull killed halfway through still reads back
    every row it had finished.

  * a `brief` is a template filled from ONE pull. It carries the pull's id, and
    reading a brief whose pull is missing raises rather than handing back a
    half-record: a filled slot means nothing without the rows it came from, and
    a caller that cannot see the rows cannot check the slot.

Everything lives under a store root that defaults to `~/.free-research/store`
and is overridable, the way `tools/edgar_scrubber/output_store.py` takes its
root -- tests pass a temporary directory and never touch a real home.

This module writes only under that root. It imports nothing from the shared
pipeline and writes none of its data; the Free Research tool is a reader of the
world and a writer of its own store, and nothing else.
"""

import datetime as dt
import json
import os
import uuid

DEFAULT_HOME = os.path.join("~", ".free-research")
STORE_DIRNAME = "store"
PULLS_DIRNAME = "pulls"
BRIEFS_DIRNAME = "briefs"
ROWS_DIRNAME = "rows"


class StoreError(Exception):
    """A record could not be read or written as asked."""


class PullNotFound(StoreError):
    """A brief named a pull the store does not have."""


def _utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _new_id(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:12])


class Store:
    """Pulls, briefs and raw rows under one root directory."""

    def __init__(self, home=None):
        root = os.path.expanduser(home or DEFAULT_HOME)
        self.home = root
        self.root = os.path.join(root, STORE_DIRNAME)
        self.pulls_dir = os.path.join(self.root, PULLS_DIRNAME)
        self.briefs_dir = os.path.join(self.root, BRIEFS_DIRNAME)
        self.rows_dir = os.path.join(self.root, ROWS_DIRNAME)
        for d in (self.pulls_dir, self.briefs_dir, self.rows_dir):
            os.makedirs(d, exist_ok=True)

    # ---------------------------------------------------------------- paths

    def _pull_path(self, pull_id):
        return os.path.join(self.pulls_dir, "%s.json" % pull_id)

    def _brief_path(self, brief_id):
        return os.path.join(self.briefs_dir, "%s.json" % brief_id)

    def rows_path(self, pull_id):
        """Where a pull's raw rows live. Inside the store root, always."""
        return os.path.join(self.rows_dir, "%s.jsonl" % pull_id)

    def _write_json(self, path, payload):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)

    def _read_json(self, path, what):
        if not os.path.exists(path):
            raise StoreError("no such %s: %s" % (what, os.path.basename(path)))
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    # ---------------------------------------------------------------- pulls

    def create_pull(self, source, query, as_of=None, pull_id=None):
        """Record a pull. Returns the stored record, rows path included."""
        if not source:
            raise StoreError("a pull needs a source id")
        if not isinstance(query, dict):
            raise StoreError("a pull's query parameters must be a dict")
        pull_id = pull_id or _new_id("pull")
        record = {
            "id": pull_id,
            "kind": "pull",
            "source": source,
            "query": query,
            "as_of": as_of or _utcnow(),
            "rows_path": self.rows_path(pull_id),
        }
        self._write_json(self._pull_path(pull_id), record)
        return record

    def read_pull(self, pull_id):
        return self._read_json(self._pull_path(pull_id), "pull")

    def list_pulls(self):
        """Every pull, newest as-of first."""
        out = []
        for name in sorted(os.listdir(self.pulls_dir)):
            if name.endswith(".json"):
                out.append(self._read_json(os.path.join(self.pulls_dir, name), "pull"))
        return sorted(out, key=lambda r: r.get("as_of", ""), reverse=True)

    def has_pull(self, pull_id):
        return os.path.exists(self._pull_path(pull_id))

    # ----------------------------------------------------------------- rows

    def write_rows(self, pull_id, rows):
        """Append rows as newline-delimited JSON. Returns how many were written.

        One line per row, flushed per row: a run that dies partway leaves a file
        whose complete lines are all still readable.
        """
        path = self.rows_path(pull_id)
        written = 0
        with open(path, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
                fh.flush()
                written += 1
        return written

    def read_rows(self, pull_id):
        """Every COMPLETE line of a pull's rows.

        A file truncated mid-line -- a killed run, a full disk -- yields every
        row before the break and drops the partial one, rather than raising and
        making the whole pull unreadable over one bad tail.
        """
        path = self.rows_path(pull_id)
        if not os.path.exists(path):
            return []
        rows = []
        with open(path, encoding="utf-8", newline="") as fh:
            for line in fh:
                if not line.endswith("\n"):
                    break  # a partial final line: nothing after it is trustworthy
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    break
        return rows

    # --------------------------------------------------------------- briefs

    def create_brief(self, pull_id, template_id, slots, model, brief_id=None,
                     dropped=None, as_of=None):
        """Record a filled brief against an existing pull."""
        if not self.has_pull(pull_id):
            raise PullNotFound("brief names unknown pull %r" % pull_id)
        if not template_id:
            raise StoreError("a brief needs a template id")
        if not isinstance(slots, dict):
            raise StoreError("a brief's slot values must be a dict")
        if not model:
            raise StoreError("a brief needs the model id that filled it")
        brief_id = brief_id or _new_id("brief")
        record = {
            "id": brief_id,
            "kind": "brief",
            "pull_id": pull_id,
            "template_id": template_id,
            "slots": slots,
            "model": model,
            "dropped": dropped or [],
            "as_of": as_of or _utcnow(),
        }
        self._write_json(self._brief_path(brief_id), record)
        return record

    def read_brief(self, brief_id):
        """A brief, or an error. Never a brief whose pull has gone missing."""
        record = self._read_json(self._brief_path(brief_id), "brief")
        pull_id = record.get("pull_id")
        if not self.has_pull(pull_id):
            raise PullNotFound(
                "brief %s names pull %r, which is not in this store"
                % (brief_id, pull_id))
        return record

    def list_briefs(self):
        """Every brief, newest as-of first.

        Reads the records directly: a brief whose pull is missing still appears
        here, so it can be seen and cleaned up. `read_brief` is the call that
        refuses it.
        """
        out = []
        for name in sorted(os.listdir(self.briefs_dir)):
            if name.endswith(".json"):
                out.append(self._read_json(os.path.join(self.briefs_dir, name), "brief"))
        return sorted(out, key=lambda r: r.get("as_of", ""), reverse=True)
