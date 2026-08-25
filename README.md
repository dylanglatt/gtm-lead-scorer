# GTM Lead Scorer

A lead-scoring and routing system that turns raw inbound-lead data into a ranked, explainable
action queue for sales reps: **who to work, how hard, and why**. Score one lead, or upload a
CSV to prioritize a whole list.

![The rep's ranked lead queue](docs/board.png)

*Upload a list and every lead is scored, tiered, and ordered by win-propensity, each with a
specific next action. Messy rows are flagged and still ranked, never dropped.*

## Background

Built to solve a real go-to-market problem: scoring and triaging inbound leads for a B2B
sales team so reps know who to work first. The original dataset is confidential, so this
public version runs entirely on synthetic data generated to match its exact format — see
[How the data was made](#how-the-data-was-made) for the generator and the numbers behind it.
The model, the tool, and the approach are the real ones; only the underlying data is a
stand-in.

## Impact

The system is built to move revenue by fixing how rep time gets allocated. Instead of leads
getting attention in roughly the order they arrive, reps work a queue ordered by
win-propensity, so the highest-probability leads are called first and the lowest get a lighter
touch or inbound-only. Concretely, it:

- ranks every inbound lead and assigns a tier and a specific next action, so a rep opens the
  list and knows exactly who to call, how hard, and why;
- lets a manager set the tier cutoffs to match team capacity, turning "who do we have time
  for" into a deliberate, adjustable decision rather than an accident of volume;
- stays reliable on the messy CRM exports reps actually paste in — bad rows are flagged and
  still ranked, never dropped or silently mis-scored — so the queue stays trustworthy.

This was built as a case study on a real GTM problem and validated on synthetic data (see
Background), so it is not a production deployment with live usage or revenue figures. The
contribution is the working system and the decisions behind it: what to predict, how to make
the score explainable enough that a rep will actually act on it, and how to keep it robust on
real-world input.

## Run it

**Live demo:** https://gtm-lead-scorer.onrender.com (click **Score the sample file** to see a ranked queue with nothing to download)

```
pip install -r requirements.txt
python3 app.py
# open http://localhost:5000
```

The fitted model ships in the repo (`model.joblib`), so it runs offline on a fresh clone.
If port 5000 is taken (macOS AirPlay uses it), run `PORT=8000 python3 app.py`. The form loads
pre-filled with an example lead, so you can click **Score** immediately or edit it first.

Then click **Score the sample file** under **List** to see the ranked queue.
`leads_messy_fixture.csv` is the other one worth uploading — see
[What it does](#what-it-does).

### Deploy it

The app serves under gunicorn, which is what every host below actually runs:

```
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120
```

- **Render** — New → Blueprint → point it at this repo. `render.yaml` sets the build and
  start commands, pins the Python version, and points the health check at `/healthz`.
- **Any Procfile host** (Heroku, Railway, Fly) — the `Procfile` carries the same command.
- **Docker** — `docker build -t lead-scorer . && docker run -p 8000:8000 lead-scorer`.

`python3 app.py` stays bound to `127.0.0.1`, because that path is the Flask development
server and should not be listening to a network. Set `HOST=0.0.0.0` to override it inside a
container. Uploads are capped at 10MB.

## Tests

```
pip install -r requirements-dev.txt
pytest
```

Covers the parity between the typed and uploaded paths (the same lead has to score the same
either way — it did not, once), ingestion of the messy fixture, what an unrecognized value is
allowed to change, that junk sinks instead of ranking, and that a file the model cannot read
is refused rather than ranked. `test_export_golden.py` holds the exported bytes for
`demo_leads.csv` fixed, so a change to anything else cannot quietly move a score.
None of it needs Salesforce credentials — `test_salesforce.py` monkeypatches the one function
that talks to the network, so `pytest` runs clean on a fresh clone with no env vars set.

## What it does

- **Score** — probability of winning, from an intake-only model (nothing that happens after
  a lead arrives is used, so it is a score a rep could actually have at intake time).
- **Tier** — Hot / Warm / Cool / Cold, each with one action (call until reached, call once
  then sequence, sequence only, inbound only). A manager can drag the cutoffs.
- **Why** — the real historical win rate for each of the lead's traits, so a rep trusts it.
- **Confidence** — High / Medium / Low, with a flag for every missing or unrecognized field.
- **Messy input is safe** — bad rows are flagged and ranked low, never crash the tool or sneak
  to the top.
- **It says when it can't** — the model only knows the vocabulary it was fit on. If too few of
  a file's values are ones it has seen, the page says so above the board; if almost none are,
  it declines to rank the file at all and names the columns it read, the ones it ignored, and
  the ones it scores on. A confident-looking queue built out of values the model never saw is
  worse than no queue.

The edges of that check, and three other things worth knowing before you trust this, are
written down in [docs/known-issues.md](docs/known-issues.md).

![A file the model can only half read](docs/mismatch.png)

*The failure that prompted this: an export identical to the training schema except that its
revenue column is named `contractor_annual_revenue`. That column is ignored, so every lead is
scored one field short. It still ranks — but the page says how much of the file it could read,
which field it could not, and what to rename.*

Two files ship for trying it under **List → Rank**:

| file | what it is |
| --- | --- |
| `demo_leads.csv` | 250 clean leads. **Start here** — it is what the screenshot above shows. |
| `leads_messy_fixture.csv` | the robustness test: 19 leads, each broken a different way (mis-cased headers, an unclosed quote, a duplicate ID, a missing ID, ragged rows, junk numbers). Every row still comes back, flagged and ranked. |

![One lead, scored and explained](docs/verdict.png)

*Scoring a single lead: the win-propensity, the tier and next action, a confidence level, and a
plain-English "why" built from each trait's historical close rate.*

## Salesforce integration

A fourth path alongside the typed lead, the CSV upload, and the JSON API: **Pull from
Salesforce**, next to the other two buttons under **List**, live-pulls Lead records from a
connected org and ranks them through the identical scoring path everything else on this page
uses — same `score_row`, same tiers, same "never guess" refusal rules. It is not a mocked
integration; it is a real OAuth 2.0 Client Credentials exchange against a real Salesforce
org, on every click.

- **Auth** — a Salesforce External Client App with the Client Credentials Flow enabled,
  configured to run as a specific user. Three env vars, same three the app already checks
  before showing the button: `SF_LOGIN_URL`, `SF_CONSUMER_KEY`, `SF_CONSUMER_SECRET`. None
  set is a fully supported state — the button just doesn't render (`sf_configured` in
  `_ctx`) and nothing else about the tool changes.
- **Field mapping reuses the same seam `/api/score` does** — `CRM_ALIASES` extends the CSV
  header map with Salesforce and HubSpot's own field names (`LeadSource`, `AnnualRevenue`,
  `Rating`, `hs_analytics_source`, `hubspotscore`, …), so a Lead record maps onto the scorer's
  fields the same way a CSV column does. A value the model was never trained on — Salesforce's
  own `LeadSource='Web'`, `Rating='Hot'` — is flagged and excluded, not guessed.
- **Provenance is on the page, not just in the response** — the ranked board (and a decline,
  if the pull comes back too thin to rank) shows the exact org this hit and links to
  `/salesforce/leads`, the raw JSON view, so a skeptical reader doesn't have to take the app's
  word for it. Real Lead Ids (`00Q…`) and Salesforce's own field shapes are the actual
  evidence.
- **An honest ceiling, not a bug** — a standard Salesforce Lead has no native field for two of
  the four things this model scores on ("Marketing channel" and "Prior score" are concepts
  this schema invented, not ones Salesforce ships). Every Salesforce-sourced lead is missing
  at least those two, which caps confidence below High regardless of how clean the rest of the
  record is. A real deployment would map those onto custom fields; a demo org does not have
  any, and the tool says so rather than pretending otherwise.
- **A rate limit protects the connected org, not the tool** — `/rank/salesforce` and
  `/salesforce/leads` are intentionally unauthenticated, same as every other route here, but
  unlike the rest they place a real call against a real external API on every hit. A sliding
  window caps both routes (they share one budget) at `SF_MAX_CALLS_PER_HOUR` pulls, so a
  page that gets shared around can't quietly burn through a Developer Edition org's daily API
  limit.
- **Seeding a demo org** — `scripts/seed_salesforce_leads.py` creates a handful of realistic
  mock Leads (mixed vocabulary, revenue bands that land inside real scoring boundaries);
  `scripts/reset_salesforce_leads.py` wipes every Lead in the org first, since a fresh
  Developer Edition org ships with its own unrelated demo Leads that would otherwise dilute
  the mock data below the ranking threshold. Both use the same three env vars and Client
  Credentials Flow as the app itself.

Covered by `test_salesforce.py`: the field mapping, the provenance note on both a ranked and
a declined board, the button's visibility, the failure paths (bad auth, an empty org), and the
rate limit — including that a refused pull never reaches Salesforce at all.

## How the data was made

The original dataset is confidential, so the public version runs on synthetic data built by
`make_synthetic_data.py`. Everything about that world is an invented parameter table at the
top of that file — level mixes, effect sizes, blank rates — and the generator is seeded, so
the same command reproduces the same rows byte for byte:

```
python3 make_synthetic_data.py   # writes data/leads_train.csv (9,000 rows) and demo_leads.csv
python3 train_and_save.py        # fits model.joblib, writes meta.json, prints the tier split
```

Two things it does deliberately, because they are the difference between a plausible dataset
and a useless one:

- **The features covary.** Each lead draws a latent quality score, and channel, fit, revenue
  and prior score are drawn from it. Independent draws would let the best possible
  combination stack every bonus and predict a 90% win chance on an inbound lead, which no
  sales team would believe. As built, the model tops out in the low 70s.
- **The relationships overlap.** Paid search beats paid social, revenue is monotone, a
  missing state is genuinely bad — but every one of them is noisy enough that the segments
  interleave, so the model has something real to learn rather than a lookup table.

The training set is committed (`data/leads_train.csv`) so the claim is checkable rather than
asserted. `make_synthetic_data.py` also fails loudly if the regenerated close rates land
within three points of the figures they replaced.

## Files

- `app.py` — the Flask web app
- `scorer.py` — scoring logic (score, tier, why, confidence) and the tier vocabulary
- `csv_io.py` / `flags.py` / `format.py` — CSV ingest, bad-value flagging, formatting
- `templates/` — the UI
- `model.joblib`, `meta.json` — the fitted model and its metadata
- `make_synthetic_data.py` / `train_and_save.py` — the data generator and the training run
- `test_scorer.py` — the test suite
- `test_schema_match.py` / `test_schema_guard.py` — the file-level match check and what it does
- `test_salesforce.py` — the Salesforce pull: field mapping, provenance note, rate limit
- `scripts/shoot_screenshots.py` — regenerates the images above
- `scripts/make_test_fixtures.py` — regenerates the CSV fixtures the tests read
- `scripts/seed_salesforce_leads.py` / `scripts/reset_salesforce_leads.py` — populate or wipe
  Leads in a connected Salesforce org, for trying **Pull from Salesforce** against real data
- `Procfile` / `render.yaml` / `Dockerfile` — the three ways to deploy it
