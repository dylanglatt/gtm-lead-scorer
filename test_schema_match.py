"""How well an upload matches the model, measured at the size of the file.

The tool has always flagged an unfamiliar value on the row it appeared on. What it could
not say was "this whole file is not the shape I was trained on", and the gap was not
theoretical: an export whose revenue column is named contractor_annual_revenue produced a
fully tiered board on which not one lead of 4,255 reached High confidence, with nothing on
the page to explain it. tests/fixtures/hearth_repro.csv is that file, reproduced.

These tests pin the measure. The response built on top of it — refuse, notice, or say
nothing — is tested in test_schema_guard.py.

The numbers here are checked against real files rather than chosen: the fixture that is
deliberately broken nineteen ways still measures 0.89 and keeps its board, and the file
whose only fault is a column name measures 0.75 and does not.
"""
import io
import os

import pytest

import app
import scorer

_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(_DIR, 'tests', 'fixtures')


def summarize(raw):
    """A file's run summary, by the same path /rank takes to build one."""
    leads, report = app.read_leads(raw)
    res = app.score_rows([lead for lead, _why in leads])
    for r, (_lead, why) in zip(res, leads):
        r['row_notes'] = why
    return app._summarize(res, report, len(leads))


def fixture(name):
    with open(os.path.join(FIXTURES, name), 'rb') as fh:
        return fh.read()


def repo_file(name):
    with open(os.path.join(_DIR, name), 'rb') as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# The two files that ship. Neither may ever be told it does not match.
# ---------------------------------------------------------------------------
def test_the_demo_file_matches_the_model():
    s = summarize(repo_file('demo_leads.csv'))
    assert s['band'] == 'ok'
    assert s['coverage'] == pytest.approx(0.984, abs=0.005)
    assert s['total_cells'] == 250 * len(scorer.KEY_FIELDS)


def test_the_messy_fixture_still_matches_and_keeps_its_board():
    """The hard requirement. leads_messy_fixture.csv is broken on purpose in nineteen
    different ways and every one of them is a row-level problem — ragged rows, duplicate
    and missing ids, junk numbers. Row-level mess is not a schema mismatch, and if this
    file ever stops matching, the measure has started punishing the wrong thing."""
    s = summarize(repo_file('leads_messy_fixture.csv'))
    assert s['band'] == 'ok'
    assert s['coverage'] == pytest.approx(0.889, abs=0.005)
    assert s['coverage'] - app.MATCH_NOTICE > 0.05, 'headroom is gone; re-check the number'
    assert s['rows_in'] == 19


# ---------------------------------------------------------------------------
# The case the measure exists for.
# ---------------------------------------------------------------------------
def test_a_renamed_column_is_caught_even_though_every_value_is_familiar():
    """hearth_repro.csv has nothing wrong with it but a column name. Every value it does
    carry is one the model knows, so a metric over recognized VALUES reads it as fine —
    the column that is missing contributes no values to be judged. Counting CELLS, the
    absent column is unusable on every row, which is the truth."""
    s = summarize(fixture('hearth_repro.csv'))
    assert s['band'] == 'notice'
    assert s['coverage'] == pytest.approx(0.746, abs=0.01)
    # The cause is nameable, which is what makes the notice worth showing.
    assert s['unusable_by_field']['company_annual_revenue'] == s['scored']
    assert 'contractor_annual_revenue' in s['ignored_cols']
    assert 'company_annual_revenue' in s['missing_cols']
    # And the symptom the user actually saw on the live board.
    assert s['confidence']['High'] == 0


def test_the_same_file_named_correctly_matches():
    """The control. One column name is the entire difference between the two files, so it
    has to be the entire difference in the verdict."""
    s = summarize(fixture('hearth_repro.csv')
                  .replace(b'contractor_annual_revenue', b'company_annual_revenue', 1))
    assert s['band'] == 'ok'
    assert s['coverage'] > 0.98


def test_a_pooled_artefact_value_counts_as_usable():
    """A third of hearth_repro.csv carries utm_medium='rd', which the encoder pools into
    its infrequent bucket. Pooling is not dropping — pooled values share a fitted
    coefficient, so they carry signal — and counting them against a file would penalise it
    for containing exactly the CRM artefacts the model was trained on. If they counted as
    unusable this file would read about 0.65 and the control above would fall to 0.89."""
    flagged = {field for field, _flag in scorer._unusable({'utm_medium': 'rd'}, {})}
    assert 'utm_medium' not in flagged, "'rd' is a level the model was fit on"
    s = summarize(fixture('hearth_repro.csv'))
    assert s['unusable_by_field'].get('utm_medium', 0) < 30, 'pooled values were counted'


# ---------------------------------------------------------------------------
# Files with nothing to rank.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('name,expected', [
    ('foreign_vocabulary.csv', 0.0),      # every column read, not one value recognized
    ('half_familiar.csv', 0.25),          # one field in four usable
    ('all_blank_values.csv', 0.0),        # every column right, every value empty
])
def test_files_the_model_cannot_read(name, expected):
    s = summarize(fixture(name))
    assert s['band'] == 'refuse'
    assert s['coverage'] == pytest.approx(expected, abs=0.01)


def test_a_file_with_no_scorable_rows_is_the_worst_match_not_a_crash():
    """No row carries an id, so no row is scored, so there are no cells to divide by.
    Coverage is a ratio and this is the input that has no denominator: it reads as the
    worst possible match rather than as a division by zero or an accidental pass."""
    raw = b'lead_id,channel,icp_category\n,google,Ideal\n,google,Ideal\n'
    s = summarize(raw)
    assert s['scored'] == 0 and s['total_cells'] == 0
    assert s['coverage'] == 0.0
    assert s['band'] == 'refuse'


# ---------------------------------------------------------------------------
# The thresholds themselves.
# ---------------------------------------------------------------------------
def test_the_bands_are_read_from_the_constants_and_include_their_edge():
    assert app.MATCH_REFUSE < app.MATCH_NOTICE
    assert app.match_band(1.0) == 'ok'
    assert app.match_band(app.MATCH_NOTICE) == 'ok'
    assert app.match_band(app.MATCH_NOTICE - 0.0001) == 'notice'
    assert app.match_band(app.MATCH_REFUSE) == 'notice'
    assert app.match_band(app.MATCH_REFUSE - 0.0001) == 'refuse'
    assert app.match_band(0.0) == 'refuse'
