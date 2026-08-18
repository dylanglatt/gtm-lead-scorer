# Spec: score any CSV, without ever inventing a number

Target repo: `dylanglatt/gtm-lead-scorer` (public, `main`, deployed to Render free).
Baseline commit for this work: whatever `main` points at when you start. Record the SHA in
`docs/baseline.md` as step one.

Work on a branch (`any-csv`). `main` is public and is the demo URL. It does not break.

---

## 0. The honest framing, which is also the product

Today the tool scores leads whose columns and values match the vocabulary `model.joblib`
was fit on. Anything else degrades: `handle_unknown='ignore'` drops the value, the row
still ranks, confidence falls, and the user gets a clean, tiered, convincing board built
out of almost no signal. It does not crash. It quietly lies.

The goal of this work is **every CSV gets a verdict**. It is emphatically *not* every CSV
gets a model score, because one case is impossible and we should say so in the product:

> A file with an unfamiliar schema and no outcome column cannot be scored by anybody.
> There is nothing to learn from and nothing to transfer. An LLM cannot fix this. Mapping
> "LinkedIn" onto a model that has never seen LinkedIn produces a number, not a prediction.

So the deliverable is a router with four outcomes, three of which produce scores and one
of which produces a diagnosis. The refusal is a feature, not a gap. Build it as one.

---

## 1. The four routes

| Route | Condition | Output |
|---|---|---|
| **N, native** | Columns resolve to the shipped fields and enough values are in the fitted vocabulary | Today's behaviour, unchanged, byte for byte |
| **M, mapped** | Columns resolve only after aliasing or an LLM mapping pass | Model score, confidence capped at Medium, every assumption listed on the page |
| **T, trained** | Schema is foreign but the file carries a usable outcome column | A model fit on *their* data at request time, cross-validated, AUC reported, refused if it cannot beat chance |
| **D, diagnostic** | Foreign schema, no outcome column, or Route T refused | No score at all. A column profile, the specific reason, and exactly what to add to get a score |

The chosen route and its reason are rendered at the top of the results page every time,
including for Route N. A user must never have to guess which engine produced their number.

---

## 2. The central design decision

**Do not generalise `scorer.py`.**

The tempting move is to parameterise `scorer.py` so its globals (`MODEL`, `META`, `BASE`,
`FACTS`, `KNOWN`, `CANON`, `LABELS`, `INTAKE`, `KEY_FIELDS`) become a bundle object passed
per request. Resist it. Those globals encode Hearth-shaped domain knowledge: revenue bands,
US state to time zone, `state_missing` as a trained signal, `legacy_score` domain 0-100.
None of it generalises, and threading a bundle through `normalize_lead`, `_unusable`,
`_prep_dict` and `_why` puts the one path that currently works at risk for the benefit of
a path that will never use those rules anyway.

Generalise the **interface** instead.

- `scorer.py` stays as-is. It is the native engine. Routes N and M both use it.
- `autofit.py` is a second, independent engine. Route T uses it.
- Both emit the **same result dict**. Everything downstream (`app.score_rows`,
  `app.rank_key`, `app._summarize`, `flags._row_flags`, `results.html`, `/export`,
  `/calibrate`) keeps working with no changes, because it only ever sees that dict.

The one-line version for the README, and for anyone reviewing the repo: *I did not
generalise the model, I generalised the contract.* That is the correct engineering answer
and it is also the better story.

### 2.1 The result contract

Freeze it explicitly. Create `contract.py` holding the key set and the type of each value,
and a `validate_result(d)` used in tests (not in the hot path):

```
lead_id: str
score: float | None          # None only when 'error' is present
tier, tier_name, action: str
why: list[{factor, win_rate, delta, arrow}]
normalized: list[str]
confidence: 'High'|'Medium'|'Low'
confidence_note: str
unusable: list[str]
flags: list[str]
warnings: list[str]
row_notes: list[str]         # attached by app.rank(), not the engine
error: str                   # present only on a row that could not be scored
```

`test_both_engines_satisfy_the_result_contract` runs a fixture through each engine and
asserts identical key sets and types. This test is what makes the two-engine design safe.

---

## 3. New modules

Keep the existing convention: each module is importable without Flask, does one thing, and
its docstring explains *why it exists*, not just what it does.

| Module | Responsibility | Must not |
|---|---|---|
| `profile.py` | rows -> one `ColumnProfile` each: original name, normalised name, inferred role (`id`/`categorical`/`numeric`/`boolean`/`datetime`/`text`/`constant`), cardinality, missing rate, up to 5 redacted sample values | touch a model, know about Hearth fields |
| `schema_match.py` | profile -> `NativeMatch`: which of the shipped fields were found, per-field value coverage against `scorer.KNOWN` after aliasing, an overall verdict | fit anything, call an LLM directly |
| `outcome.py` | profile + rows -> `OutcomeColumn \| None`: which column is the label, which value is positive, why | guess when ambiguous; it returns a reason for refusal instead |
| `leakage.py` | features + label -> the set of columns to exclude, each with a human sentence | silently drop anything |
| `autofit.py` | rows + outcome -> a fitted, cross-validated model and result dicts, or a refusal | reuse `model.joblib` |
| `route.py` | profile + match + outcome -> `Route` with `.kind` and `.reasons` | do any work itself |
| `llm_map.py` | optional Claude pass over the profile summary -> proposed column mapping | ever see a full row; ever be required |
| `contract.py` | the result dict shape and its validator | be imported by the hot path |

`explain.py` is a small extraction, not a new idea: pull the row-building half of
`scorer._why` into `explain.why_rows(pairs, facts, base_rate)` so `autofit` can build the
same panel from segment rates computed on the uploaded data. `scorer._why` then calls it.
This is the only edit to `scorer.py` in the whole spec, and it must not change output.

---

## 4. Route decision rules

All thresholds live in one `ROUTING` dict at the top of `route.py`, named, with a comment
per number explaining what it is protecting against. No magic numbers scattered anywhere.

```
NATIVE_MIN_FIELDS      = 3     # of scorer.KEY_FIELDS found as columns
NATIVE_MIN_COVERAGE    = 0.80  # share of non-blank values in the fitted vocabulary,
                               # averaged over the key fields that were found
MAPPED_MIN_COVERAGE    = 0.50  # below this, mapping is wishful thinking
AUTOFIT_MIN_ROWS       = 200
AUTOFIT_MIN_POSITIVES  = 30
AUTOFIT_MIN_AUC        = 0.60  # below: refuse to rank, route to D
AUTOFIT_LOWCONF_AUC    = 0.68  # 0.60-0.68: rank, but cap confidence at Low
```

Decision order, first match wins:

1. File-level parse failure (`csv_io.read_leads` raises) -> unchanged, today's message.
2. `NativeMatch.fields_found >= NATIVE_MIN_FIELDS` and `coverage >= NATIVE_MIN_COVERAGE`
   -> **Route N**.
3. After aliasing / optional LLM mapping, the same test passes at `MAPPED_MIN_COVERAGE`
   -> **Route M**.
4. `outcome.detect()` returns a column and row/positive counts clear the minimums
   -> **Route T**.
5. Otherwise -> **Route D**.

Route T is deliberately *below* Route M in priority. If a file matches the shipped schema
and also has outcomes, use the shipped model. It was fit on more data than the upload has,
and switching engines under a user who uploaded a familiar file would be surprising.

### 4.1 One coverage wrinkle you will hit

`min_frequency=20` in `train_and_save.build_pipeline` means the fitted categories can
include sklearn's pooled `infrequent_sklearn` bucket, and the recent training data now
carries deliberate CRM artefact values that pool into it. When computing coverage in
`schema_match.py`, **exclude `infrequent_sklearn` from the vocabulary**. Counting it as a
known value would let a file of pure garbage report high coverage, because garbage is
exactly what pools there. Write the test that proves this
(`test_a_file_of_junk_does_not_match_natively_via_the_infrequent_bucket`).

---

## 5. Route M, mapped

Two mapping layers, in order, both auditable:

1. **Column mapping.** Extend `csv_io.build_header_map` with a synonym table
   (`schema_match.COLUMN_SYNONYMS`): `lead source`, `source`, `utm_source` -> `channel`;
   `segment`, `tier`, `fit`, `grade` -> `icp_category`; `revenue`, `annual revenue`,
   `company size $` -> `company_annual_revenue`; and so on. Deterministic, hand-written,
   tested. Then, optionally, the LLM pass (section 8) for names the table misses.
2. **Value mapping.** Existing `scorer.CHANNEL_ALIASES` / `REVENUE_ALIASES` already do
   this. Extend the tables; do not add a generic guesser. A value that does not resolve
   stays unresolved and flags, exactly as today.

Route M differences from N, all of which must be visible on the page:

- Confidence is capped at **Medium** for every row, even a row with nothing unusable.
  Add this as a post-processing step in `app.rank()`, not inside `scorer.confidence`,
  so the native confidence rule stays untouched and provably unchanged.
- An **Assumptions** panel above the board listing every column mapping and every value
  alias that was applied, in the form `your column "Lead Source" was read as Source`.
  This panel is the thing that makes Route M defensible rather than sneaky.
- The `/export` CSV gains a `route` column, and the run summary carries the assumption
  count. Keep the export a flat table; do not try to smuggle a preamble into a CSV.

---

## 6. Route T, trained on the upload

This is the substantial build and the best material in the whole project. Order matters.

### 6.1 Outcome detection (`outcome.py`)

Deterministic first, LLM only as a tiebreak.

Candidate columns by normalised name: `won, is_won, win, converted, conversion,
closed_won, outcome, result, status, stage, deal_stage, disposition, label, target, y,
success, closed`.

For each candidate, coerce values case-insensitively and trimmed:

- `{1,0}`, `{true,false}`, `{yes,no}`, `{y,n}`, `{won,lost}`, `{closed won, closed lost}`,
  `{converted, not converted}`, `{success, fail}` -> binary, positive class known.
- Exactly two distinct values that match none of the above -> **ambiguous**. Do not guess.
  Route D renders a "which of these two means won?" selector that re-submits.
- Three to eight distinct values (a pipeline `stage` column) -> **ambiguous**, same
  selector, listing all values. This is a common real export and handling it well is worth
  more than any LLM feature in this spec.
- More than eight distinct values -> not an outcome column.

Reject a detected column if positives < `AUTOFIT_MIN_POSITIVES`, or positive rate is
outside 1%-99%. Return the reason string either way; Route D prints it.

Rows with a blank outcome are **unlabeled**: fit on the labeled rows, score the unlabeled
ones with the full model. This is the realistic case (a CRM export of open plus closed
deals) and it is the one that makes Route T actually useful rather than a party trick.

### 6.2 Leakage guard (`leakage.py`)

**Build this before you build the fit.** A naive fit on a real CRM export returns AUC 0.99
and is worthless, because `close_date`, `amount_won`, `won_reason` and `stage` all record
the outcome. Shipping that would be worse than shipping nothing.

Exclude a column, with a sentence, when any of these hold:

1. **Name looks terminal**: matches `close`, `won`, `lost`, `churn`, `signed`, `contract`,
   `revenue_actual`, `mrr`, `arr`, `commission`, `reason`. Sentence: "excluded, the name
   suggests it is recorded after the outcome is known."
2. **Single-feature AUC >= 0.97** (numeric) or **normalised mutual information >= 0.9**
   (categorical) against the label. Sentence: "excluded, it predicts the outcome almost
   perfectly on its own, which usually means it is a record of the outcome."
3. **Missingness is conditional on the label**: `|missing_rate(y=1) - missing_rate(y=0)|
   >= 0.5`. This is the one that catches `close_date`. Sentence: "excluded, it is filled
   in for one outcome and blank for the other."
4. **Identifier-shaped**: cardinality equals row count, or the name matches an id pattern.
5. **Near-constant**: cardinality 1, or one value covers >= 99% of rows.
6. **High-cardinality text**: cardinality > 200 or > 50% of rows, and role is `text`.
7. **Datetime**: excluded wholesale in v1. There is no recency term here and inventing one
   is a separate piece of work. Sentence says so.

Every exclusion is rendered on the results page under "Columns we did not use, and why."
That list is the single most credible thing this feature can show a reviewer. Do not bury
it behind a toggle.

### 6.3 The fit

Mirror `train_and_save.build_pipeline` so the shape is familiar and `_fit_categories`-style
introspection keeps working:

- Categoricals: `OneHotEncoder(handle_unknown='ignore', min_frequency=20)`.
- Numerics: `StandardScaler`, median imputation, with the same "impute, never pass through
  a broken value" rule `scorer.validate_number` enforces.
- `LogisticRegression(C=0.5, max_iter=2000)`. Same model class as the shipped one. Do not
  reach for gradient boosting; a linear model keeps the explanation honest and the fit
  fast, and fast is a hard requirement (section 9).

Validation: `StratifiedKFold(n_splits=5)` (drop to 3 if positives < 100). Report mean AUC
and standard deviation. Displayed scores for **labeled** rows come from
`cross_val_predict(..., method='predict_proba')`, so the board is out-of-fold and not
flattered by in-sample optimism. Unlabeled rows are scored by the model fit on all labeled
rows. Say which is which on the page.

Refusal, and it must read as confidence rather than failure:

> We fit a model on your 1,240 leads and it does not predict better than chance
> (AUC 0.54, plus or minus 0.03 across 5 folds). Ranking these would be a coin flip wearing
> a queue's clothes, so we have not. Here is what we found in your file instead.

### 6.4 Tiers and the why-panel for Route T

- Cutoffs: percentiles of the uploaded score distribution, same constants as
  `train_and_save.TIER_THRESHOLDS` (p90 / p70 / p35). `/calibrate` continues to work
  unchanged because it operates on absolute cutoffs held in `APPLIED`.
- Why-panel: compute segment win rates on the labeled rows for the top few surviving
  categorical features, exactly as `make_synthetic_data.segment_win_rates` does, and pass
  them to `explain.why_rows`. Same panel, same shape, same honesty about using observed
  rates rather than coefficients. Cite n per segment and suppress any segment with n < 20.
- `confidence` for Route T counts unusable *mapped* features per row using the same rule
  (`scorer.confidence` is reusable as a pure function; import it). Cap at Low when the
  model's AUC is in the `AUTOFIT_LOWCONF_AUC` band.

### 6.5 Lead IDs

Native policy is that a row with no `lead_id` is not scored, because a fabricated id would
rank a phantom among real leads. For a foreign file that is too strict: their id column is
just named something else. So:

- Detect an id column: candidate names (`id`, `lead_id`, `record_id`, `contact_id`,
  `opportunity_id`, `crm_id`, `email`), or any column whose cardinality equals row count
  and whose role is `id`/`text`.
- If none is found, use `row-N` **and** raise one file-level banner ("no ID column found,
  rows are numbered by their position in your file"), not a flag on every row.
- Document this as a deliberate divergence from `app.score_row`'s rule, with the reason,
  in the `autofit.py` docstring. Someone will read that file and ask.

---

## 7. Route D, diagnostic

No score. Not a zero, not a placeholder, no board. The page shows:

1. **Why**, in one sentence, from `Route.reasons`.
2. **What we found**: the column profile as a table (name, inferred type, distinct values,
   % filled, a few examples). This is genuinely useful on its own and costs nothing.
3. **What would get you a score**, as a short branching list: add an outcome column named
   any of `won / converted / closed_won` with two values, *or* rename your columns to
   match the schema (link to the field list), *or* try the demo file.
4. The ambiguous-outcome selector from 6.1 when one applies.

Route D also fires when Route T refuses on AUC or row count, carrying the refusal sentence
into slot 1.

---

## 8. The optional Claude pass (`llm_map.py`)

Everything above works with `ANTHROPIC_API_KEY` unset. CI must run the whole suite with it
explicitly unset, and there must be a test asserting that all four routes are reachable
without it. This is not defensive decoration: the demo is a public URL and strangers must
not be able to spend tokens or take the site down by removing a key.

When the key is present:

**What it does.** One call per upload. Maps *column names only* to shipped fields, and may
propose value aliases for a categorical column already mapped to a native feature.

**What it is sent.** The profile summary and nothing else: for each column, the header
text, inferred role, cardinality, and up to 5 sample values with anything matching an
email, phone or long-digit pattern redacted. Never a full row. Never the whole file.

**What comes back.** Strict JSON, forced via a tool schema:
`{"columns": [{"source": str, "target": str|null, "confidence": float, "why": str}],
"value_aliases": [{"column": str, "from": str, "to": str}]}`.

**Validation, in code, before anything is used.** This is the whole safety story:

- `target` must be a member of the shipped field list, or `null`. Anything else is dropped.
- `source` must be a header that actually exists in the file.
- Each `value_aliases[].to` must be a member of `scorer.KNOWN[column]`, and `from` must be
  a value observed in that column. Both checks, not one.
- Any surviving proposal appears in the Assumptions panel and forces Route M, never N.

**Prompt injection.** A CSV header can say anything. Assume it will say "ignore previous
instructions." The allowlist validator above is the defence, and there must be a test with
a hostile fixture (`tests/fixtures/injection_headers.csv`) asserting the mapping is
unchanged. Do not rely on prompt wording for this.

**Budget guards.** 8-second timeout, one retry, then fall through to deterministic
mapping. Cache the result keyed by a hash of the sorted header tuple, so re-uploading the
same schema costs nothing. A per-process counter caps calls per hour and disables the
feature past the cap with a log line. Model: the cheapest one that does the job; this is
a naming task, not a reasoning task.

---

## 9. Running inside Render free, which is 512MB

This is a hard constraint and it will bite Route T specifically. `--workers 1 --threads 8`
means eight concurrent requests share one process and one memory budget, with pandas,
numpy, sklearn and `model.joblib` already resident.

- **Row cap for autofit**: 20,000 rows. Above that, sample 20,000 stratified on the label
  for the fit, score everything with the resulting model, and say so on the page. Native
  path keeps whatever it does today.
- **Single-flight**: a module-level lock so only one fit runs at a time. A second concurrent
  upload waits, or gets a plain "one model is training right now, try again in a moment."
  Two simultaneous fits will OOM the instance and Render will restart it mid-demo.
- **Wall clock budget**: 20 seconds for the fit including CV. Over it, abort and route to D
  with an honest message. `--timeout 120` in gunicorn is the outer bound; do not get near it.
- **Release everything**: after producing result dicts, drop the DataFrame and the fitted
  pipeline. Store only what `/results/<token>` and `/calibrate` need, which is scores, why
  rows, flags and the run summary. Nothing about the upload is retained beyond the existing
  64-entry `_RESULTS` cache, and that stays the cap.
- **Uploaded data never touches disk.** It does not today. Keep it that way and put one
  line about it in the README and on the upload form. For a public URL where strangers may
  paste a CRM export, that sentence is worth writing.
- Row cap for the native path too: nothing currently bounds row count, only the 10MB body.
  Add one, 100,000, with the same plain-sentence refusal style as the 413 handler.

---

## 10. Invariants that must not break

Put these in `docs/invariants.md` and make each one a test.

1. **`demo_leads.csv` produces byte-identical output.** Capture the current `/export` CSV
   from the baseline commit into `tests/golden/demo_export.csv` *before writing any code*,
   and assert equality on every run. If this test goes red at any point, stop and fix it
   before continuing. This is the single most important line in this document.
2. The existing 21 tests pass, unmodified, throughout. Adding tests is expected; changing
   an existing assertion requires a comment explaining why the old one was wrong.
3. `scorer.py` changes only by the `explain.why_rows` extraction, and that extraction
   changes no output.
4. The typed single-lead path is untouched. It is Route N by definition and never routes.
5. `test_the_training_prep_matches_the_scorers_prep` still holds. `autofit.py` has its own
   prep and is not covered by it; do not try to unify them.
6. Every input row still produces exactly one output row, on every route.
7. No route ever emits a score without also emitting the route and its reason.

---

## 11. Test plan

Beyond the golden test and the contract test:

- **Route table**: one fixture per route, asserting the decision and the reason string.
  Include a file that *nearly* matches natively (2 of 4 key fields) to pin the boundary.
- **Junk does not match natively via the infrequent bucket** (section 4.1).
- **Leakage**: a fixture with a planted `close_date` (blank for losses) and a planted
  `amount_won`. Assert both are excluded and both appear in the exclusion list with their
  sentence.
- **Refusal**: a fixture with a label assigned at random. Assert AUC lands near 0.5, the
  route falls to D, and no board renders. This is the test that proves the tool is honest.
- **Signal**: a fixture with a genuinely predictive column. Assert AUC > 0.7 and a board
  with a real spread, mirroring `test_the_demo_file_ranks_and_spreads`.
- **Unlabeled rows**: fixture with half the outcomes blank. Assert the labeled rows get
  out-of-fold scores and the unlabeled ones get full-model scores, and that the page says
  which.
- **Ambiguous outcome**: a `stage` column with six values. Assert the selector renders and
  a re-submit with a chosen positive value produces a board.
- **No API key**: whole suite green with `ANTHROPIC_API_KEY` unset. Plus a test that
  explicitly monkeypatches it away and walks all four routes.
- **LLM validator**: stubbed client returning (a) a hallucinated target field, (b) a source
  column not in the file, (c) a value alias whose target is not in `KNOWN`, (d) valid JSON
  with an injected instruction in a `why` string. All four must be neutralised.
- **Injection fixture**: hostile headers, mapping unchanged.
- **Memory**: fit a 20,000-row fixture and assert peak RSS delta stays under a stated
  budget. Crude, but it is the difference between a working demo and a restarting one.

---

## 12. Phases, each independently shippable

Do not build this in one pass. Each phase ends with the golden test green, the suite green,
and a commit that could be merged on its own.

**Phase 0: baseline.** Record the SHA. Capture `tests/golden/demo_export.csv`. Write
`docs/invariants.md`. No behaviour change. Commit.

**Phase 1: see before you score.** `profile.py`, `route.py` (returning only N or D),
`contract.py`, the Route D page, the route banner on every results page. Everything that
matches natively today still routes N and is byte-identical. Everything else, which today
gets a fake board, now gets a diagnosis. **This phase alone fixes the honesty problem** and
is worth shipping to the live demo by itself.

**Phase 2: mapping.** `schema_match.py` with the deterministic synonym table, Route M,
the confidence cap, the Assumptions panel. Still no LLM.

**Phase 3: the real feature.** `outcome.py`, `leakage.py`, `autofit.py`, Route T, the
refusal, out-of-fold scoring, the exclusion list, the ambiguous-outcome selector. This is
the bulk of the work and where the interesting decisions live.

**Phase 4: the optional LLM.** `llm_map.py`, the validator, the caches and budget guards,
the hostile fixtures. Ship the feature flag off by default and turn it on deliberately.

**Phase 5: deploy and demo.** Row caps, single-flight lock, wall-clock budget, memory
release. README section explaining the four routes with a screenshot of the refusal, which
is the screenshot a reviewer will remember. Update the live demo.

---

## 13. Demo fixtures to commit

The feature has to be legible in about ninety seconds to someone who will not read the
code. Commit four files next to `demo_leads.csv` and link all of them from the upload page:

| File | Routes to | Shows |
|---|---|---|
| `demo_leads.csv` (exists) | N | The original tool, unchanged |
| `demo_crm_export.csv` | M | Columns named `Lead Source`, `Segment`, `Annual Revenue`; assumptions panel populated |
| `demo_saas_pipeline.csv` | T | A foreign schema with a `closed_won` column, a planted leaky `close_date`, and real signal. The exclusion list and the AUC are the payoff |
| `demo_no_outcomes.csv` | D | The refusal, done well |

Generate them the way `make_synthetic_data.py` generates the training set: deterministic,
seeded, committed, and regenerable. Same standard, so a reviewer can check them.

---

## 14. Out of scope, and say so in the README

- Persistence, accounts, saved models per user. Everything is ephemeral and in-process.
- Any time or recency feature in Route T.
- Non-CSV input.
- Model classes beyond logistic regression in Route T.
- Value-level LLM mapping beyond the tightly validated alias proposals in section 8.
