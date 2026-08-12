# GTM Lead Scorer

A small web tool that scores an inbound sales lead from its intake data and tells a rep
**who to work, how hard, and why**. Paste one lead, or upload a CSV of leads to rank a whole list.

## Background

Built to solve a real go-to-market problem: scoring and triaging inbound leads for a B2B
sales team so reps know who to work first. The original dataset is confidential, so this
public version runs entirely on synthetic data generated to match its exact format. The
model, the tool, and the approach are the real ones; only the underlying data is a stand-in.

## Run it

```
pip install -r requirements.txt
python3 app.py
# open http://localhost:5000
```

The fitted model ships in the repo (`model.joblib`), so it runs offline on a fresh clone.
If port 5000 is taken (macOS AirPlay uses it), run `PORT=8000 python3 app.py`.

## What it does

- **Score** — probability of winning, from an intake-only model (nothing that happens after
  a lead arrives is used, so it is a score a rep could actually have at intake time).
- **Tier** — Hot / Warm / Cool / Cold, each with one action (call until reached, call once
  then sequence, sequence only, inbound only). A manager can drag the cutoffs.
- **Why** — the real historical win rate for each of the lead's traits, so a rep trusts it.
- **Confidence** — High / Medium / Low, with a flag for every missing or unrecognized field.
- **Messy input is safe** — bad rows are flagged and ranked low, never crash the tool or sneak
  to the top. Try `leads_messy_fixture.csv` (broken ~19 different ways) under **List → Rank**.

## Files

- `app.py` — the Flask web app
- `scorer.py` — scoring logic (score, tier, why, confidence) and the tier vocabulary
- `csv_io.py` / `flags.py` / `format.py` — CSV ingest, bad-value flagging, formatting
- `templates/` — the UI
- `model.joblib`, `meta.json` — the fitted model and its metadata
