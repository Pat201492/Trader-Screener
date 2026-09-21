# Brief templates — slot kinds and validation rules

A template is a fixed document skeleton with named slots. The filler hands a
model one slot at a time, tells it which columns of the pull it may read, and
asks for that slot's value and nothing else.

This is the point of the format. A small local model asked to "write a brief
about these filings" produces something shaped differently every run and
impossible to check. The same model asked "name the column that shows how these
fall over time, from this list of three" answers well. The template carries the
document's structure so the model does not have to.

## Shape

```json
{
  "id": "filing_brief",
  "title": "Filing brief",
  "slots": [ { "name": "...", "kind": "...", "columns": ["..."] } ]
}
```

`slots` is an array, and `load()` returns it in that order — the array is the
document order.

## Slot kinds

### `prose`

Free text, bounded.

| field | required | meaning |
|---|---|---|
| `columns` | yes | the row columns this slot may read |
| `max_words` | yes | positive integer; the value is rejected if it runs longer |
| `prompt` | no | what to ask the model for this slot |

### `table`

Named columns of the pull's rows.

| field | required | meaning |
|---|---|---|
| `columns` | yes | the columns the model may choose among |
| `prompt` | no | what to ask the model for this slot |

### `chart`

A mark type and the columns for its axes.

| field | required | meaning |
|---|---|---|
| `columns` | yes | the columns the model may plot |
| `mark` | yes | mark type, e.g. `bar`, `line` |
| `x`, `y` | yes | column **names**, each one declared in `columns` |

**A chart slot names columns, never numbers.** A literal number on `x` or `y` is
rejected. This is the rule that makes a chart here checkable: the model picks
which column to plot, and the renderer reads the values out of the rows. A model
that can write a number into a chart can write one the data does not contain,
and then the picture is fiction.

## Every rejection rule

The validator raises `TemplateError` naming what it rejected.

| rule | rejected when |
|---|---|
| template is an object | the top level is not an object |
| `id` present | `id` missing or empty |
| `slots` present | `slots` missing or empty |
| `slots` is a list | `slots` is not a list |
| slot is an object | a slot is not an object |
| slot has a name | `name` missing or empty |
| names are unique | two slots share a `name` |
| kind is known | `kind` is not `prose`, `table` or `chart` — the message names the bad kind |
| columns declared | `columns` missing, empty, or not a list |
| columns are names | a column entry is not a non-empty string |
| prose is bounded | a `prose` slot has no `max_words` |
| bound is sane | `max_words` is not a positive integer (`True` and floats are rejected) |
| chart has a mark | a `chart` slot has no `mark` |
| chart has axes | `x` or `y` missing |
| **chart names a column** | `x` or `y` is a number or boolean rather than a column name |
| chart axes are names | `x` or `y` is neither a number nor a string |
| chart plots what it declared | `x` or `y` names a column absent from `columns` |

## Listing

`list_templates()` returns every template in the directory that validates. A
file that does not validate is skipped rather than raising, so one broken
template cannot hide the working ones from a caller listing them. `load_by_id()`
is strict by contrast, and also rejects a file whose declared `id` disagrees with
its filename.

## Shipped

`templates/filing_brief.json` — one `prose`, one `table` and one `chart` slot
over a filings pull.
