"""Writes the CSV fixtures the schema-match tests read.

Generated rather than hand-typed, and committed rather than built at test time, for the
same reason data/leads_train.csv is: a fixture a reviewer can open and check is worth more
than one that appears by magic, and a seeded generator means the file a test failed on is
the file you get back.

    python3 scripts/make_test_fixtures.py

The interesting one is hearth_repro.csv. It reproduces a real export that broke the tool
quietly: the schema is right in every respect except that the revenue column is called
contractor_annual_revenue, which the header map does not accept. Every row then scores
with one field missing, the board comes back fully tiered and confident, and nothing on
the page says the model never saw a revenue figure. It is the case the file-level match
check exists for, so it is committed as data rather than described in a comment.
"""
import csv
import os
import sys

import numpy as np

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _DIR)          # this lives in scripts/, the generator lives at the root

import make_synthetic_data as synth  # noqa: E402
FIXTURES = os.path.join(_DIR, 'tests', 'fixtures')

HEARTH_ROWS = 1200          # enough for the ratios to be stable, small enough to read
HEARTH_SEED = 4471
UTM_ARTEFACT_SEED = 11
UTM_ARTEFACT_SHARE = 0.36   # the real export carried 'rd' on 1521 of its 4255 rows


def _write(name, header, rows):
    path = os.path.join(FIXTURES, name)
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    print(f'wrote tests/fixtures/{name}  {len(rows)} rows')


def hearth_repro():
    """The right schema with one column name the header map does not know.

    Drawn from the same generator as the training set, so the VALUES are all familiar —
    which is the point. Nothing is wrong with this file except a column name, and that is
    exactly the failure a metric over recognized values cannot see: the column is not
    there to be recognized. Only counting cells catches it."""
    df = synth.generate(HEARTH_ROWS, HEARTH_SEED, prefix='H', artefacts=False)
    rows = df.to_dict('records')
    # A CRM artefact value on about a third of the rows, as the real export had. These
    # pool into the encoder's infrequent bucket and still count as usable, so they must
    # NOT be what pushes this file over the line — the missing column has to do that on
    # its own, or the fixture is not testing what it claims to.
    rng = np.random.default_rng(UTM_ARTEFACT_SEED)
    for r in rows:
        if rng.random() < UTM_ARTEFACT_SHARE:
            r['utm_medium'] = 'rd'
    cols = ['lead_id', 'channel', 'icp_category', 'company_annual_revenue', 'utm_medium',
            'time_zone', 'legacy_score', 'state']
    header = [('contractor_annual_revenue' if c == 'company_annual_revenue' else c)
              for c in cols]
    _write('hearth_repro.csv', header, [[r[c] for c in cols] for r in rows])


def foreign_vocabulary():
    """Every column recognized, not one value the model has ever seen. Another CRM's
    words for the same ideas — which is the case that most deserves a refusal, because
    the board it produces today looks completely normal."""
    _write('foreign_vocabulary.csv',
           ['lead_id', 'channel', 'icp_category', 'company_annual_revenue', 'utm_medium',
            'state'],
           [['F-1', 'LinkedIn', 'Enterprise', 'Series B', 'inmail', 'CA'],
            ['F-2', 'Webinar', 'Mid-Market', 'Seed', 'email', 'NY'],
            ['F-3', 'LinkedIn', 'SMB', 'Series A', 'inmail', 'TX'],
            ['F-4', 'Referral', 'Enterprise', 'Series C', 'partner', 'WA'],
            ['F-5', 'Cold Call', 'SMB', 'Bootstrapped', 'phone', 'FL']])


def half_familiar():
    """Two of the four key fields present, and the values only half recognized. Sits at
    0.25, which is the number a file gets when one field in four is usable."""
    _write('half_familiar.csv',
           ['lead_id', 'channel', 'icp_category', 'notes', 'owner'],
           [['H-1', 'google', 'Enterprise', 'call back', 'ana'],
            ['H-2', 'LinkedIn', 'High Value', '', 'ben'],
            ['H-3', 'google', 'Enterprise', '', 'cal'],
            ['H-4', 'tiktok', 'SMB', '', 'dee']])


def all_blank_values():
    """Every column right, every value empty. There is nothing here to rank, and the
    denominator is zero — the case that would be a crash if coverage were a plain
    division."""
    _write('all_blank_values.csv',
           ['lead_id', 'channel', 'icp_category', 'company_annual_revenue', 'utm_medium'],
           [['B-1', '', '', '', ''], ['B-2', '', '', '', ''], ['B-3', '', '', '', '']])


def main():
    os.makedirs(FIXTURES, exist_ok=True)
    hearth_repro()
    foreign_vocabulary()
    half_familiar()
    all_blank_values()


if __name__ == '__main__':
    main()
