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
public version runs entirely on synthetic data generated to match its exact format. The
model, the tool, and the approach are the real ones; only the underlying data is a stand-in.

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

```
pip install -r requirements.txt
python3 app.py
# open http://localhost:5000
```

The fitted model ships in the repo (`model.joblib`), so it runs offline on a fresh clone.
If port 5000 is taken (macOS AirPlay uses it), run `PORT=8000 python3 app.py`. The form loads
pre-filled with an example lead, so you can click **Score** immediately or edit it first.

## What it does

- **Score** — probability of winning, from an intake-only model (nothing that happens after
  a lead arrives is used, so it is a score a rep could actually have at intake time).
- **Tier** — Hot / Warm / Cool / Cold, each with one action (call until reached, call once
  then sequence, sequence only, inbound only). A manager can drag the cutoffs.
- **Why** — the real historical win rate for each of the lead's traits, so a rep trusts it.
- **Confidence** — High / Medium / Low, with a flag for every missing or unrecognized field.
- **Messy input is safe** — bad rows are flagged and ranked low, never crash the tool or sneak
  to the top. Try `leads_messy_fixture.csv` (broken ~19 different ways) under **List → Rank**.

![One lead, scored and explained](docs/verdict.png)

*Scoring a single lead: the win-propensity, the tier and next action, a confidence level, and a
plain-English "why" built from each trait's historical close rate.*

## Files

- `app.py` — the Flask web app
- `scorer.py` — scoring logic (score, tier, why, confidence) and the tier vocabulary
- `csv_io.py` / `flags.py` / `format.py` — CSV ingest, bad-value flagging, formatting
- `templates/` — the UI
- `model.joblib`, `meta.json` — the fitted model and its metadata
