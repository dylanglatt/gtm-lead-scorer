# Known issues

Things that are wrong, or not yet right, in the file-level match check, in the run
history that watches it, and around both.
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

## 4. The row cap is tested as a branch, never at its real size

`MAX_ROWS = 100_000` in `app.py` refuses an oversized file before scoring it, and
`test_a_file_with_too_many_rows_is_refused_before_it_is_scored` covers it — by patching the
cap down to 2 and uploading `demo_leads.csv`. That proves the branch fires, that the
comparison is the right way round, and that the message and the sample-file button render.

It proves nothing about 100,000 rows, because no file that size has ever been through this.
What is untested is whether an upload just under the cap parses, scores, sorts, stores and
renders inside the memory and the request time a 512MB instance running one worker and
eight threads actually has. The number was chosen as one that sounded safe, not as one
measured against the box.

To find out, build one and watch it:

```
python3 - <<'PY'
import csv, itertools, os
src = list(csv.DictReader(open('demo_leads.csv', newline='', encoding='utf-8')))
with open('/tmp/big.csv', 'w', newline='', encoding='utf-8') as fh:
    w = csv.DictWriter(fh, fieldnames=src[0].keys())
    w.writeheader()
    for i, row in enumerate(itertools.islice(itertools.cycle(src), 99_000)):
        w.writerow(dict(row, lead_id=f'B-{i:06d}'))
print(os.path.getsize('/tmp/big.csv') / 1e6, 'MB')
PY
```

Then upload `/tmp/big.csv` and watch the resident memory of the server process.

That file comes out at 7.0MB, which clears `MAX_CONTENT_LENGTH` — the 10MB body limit —
with room to spare. A wider export would not: the same 99,000 rows with a dozen extra
columns hits the size limit long before the row cap, and the two bounds have never been
set against each other. Whichever one a real upload meets first is currently an accident
of how many columns the CRM exports.

## 5. One upload source is really every uploader at once

Run history groups by intake path, and `upload` is one path however many different people
and systems use it. `/health` compares each run with the previous run of the same source,
so two teams alternately uploading two legitimately different exports produce a schema
change flag on every single run — each one correct in isolation, and the sequence
meaningless.

Two files, each perfectly good, uploaded in turn:

```
python3 - <<'PY'
import io, os, tempfile
os.environ['RUN_HISTORY_DB'] = os.path.join(tempfile.mkdtemp(), 'runs.db')
import app
from test_run_history import csv_bytes, COLUMNS
c = app.app.test_client()
for cols in [COLUMNS, COLUMNS + ['created_at'], COLUMNS, COLUMNS + ['created_at']]:
    c.post('/rank', data={'csv': (io.BytesIO(csv_bytes(cols)), 'l.csv')},
           content_type='multipart/form-data')
for r in app._health()['sources'][0]['rows']:
    print(r['fingerprint'], r['pct'], r['flags'])
PY
```

Every run but the oldest is flagged, and nothing is wrong with any of them.

The fix is to key history on something narrower than the route — the fingerprint itself is
the obvious candidate, so a source becomes (path, schema) and each schema keeps its own
history. That is a real design decision about what a "source" is and it wants a second
opinion, not a quick patch: it also decides what happens the first time a legitimate
schema change arrives, which under that model starts a new history rather than flagging
the old one.

Until then this is accurate for the case it was built for — one team, one export, watched
over months — and noisy for the case it was not.

## 6. A run is recorded only when something scored

`_summarize` is the hook, so a file that never reached the model leaves no trace: an
unreadable CSV, an oversized upload, a Salesforce pull that failed to authenticate. Those
are refusals rather than runs, and none of them says anything about coverage or schema,
which is the argument for leaving them out.

It is also the argument against. A source whose exports have started arriving corrupt
shows up on `/health` as silence — the same silence as nobody uploading anything — and
silence is the failure mode this whole page exists to remove.

```
python3 - <<'PY'
import io, os, tempfile
os.environ['RUN_HISTORY_DB'] = os.path.join(tempfile.mkdtemp(), 'runs.db')
import app
c = app.app.test_client()
c.post('/rank', data={'csv': (io.BytesIO(b'a,b\n1,2\n'), 'junk.csv')},
       content_type='multipart/form-data')
print(app._health()['sources'])
PY
```

```
[]
```

Fixing it means a second row shape — a run with no coverage and no fingerprint — and a
page that can render both without the empty columns reading as zeroes. Worth doing; not
worth guessing at the shape of.

## 7. The clobber check sees the record, not the field

Writeback skips a Lead when `LastModifiedById` is somebody other than the integration user
and `LastModifiedDate` is newer than our own `LeadScorer_Scored_At__c`. That is the whole
signal Salesforce offers without Field History Tracking: **the record changed and who
changed it**, never which field.

So a rep correcting a typo in an address marks the record as manually overridden, and a
lead whose score has genuinely moved since then is never updated again:

```
python3 - <<'PY'
import app
from test_salesforce import already_written, INTEGRATION_USER, HUMAN_USER
rec = already_written(1)
rec['AnnualRevenue'] = 15_000_000                    # genuinely a different score now
rec['LastModifiedById'] = HUMAN_USER                 # a rep fixed the address
rec['LastModifiedDate'] = '2026-08-27T09:00:00.000+0000'
row = app.score_row(app._api_map_lead(rec), app.APPLIED['cut'])
plan = app._sf_row_plan(row, rec, INTEGRATION_USER)
print(plan['action'], '|', plan['note'])
print('stored', rec['LeadScorer_Score__c'], '-> computed', round(row['score'], 4))
PY
```

```
overridden | edited in Salesforce since this tool last scored it, so it is left alone
stored 0.5473 -> computed 0.5434
```

Checking `match` before `overridden` (see `_sf_row_plan`) already removes the common case:
a record a human touched whose computed values are unchanged reads as "already matches",
not as an override. What is left is the case above, where a real re-score is due and an
unrelated edit blocks it, and there is currently no way to clear the flag except editing
the record as the integration user.

The direction of the error is deliberate — a lead this tool declines to update is visible
on the page and costs somebody a click, and a lead it overwrites is somebody's work gone
with nothing left to notice. But it is a real staleness bug, not a clean trade.

Fixing it means Field History Tracking on the six owned fields and a second query per
batch, or storing a checksum of what this tool last wrote and comparing against that
instead of against a timestamp. The second is cheaper and needs a ninth custom field,
which is a schema decision rather than a code one.

## 8. The model version records the model, not the cutoffs

`LeadScorer_Model_Version__c` is a content hash of `model.joblib`, so it answers "were
these two scores produced by the same model". It does not answer "were they tiered the
same way". Tier boundaries live in `APPLIED` and a manager moves them in Manager ·
Calibration, so two writes carrying the identical model version and the identical score
can carry different tiers:

```
python3 - <<'PY'
import app, scorer
row = app.score_row({'lead_id': 'X-1', 'channel': 'google', 'icp_category': 'High Value',
                     'company_annual_revenue': '$1,000,000 to $4,999,999',
                     'utm_medium': 'brand', 'legacy_score': 70}, scorer.DEFAULT_CUTOFFS)
for cuts in (scorer.DEFAULT_CUTOFFS, {'hot': 0.99, 'warm': 0.98, 'cool': 0.97}):
    v = app._sf_verdict(app._retier(row, cuts))
    print(v['LeadScorer_Model_Version__c'], v['LeadScorer_Score__c'], v['LeadScorer_Tier__c'])
PY
```

```
model-53dbe1a9f81e 0.2064 Cool
model-53dbe1a9f81e 0.2064 Cold
```

That is correct behaviour — a moved cutoff re-tiers without re-scoring, which is an
invariant this repo holds elsewhere on purpose — and it is still a thing that will confuse
somebody reading two Lead records six months apart. The cutoffs in force are visible in
Manager · Calibration and nowhere in the CRM.

Writing them would mean a ninth field or folding them into the version string, and folding
them in would make the version string stop meaning "which model", which is the one
question it currently answers cleanly.
