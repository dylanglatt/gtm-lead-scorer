# Known issues

Things that are wrong, or not yet right, in the file-level match check and around it.
Written down rather than fixed because each needs a decision or real data, and a limit
stated plainly is worth more than a surprise.

Every item has a reproduction. Run them from the repo root, with the requirements
installed.

## 1. The notice threshold is a guess against two files

`MATCH_NOTICE = 0.80` was set against the only files available: `demo_leads.csv` (0.98),
`leads_messy_fixture.csv` (0.89), and one reconstructed export (0.75). Nothing real was
measured between 0.75 and 0.89, so the line sits in a gap rather than at a boundary
anybody has evidence for.

A file sitting exactly at 0.80 gets no notice at all — and that can be a file where 80% of
its rows are missing one of the four fields the model scores on. That is a lot of missing
data to say nothing about.

```
python3 -c "import app; print(app.match_band(0.80), app.match_band(0.7999))"
```

```
ok notice
```

Needs calibration against real uploads. Until then the number is a defensible guess and
not a measurement.

## 2. The coverage denominator counts only scored rows

Coverage is `usable cells / (scored rows x key fields)`. A row that could not be scored at
all — no `lead_id`, or a scoring failure — is not in the denominator, so it cannot pull
coverage down.

A file of 5 clean leads and 95 rows with no id therefore measures **1.00** and reads as a
perfect match:

```
python3 - <<'PY'
import app
GOOD = 'ID-{},google,High Value,"$1,000,000 to $4,999,999",brand'
BLANK = ' ,google,High Value,"$1,000,000 to $4,999,999",brand'
rows = [GOOD.format(i) for i in range(5)] + [BLANK] * 95
raw = ('lead_id,channel,icp_category,company_annual_revenue,utm_medium\n'
       + '\n'.join(rows)).encode()
s = app._rank_bytes(raw, app.APPLIED['cut'])['summary']
print(s['coverage'], s['band'], s['rows_in'], s['scored'], s['failed'])
PY
```

```
1.0 ok 100 5 95
```

The page is not silent about those rows — the run summary counts them, and a refused file
lists them with their reasons — but the *match verdict* ignores them.

The fix is to count a failed row as zero usable cells, making the denominator
`rows_in x key fields`. It is not free:

- it re-baselines `leads_messy_fixture.csv` from **0.8889** to **0.8421** (64 usable cells
  over 76 rather than over 72), still above the notice line but with less room;
- it merges two different problems into one number. A file whose schema is unreadable and
  a file whose rows are unidentifiable would then produce the same coverage and the same
  message, and those need opposite advice — rename your columns, versus fill in your ids.
  The refusal copy has to tell them apart before the metric stops doing so.

## 3. Equal scores are ordered alphabetically, under a heading that says otherwise

`rank_key` breaks ties by score, then confidence, then `lead_id`. The last step makes the
order reproducible rather than arbitrary, which is deliberate. The heading above it says
"work the top first".

On a file with little score dispersion, that heading presents an alphabetical list as a
ranking, and nothing on the page says so. Three identical leads:

```
python3 - <<'PY'
import app
rows = ['%s,google,High Value,"$1,000,000 to $4,999,999",brand' % i
        for i in ['Z-1', 'A-1', 'M-1']]
raw = ('lead_id,channel,icp_category,company_annual_revenue,utm_medium\n'
       + '\n'.join(rows)).encode()
print([(r['lead_id'], r['score']) for r in app._rank_bytes(raw, app.APPLIED['cut'])['queue']])
PY
```

```
[('A-1', 0.1333), ('M-1', 0.1333), ('Z-1', 0.1333)]
```

Same score, alphabetical order, presented as a queue.

## 4. The row cap has no test

`MAX_ROWS = 100_000` in `app.py` refuses an oversized file before scoring it. Every other
refusal path in the tool has a test; this one does not. The honest test builds a
100,000-row file, and the cheap one patches the constant down and proves less than it
appears to.

It has been exercised by hand and not by CI, so it can regress silently.
