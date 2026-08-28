# AGENTS.md

Working context for anyone changing this code, human or agent. The README describes the
product; this file is the engineering contract. Read it before your first edit.

## The rule everything else serves

**Never guess.**

A value the model was not fit on is flagged and excluded, not mapped onto the nearest
familiar thing. A file whose columns the model largely cannot read is refused outright
rather than ranked. A row with no `lead_id` is kept and reported, never dropped and never
given a fabricated one. Duplicate ids are both kept and both flagged.

The failure this exists to prevent is not a crash. It is a clean, tiered, confident board
built out of almost no signal. That is worse than an error, because nobody investigates
it. The renamed-column case in the README is exactly this: one column silently unread,
every row scored one field short, and a board that looked fine.

Any change that makes the tool more willing to produce a number it cannot defend is wrong,
however much it improves the demo.

## The paths in, and where they converge

Four intakes. They differ only in how raw fields are collected. From there it is one
function.

| Path | Route | Entry |
| --- | --- | --- |
| One typed lead | `GET /score` | `scorer.score_lead` |
| A CSV upload | `POST /rank` | `read_leads` then `score_rows` |
| Another system's JSON | `POST /api/score` | `_api_map_lead` then `score_row` / `score_rows` |
| A live Salesforce pull | `POST /rank/salesforce` | `_sf_query_leads` then the same `_rank_leads` |

Each of the four names itself to run history as it goes past `_summarize`. `GET /health`
reads that history back and is not an intake: loading it scores nothing and logs nothing.

`scorer.score_lead` is the only place a lead is judged. There is no second scoring path
and there must not be one: an earlier version had a `from_form` flag for a single feature,
and the same lead scored 38% Warm typed and 3.4% Cold uploaded. Held by
`test_the_same_lead_scores_the_same_typed_and_uploaded`.

### The seams worth knowing

- **`HEADER_MAP` / `CRM_ALIASES`** (`app.py`) is the vocabulary seam. `HEADER_MAP` is built
  from `FIELDS` and accepts any casing or punctuation of a column name. `CRM_ALIASES` adds
  Salesforce and HubSpot's own field names on top. Extending this per org is the intended
  use. Mapping an unfamiliar *value* through it is not.
- **`_summarize`** (`app.py`) is where a scoring run becomes a fact about the pipeline.
  Every intake that produces a summary passes through it, so it is also the single hook
  for run history: it takes an `origin` (`upload` / `sample` / `api` / `salesforce`) and
  writes one row. A new intake gets logged by existing, not by remembering to log.
  `origin=None` records nothing, which is what off-request scoring and the tests get.
- **`score_row` / `score_rows`** (`app.py`) is the batch entry. It owns two rules the
  single-lead form does not have: a row with no id never reaches the model, and anything
  the model throws is caught per row so one bad row cannot take a file down.
- **`APPLIED` cutoffs** (`app.py`) are the team-wide tier boundaries. Only Manager
  Calibration writes them. Moving a cutoff re-tiers a stored board; it never re-scores it.
- **`scorer.TIERS`** is the single source for tier names, actions and colour keys. Nothing
  else, template included, may restate them.

### Modules

- `scorer.py` decides everything: normalize, score, tier, confidence, why. Owns field
  values for every path.
- `app.py` is routing and presentation. It decides how things look, never what they say.
- `csv_io.py` is bytes to row dicts. Raises `ValueError` for exactly three file-level
  problems (empty, header-only, no recognizable column) and for nothing else. Every other
  mess is a note on the row, and the row still ships.
- `flags.py` classifies a problem as a missing field or a bad value.
- `format.py` reshapes results for templates. Decides nothing.
- `runs.py` is the run-history store: hash a header set, append a row, read rows back.
  Decides nothing about what a change means — that lives in `app.py` beside `MATCH_NOTICE`.
- `model.joblib` / `meta.json` are the fitted model and its metadata, built by
  `make_synthetic_data.py` then `train_and_save.py`.

## What must stay true

Six test files. The ones below encode a decision rather than a behaviour, so a failure
here means the change is wrong, not that the test is stale.

| Invariant | Held by |
| --- | --- |
| The same lead scores the same typed and uploaded | `test_the_same_lead_scores_the_same_typed_and_uploaded` |
| One lead and a batch of one are the same call | `test_one_lead_and_a_batch_of_one_are_the_same_call` |
| The demo export is byte identical to the golden file | `test_the_demo_export_is_byte_identical_to_the_golden_file` |
| An unrecognized value costs confidence, never the score | `test_an_unrecognized_value_costs_confidence_but_not_the_score` |
| Salesforce's own vocabulary is flagged, not guessed | `test_salesforce_native_vocabulary_is_flagged_not_guessed` |
| Every input row produces exactly one output row | `test_every_messy_row_produces_exactly_one_result` |
| A file the model cannot read is refused, not ranked | `test_a_file_the_model_cannot_read_is_not_ranked` |
| A refused file still accounts for every row it read | `test_a_refused_file_still_accounts_for_every_row_it_read` |
| A moved cutoff re-tiers without re-scoring | `test_a_moved_cutoff_retiers_without_rescoring` |
| No template hardcodes a tier name or action | `test_no_template_hardcodes_a_tier_name_or_action` |
| A reordered or recased header is the same schema | `test_the_fingerprint_is_stable_across_column_reorder_and_case` |
| A renamed or dropped column is not | `test_the_fingerprint_moves_when_a_column_is_renamed_or_dropped` |
| A failed history write leaves the scoring run intact | `test_a_failed_write_leaves_the_scoring_run_intact` |
| No store configured ranks exactly as a store does | `test_no_store_configured_ranks_normally_and_health_says_so` |

The golden export test is the tripwire. It pins the exported bytes for `demo_leads.csv`,
so a change anywhere else cannot quietly move a score. If it fails and you believe the new
output is correct, say so explicitly and regenerate it in its own commit. Do not fold a
golden update into a feature commit.

## Conventions

**Optional integrations degrade to absent, never to an error.** Salesforce is configured by
three env vars. None set is a fully supported state: the button does not render
(`sf_configured`) and nothing else changes. Run history takes the same posture from one
var, `RUN_HISTORY_DB`: unset means no store, no writes, and `/health` saying so plainly.
Any integration added later follows this.

**Bookkeeping is expendable; the run is not.** Nothing observational may fail a scoring
run. `runs.record` swallows every exception it can produce and `_record_run` catches on
top of it, because the contract is "the rep still gets their board", not "the store
handles its own errors". Whatever gets watched next inherits this.

**An absent store and a broken one are different answers.** `runs.recent()` returns `None`
for either, and `/health` separates them: not configured is a supported way to run this
app, unreadable is a fault. Collapsing the two would let an unmounted disk read as a quiet
week, which is the exact silent-but-fine failure the page exists to remove.

**Anything that calls an external system gets a rate limit.** Every route here is
unauthenticated on purpose, but `/rank/salesforce` and `/salesforce/leads` place a real
call against a real org on every hit. `SF_MAX_CALLS_PER_HOUR` caps them on a shared sliding
window, checked before the call so it bounds usage rather than describing it afterwards.

**Errors are sentences.** A bad file gets a plain-English explanation, never a stack trace.
`csv.field_size_limit` is raised and `csv.Error` is caught because an unclosed quote is a
real export, not an edge case.

**Limits get written down, not hidden.** `docs/known-issues.md` holds four of them, each
with a runnable reproduction. A stated limit is worth more than a surprise. Add to it in
the same commit that creates the limit.

**`docs/spec-any-csv.md` is a design document and is deliberately not built.** Do not
implement it as if it were a backlog.

**Storage is one disk, and that is a decision, not a default.** Run history is SQLite on a
Render persistent disk (`render.yaml`), which pins the service to one instance. That was
already true — `--workers 1` says so for `_RESULTS`' sake — so the disk costs nothing that
was not already spent. If history ever has to outlive a single instance it moves to a
managed Postgres and the disk block goes away; it does not get scaled.

**Pins are load-bearing.** `model.joblib` is a pickle, so `scikit-learn` is pinned exactly
and `PYTHON_VERSION` is pinned alongside it. The gunicorn command appears in `Procfile`,
`render.yaml` and the `Dockerfile`; if one changes the others change with it. `--workers 1`
is not a resource decision: `_RESULTS` is per-process state holding ranked boards between
the POST and the redirected GET. Raise `--threads`, never `--workers`, unless `_RESULTS`
moves out of process memory first.

## Running it

```
pip install -r requirements.txt && python3 app.py        # http://localhost:5000
pip install -r requirements-dev.txt && pytest            # full suite, no credentials needed
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120
RUN_HISTORY_DB=./runs.db python3 app.py                  # ...with /health keeping history
```

`pytest` runs clean on a fresh clone with no env vars set. `test_salesforce.py`
monkeypatches the one function that touches the network; `test_run_history.py` writes a
real SQLite file to a temp path, because the thing most likely to be wrong there is the
SQL and a stub would pass either way.

## Ask before you build

This repo is small enough to change quickly and opinionated enough that quick changes go
wrong. Stop and ask rather than choosing for me:

- adding a dependency, or changing a pinned version
- anything that alters a score, a tier boundary, or the contents of the golden export
- weakening a refusal, a flag, or a rate limit to make a demo smoother
- adding a second scoring path, or a branch inside the scorer that depends on which intake
  a lead arrived from
- storage decisions: where state lives, and what happens to it on redeploy
- creating or renaming fields in a connected Salesforce org, which is state that is not
  easy to undo

An incomplete spec is a question, not a gap to fill in with a reasonable assumption.

## Working rules

- Update this file in the same commit as any change to the architecture, the invariants or
  the conventions above. Never as a follow-up pass.
- New limitations go in `docs/known-issues.md` with a reproduction, in the same commit that
  introduces them.
- Prose here explains why, not what. The code says what it does; comments and docs exist
  for the decisions that are not recoverable from reading it.
- No `Co-Authored-By`, `Claude-Session`, or "Generated with" trailers on commits.
