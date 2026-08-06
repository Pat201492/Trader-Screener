# Human-in-loop validation loop (issue #105)

Part of #95, built on #101 (reduction + span offsets), #102 (field spec), and
#104 (extraction ladder). This is the **kicker** in the original request — the
reason a local model beats a hosted one here. The tool pops the first N
documents, you validate by hand, and the extractor learns your field
definitions before it runs unattended.

It is also the **highest-leverage** issue in the chain: its output feeds three
separate consumers — exemplars (#106), hand-seeded regex rules (#107), and the
held-out eval set (#108). Ten validated minutes here removes hours of GPU time
downstream.

```
tools/edgar_scrubber/
  validation.py       # core: proposals, verdicts, spans, exemplars, rule seeds, resumable session
  validate_ui.py      # the two-pane keyboard-first terminal UI (+ offline --demo)
  test_validation.py  # every #105 acceptance criterion
```

## The screen

Two panes. **Left**: the rendered document, scrolled to the field's section,
with the **candidate span highlighted**. **Right**: the proposed value with
accept / correct / reject.

Span highlighting is the whole ergonomic argument. A proposed `barrier_pct` of
70% tells you nothing about whether it came from *Key Terms* or from a
hypothetical-example table three pages later. The highlight tells you instantly
— the difference between 30 seconds and 5 minutes per document, which across 20
documents is the difference between a workable loop and one you abandon.

```
EDGAR validation  session=jpm-2025  doc [######------] 6/20  field 20/29
--------------------------------------------------------------------------------
DOCUMENT  (section: estimated_value)          | FIELD  barrier_pct
--------------------------------------------- | (percent_of_initial)
                                              | proposed: 97.23
Barrier: 70.00% of the Initial Value.         | from: model:qwen2.5-7b  conf 0.41
                                              | span: source[492..498]
Estimated Value of the Notes                  | ! warn: barrier_pct=97.23 above range
                                              |
Our estimated value of the notes is $[972.30] | [a]ccept   [c]orrect   [r]eject
per $1,000 stated principal amount.           | [A]ccept-all  [n]ext  [p]rev  [N]ext doc
--------------------------------------------------------------------------------
[a]ccept  [c]orrect  [r]eject  [A]ll  [n]ext  [N]ext-doc  [q]uit  [?]help
```

Here the model grabbed `972.30` (the estimated value) for `barrier_pct`. The
highlight lands on `$[972.30]` in the *Estimated Value* section — the classic
silent wrong answer, obvious at a glance instead of invisible in a bare number.

## Three verdicts, all informative

| Key | Verdict | What it teaches | Exemplar written (#106) |
|-----|---------|-----------------|--------------------------|
| `a` | **Accept** — value and span both correct | a positive example and a rule seed | `positive` |
| `c` | **Correct** — wrong value, or right value from the wrong span; you mark the true span | the boundary the model got wrong, which a plain accept cannot — **the most valuable labels in the system** | `corrected` |
| `r` | **Reject** — field genuinely absent | that absence is a valid answer; suppresses the value that would otherwise be hallucinated | `negative` |

`c` prompts for the true value and, optionally, the text the value sits in;
`locate_span` resolves that snippet to **exact source offsets** (see below).

## Flow

- Pull the next unvalidated document from #100's crawl (`ValidationSession.next_unvalidated`).
- Extract **all** spec fields and show them together — one document, all fields.
  Field-at-a-time across documents means re-reading the same filing once per field.
- Keyboard-first: accept-all (`A`), next field (`n`/`p`), next document (`N`).
  Repetitive batch work; a mouse-driven UI makes 20 documents feel like 100.
- Progress against the target N is in the header.
- **Every verdict writes to #106 immediately**, so document 11 already benefits
  from your work on 1–10. Batching the learning until session end throws away
  the compounding that makes this design work.

## Corrected spans resolve through all four #101 stages

The extraction ladder returns a span in the coordinates of the reduced text it
was handed. `build_field_context` gives that text an `OffsetMap` composed
straight back to the original bytes — through the **table pre-parse** (stage 1),
**boilerplate strip** (stage 2), and **section split** (stage 3) — so
`LadderExtractor` lifts the model's offset pair to an original-source span. A
hand-marked correction resolves through the same map, and the stored offset
round-trips back into a **sub-block** window (stage 4, the warm path #107 uses).

A value marked in a boilerplate-stripped, section-split, sub-blocked fragment
therefore still slices the *original* document bytes exactly. That exactness is
the contract three downstream issues depend on.

## Rule seeding is a by-product, not extra work

#107 is regex-first, and its seeds come from here. After ~10 validated documents
for an issuer, the accepted spans already show where each field's anchor sits.
Once a field's anchor is **stable** across validated documents
(`ValidationSession.rule_seed_for`: the most common anchor is shared by
≥ `rule_seed_threshold` docs *and* is a majority), the UI offers "promote this
to a rule?" — once. Answered either way, it does not appear again.

## The exemplar store is the ladder's rung-3 provider

`ValidationStore` is callable: `store(issuer, field)` returns the rendered
exemplar lines, so it plugs straight into
`ExtractionLadder(spec, exemplars=store, ...)` — the same `exemplars=` hook the
ladder already accepts (#104). Corrected exemplars rank ahead of plain
positives; a reject contributes a negative. There is no separate wiring step:
writing a verdict *is* teaching the model.

> The store is the #105 → #106 handoff. It implements exactly the write path
> #105 needs to close its loop and the callable read path the ladder consumes.
> #106 owns the fuller exemplar system; it extends this store, it does not fork it.

## Resumability

Every verdict and every completed-document mark is committed the instant it is
made, to a local SQLite file under the scrubber's own home (the same
ownership boundary the output store enforces — never a pipeline volume). A
killed session resumes from the last field ruled on: no completed work redone,
no partial document lost. A re-verdict of the same field overwrites in place.

## Run it

Offline walkthrough — one canned 424B2, no network, no model:

```bash
python tools/edgar_scrubber/validate_ui.py --demo
```

Live — validate the first N documents of a crawl:

```bash
python tools/edgar_scrubber/validate_ui.py \
    --user-agent "Your Name you@example.com" \
    --query tools/edgar_scrubber/queries/424b2-jpm-2025-pilot.json \
    --n 20 --session jpm-2025
```

`--local-only` never escalates to Claude (#104 local-only mode); `--no-color`
disables ANSI highlighting for a dumb terminal or a log.

## Test

```bash
python tools/edgar_scrubber/test_validation.py
```

Stdlib only, no network, no model (fake clients script the ladder, an in-memory
store stands in). Exit 0 = pass. Checks every #105 acceptance criterion:

- a 424B2 validated end-to-end — **every** spec field gets a verdict;
- corrected spans persist with **exact offsets through all four #101 stages**;
- **documents 11+ measurably improve over 1–10** on the same issuer — the same
  document goes from gated/wrong to clean/right purely from the exemplars the
  loop wrote (the test that proves the loop closes);
- a rule-seed prompt appears once a field's anchor is stable, and only once;
- the session is resumable — a partial document is never lost across a close.

## References

- Issue #105 (this module) — part of #95, built on #101 / #102 / #104
- [DOCUMENT_REDUCTION.md](DOCUMENT_REDUCTION.md) — the four-stage reduction and the `OffsetMap` a corrected span resolves through
- [FIELD_SPEC.md](FIELD_SPEC.md) — the fields validated and their anchors
- [EXTRACTION_LADDER.md](EXTRACTION_LADDER.md) — the ladder whose `exemplars=` hook the store feeds
- Issue #106 (exemplars) / #107 (regex rule seeds) / #108 (held-out eval) — the three consumers of this loop's output
