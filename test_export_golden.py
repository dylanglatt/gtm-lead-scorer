"""The one test that has to stay green while anything else changes.

`docs/spec-any-csv.md` proposes routing foreign CSVs to other engines. Every phase of that
work, and the smaller honesty banner built ahead of it, is a change made *around* the
scoring path while the scoring path itself must not move. This file is the tripwire: the
exact bytes `/export` produced for `demo_leads.csv` at the baseline commit, asserted on
every run.

It is deliberately a byte comparison rather than a set of assertions about columns. An
assertion only catches what it was written to look for; the bytes catch a changed score in
the fourth decimal, a re-ordered board, a renamed tier, a new column, a different quoting
rule, and anything else nobody thought to check.

To regenerate — which should happen only when a change to the scored output is *intended*,
and the diff belongs in that commit's message:

    python3 -c "import test_export_golden as t; t.regenerate()"
"""
import os

import pytest

import app
import scorer

_DIR = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(_DIR, 'demo_leads.csv')
GOLDEN = os.path.join(_DIR, 'tests', 'golden', 'demo_export.csv')


def _export_bytes():
    """The export exactly as a user downloading it would receive it: the real upload
    route, the real redirect, the real default filters."""
    client = app.app.test_client()
    with open(DEMO, 'rb') as fh:
        posted = client.post('/rank', data={'csv': (fh, 'demo_leads.csv')},
                             content_type='multipart/form-data')
    assert posted.status_code == 303, f'upload did not redirect: {posted.status_code}'
    exported = client.get(posted.headers['Location'].rstrip('/') + '/export')
    assert exported.status_code == 200, f'export did not render: {exported.status_code}'
    return exported.get_data()


@pytest.fixture()
def default_cutoffs():
    """The export carries a tier column, and tiers come from the team-wide cutoffs in
    APPLIED, which Manager · Calibration writes at runtime. Pin them to the defaults so
    this test asserts the same thing whatever else has run first."""
    previous = dict(app.APPLIED['cut'])
    app.APPLIED['cut'] = dict(scorer.DEFAULT_CUTOFFS)
    yield
    app.APPLIED['cut'] = previous


def test_the_demo_export_is_byte_identical_to_the_golden_file(default_cutoffs):
    """If this fails, a change altered the scored output. That may be correct — but it is
    never incidental, so it stops here until someone says so out loud."""
    with open(GOLDEN, 'rb') as fh:
        expected = fh.read()
    actual = _export_bytes()
    if actual == expected:
        return

    # A 250-row byte mismatch is unreadable as a raw diff, so point at the first line that
    # moved and say how many did.
    want = expected.decode('utf-8').splitlines()
    got = actual.decode('utf-8').splitlines()
    moved = [i for i, (a, b) in enumerate(zip(want, got), 1) if a != b]
    detail = [f'golden {len(want)} lines, produced {len(got)} lines',
              f'{len(moved)} line(s) differ']
    if moved:
        first = moved[0]
        detail += [f'first difference at line {first}:',
                   f'  golden:   {want[first - 1]}',
                   f'  produced: {got[first - 1]}']
    pytest.fail('/export for demo_leads.csv changed.\n' + '\n'.join(detail))


def regenerate():
    """Overwrite the golden file with what the app produces now. Not a test."""
    app.APPLIED['cut'] = dict(scorer.DEFAULT_CUTOFFS)
    os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
    with open(GOLDEN, 'wb') as fh:
        fh.write(_export_bytes())
    print(f'wrote {GOLDEN}')
