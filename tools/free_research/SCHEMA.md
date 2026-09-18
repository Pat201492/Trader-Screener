# Free Research store — record shapes and layout

Two record kinds and one rows file per pull, all under a single store root.

## Layout

```
<home>/store/
  pulls/<pull id>.json      one pull record
  briefs/<brief id>.json    one brief record
  rows/<pull id>.jsonl      that pull's raw rows, newline-delimited JSON
```

`<home>` defaults to `~/.free-research` and is overridden with
`Store(home=...)`. Tests pass a temporary directory; nothing in this package
writes outside the root it was given.

## `pull`

What a source returned, and the query that asked for it.

| field | type | meaning |
|---|---|---|
| `id` | string | `pull_<12 hex>`, the record's name and its rows file's stem |
| `kind` | string | always `"pull"` |
| `source` | string | source id, e.g. `edgar` |
| `query` | object | the query parameters as passed, verbatim |
| `as_of` | string | UTC ISO-8601, second resolution, when the pull ran |
| `rows_path` | string | absolute path to the rows file, always inside the store root |

`source` must be non-empty and `query` must be an object, or `create_pull`
raises `StoreError`.

## `brief`

A template filled from exactly one pull.

| field | type | meaning |
|---|---|---|
| `id` | string | `brief_<12 hex>` |
| `kind` | string | always `"brief"` |
| `pull_id` | string | the pull this was built from |
| `template_id` | string | which template was filled |
| `slots` | object | slot name to filled value |
| `model` | string | the model id that filled the slots |
| `dropped` | array | slots dropped during filling, with the reason |
| `as_of` | string | UTC ISO-8601, when the brief was built |

`template_id`, a dict `slots` and `model` are all required.

### A brief never outlives its pull

`create_brief` raises `PullNotFound` if the pull does not exist, and
`read_brief` raises it if the pull has since gone. A filled slot means nothing
without the rows it was drawn from: a caller that cannot reach the rows cannot
check the slot, so a half-record is worse than an error.

`list_briefs` is the exception — it reads records directly, so an orphaned brief
still shows up and can be found and removed.

## Rows

One JSON object per line, written and flushed a line at a time.

Reading stops at the first line with no terminating newline and returns
everything before it. A pull killed halfway through therefore still yields every
row it finished writing, rather than failing the whole file over a partial tail.

## Why this store is local

Per `ARCHITECTURE.md`, an exploratory tool writes only to its own datastore and
is never a second writer of shared pipeline data. This package holds to that:
`store.py` writes under its root and nothing else, and imports nothing from the
pipeline. When a Free Research field proves out, it graduates upstream through
the pipeline's own ingest — it does not start being written from here.
