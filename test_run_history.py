"""Run history: the fingerprint, what /health flags, and the promise that none of it can
cost a rep a board.

The feature is bookkeeping, which makes its failure modes quiet ones. Three things are
worth a test rather than a read-through:

  THE FINGERPRINT HAS TO BE BORING in exactly the right way. A reordered or recased export
  is the same schema and must hash the same, or every run reads as a change and the page
  trains its reader to ignore it. A renamed or dropped column is a different schema and
  must hash differently, or the page misses the one failure it was built for.

  THE FLAGS ARE ABOUT CHANGE, not level. A source that has always been poor is a known
  limitation; a source that got worse is news.

  A LOGGING FAILURE IS NOT A SCORING FAILURE. Every test below that breaks the store
  asserts on the board, not on the error.

No credentials and no network. The Salesforce case monkeypatches the one function that
touches the org, matching test_salesforce.py.
"""
import csv
import io
import json
import sqlite3

import pytest

import app
import runs


# ---------------------------------------------------------------------------
# Fixtures. The store is a real SQLite file on a temp path -- not a fake, because the
# thing most likely to be wrong here is the SQL, and a stub would pass either way.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_store_by_default(monkeypatch):
    """Every test starts with no store configured, whatever the developer's shell has set.
    A suite whose results depend on an env var is a suite that passes on one machine."""
    monkeypatch.delenv(runs.ENV_VAR, raising=False)


@pytest.fixture()
def store(tmp_path, monkeypatch, _no_store_by_default):
    """A configured, working store. Depends on the autouse fixture explicitly so the
    ordering is stated rather than inherited from pytest's."""
    path = tmp_path / 'runs.db'
    monkeypatch.setenv(runs.ENV_VAR, str(path))
    return path


@pytest.fixture()
def client():
    return app.app.test_client()


# A file the model reads cleanly: every key field present and every value one it was fit
# on, so coverage lands at 1.0 and any drop in a test below is the test's own doing.
COLUMNS = ['lead_id', 'channel', 'icp_category', 'company_annual_revenue',
           'utm_medium', 'legacy_score', 'state']
VALUES = {'lead_id': 'L-0', 'channel': 'google', 'icp_category': 'High Value',
          'company_annual_revenue': '$1,000,000 to $4,999,999', 'utm_medium': 'brand',
          'legacy_score': '70', 'state': 'TX'}


def csv_bytes(columns, n=5, values=None):
    """A CSV with these HEADERS, exactly as written. Each column carries the value of
    whichever canonical field its normalized name resolves to, so recasing a header
    changes the header and nothing else -- which is the whole thing under test. `values`
    overrides by header string, for the renamed-column case where the data is unchanged
    and only the name on top of it moved."""
    values = dict(values or {})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for i in range(n):
        row = []
        for h in columns:
            if h in values:
                row.append(values[h])
            else:
                v = VALUES.get(app._norm_header(h), '')
                row.append(f'L-{i}' if app._norm_header(h) == 'lead_id' else v)
        w.writerow(row)
    return buf.getvalue().encode()


def upload(client, raw, name='leads.csv'):
    """POST /rank and follow the redirect, exactly as a browser does. Returns the board."""
    posted = client.post('/rank', data={'csv': (io.BytesIO(raw), name)},
                         content_type='multipart/form-data')
    assert posted.status_code == 303, 'upload did not Post/Redirect/Get'
    page = client.get(posted.headers['Location'])
    assert page.status_code == 200
    return page.get_data(as_text=True)


def recased(columns):
    """'company_annual_revenue' -> 'Company Annual Revenue'. Different bytes on row 1,
    the same column to anything that reads it."""
    return [c.replace('_', ' ').title() for c in columns]


def logged(source=None):
    """Runs from the store, newest first, optionally one source's."""
    rows = runs.recent()
    assert rows is not None, 'the store was configured and could not be read'
    return [r for r in rows if source is None or r['source'] == source]


# ---------------------------------------------------------------------------
# 1. The fingerprint. A hash of the header SET -- not of the file, and not of the order.
# ---------------------------------------------------------------------------
def test_the_fingerprint_is_stable_across_column_reorder_and_case():
    """Same columns, written three ways. One schema, one fingerprint.

    This is the test that decides whether the page is worth looking at. A fingerprint that
    moved when somebody exported with the columns in a different order would flag every
    run, and a flag that fires every time is not a flag."""
    plain = runs.schema_fingerprint(COLUMNS)
    assert plain
    assert runs.schema_fingerprint(list(reversed(COLUMNS))) == plain
    assert runs.schema_fingerprint(recased(COLUMNS)) == plain
    assert runs.schema_fingerprint(recased(reversed(COLUMNS))) == plain
    # Duplicated header, same set. A CSV with the column twice is a mess on the row, not
    # a different schema.
    assert runs.schema_fingerprint(COLUMNS + [COLUMNS[1]]) == plain


def test_the_fingerprint_moves_when_a_column_is_renamed_or_dropped():
    """The two changes the whole feature exists to see. Both are ordinary-looking files."""
    plain = runs.schema_fingerprint(COLUMNS)
    renamed = ['contractor_annual_revenue' if c == 'company_annual_revenue' else c
               for c in COLUMNS]
    assert runs.schema_fingerprint(renamed) != plain
    dropped = [c for c in COLUMNS if c != 'company_annual_revenue']
    assert runs.schema_fingerprint(dropped) != plain
    assert runs.schema_fingerprint(renamed) != runs.schema_fingerprint(dropped)
    assert runs.schema_fingerprint([]) == ''


def test_a_reordered_upload_records_the_same_fingerprint(store, client):
    """The same claim, through the real route: two uploads of one schema, two runs, one
    fingerprint. The unit test above pins the function; this pins the wiring, which is
    where a header could quietly be sorted, dropped or counted instead of hashed."""
    upload(client, csv_bytes(COLUMNS))
    upload(client, csv_bytes(recased(reversed(COLUMNS))))
    runs_logged = logged('upload')
    assert len(runs_logged) == 2
    assert runs_logged[0]['fingerprint'] == runs_logged[1]['fingerprint']


# ---------------------------------------------------------------------------
# 2. What /health flags. Both rules compare a run with the previous run of the SAME
# source, so a run is only ever news relative to its own history.
# ---------------------------------------------------------------------------
def test_a_schema_change_between_runs_is_flagged(store, client):
    """Upload a file, then the same file with one column renamed -- the README's failure,
    in which every row still scores and the board still looks fine."""
    upload(client, csv_bytes(COLUMNS))
    renamed = ['contractor_annual_revenue' if c == 'company_annual_revenue' else c
               for c in COLUMNS]
    upload(client, csv_bytes(renamed, values={
        'contractor_annual_revenue': VALUES['company_annual_revenue']}))

    source = app._health()['sources'][0]
    assert source['source'] == 'upload'
    newest, first = source['rows'][0], source['rows'][1]
    assert any('Schema changed' in f for f in newest['flags'])
    assert first['flags'] == [], 'the first run of a source has nothing to have changed from'

    page = client.get('/health').get_data(as_text=True)
    assert 'Schema changed' in page


def test_a_coverage_drop_below_the_notice_threshold_is_flagged(store, client):
    """Identical columns, so the fingerprint does not move: this isolates the coverage
    rule from the schema rule. The revenue column arrives full of a value the model was
    never fit on, which is unusable without being missing -- one of the four key fields
    gone, so coverage lands at 0.75 against a MATCH_NOTICE of 0.80."""
    upload(client, csv_bytes(COLUMNS))
    upload(client, csv_bytes(COLUMNS, values={'company_annual_revenue': 'Banana'}))

    source = app._health()['sources'][0]
    newest, before = source['rows'][0], source['rows'][1]
    assert before['pct'] > newest['pct']
    assert before['pct'] >= app.MATCH_NOTICE * 100 > newest['pct']
    assert newest['fingerprint'] == before['fingerprint'], 'the schema did not change'
    assert not any('Schema changed' in f for f in newest['flags'])
    assert any('Coverage crossed below' in f for f in newest['flags'])


def test_a_drop_that_stays_above_the_threshold_is_not_flagged():
    """The rule is a downward CROSSING, not any decline. 0.99 -> 0.85 is still a board
    worth trusting and the tool says nothing about it on the board either; flagging it
    here would put the page out of step with the thing it is watching."""
    prev = {'fingerprint': 'abc', 'coverage': 0.99}
    assert app._run_flags({'fingerprint': 'abc', 'coverage': 0.85}, prev) == []
    assert app._run_flags({'fingerprint': 'abc', 'coverage': 0.79}, prev)
    # Already below, staying below: news the first time, not every time after.
    below = {'fingerprint': 'abc', 'coverage': 0.60}
    assert app._run_flags({'fingerprint': 'abc', 'coverage': 0.55}, below) == []


def test_a_run_is_judged_against_its_own_source(store, client, monkeypatch):
    """Two sources interleaved. A Salesforce pull between two uploads must not make the
    second upload look like a schema change, or the page is unreadable the moment more
    than one thing is feeding the tool."""
    upload(client, csv_bytes(COLUMNS))
    monkeypatch.setattr(app, '_sf_query_leads', lambda: (
        [{'Id': f'00Q{i:015d}', 'LeadSource': 'google', 'AnnualRevenue': 2_500_000,
          'State': 'Texas', 'Rating': 'High Value'} for i in range(5)],
        'https://example-dev-ed.develop.my.salesforce.com', '0055g00000ABCDEAA3'))
    posted = client.post('/rank/salesforce')
    client.get(posted.headers['Location'])
    upload(client, csv_bytes(COLUMNS))

    by_source = {s['source']: s for s in app._health()['sources']}
    assert set(by_source) == {'upload', 'salesforce'}
    assert by_source['upload']['rows'][0]['flags'] == []
    assert by_source['salesforce']['runs'] == 1


# ---------------------------------------------------------------------------
# 3. Every intake path lands in the history, because every one of them goes through
# _summarize. A path that stopped being recorded would be invisible rather than loud.
# ---------------------------------------------------------------------------
def test_each_intake_path_records_its_own_source(store, client, monkeypatch):
    upload(client, csv_bytes(COLUMNS))
    client.get(client.post('/rank/sample').headers['Location'])
    client.post('/api/score', json={'lead_id': 'A-1', 'channel': 'google',
                                    'icp_category': 'High Value', 'utm_medium': 'brand',
                                    'company_annual_revenue': '$1,000,000 to $4,999,999'})
    monkeypatch.setattr(app, '_sf_query_leads', lambda: (
        [{'Id': '00Q000000000001', 'LeadSource': 'google', 'AnnualRevenue': 2_500_000,
          'State': 'Texas', 'Rating': 'High Value'}],
        'https://example-dev-ed.develop.my.salesforce.com', '0055g00000ABCDEAA3'))
    client.get(client.post('/rank/salesforce').headers['Location'])

    assert {r['source'] for r in logged()} == set(app.ORIGINS)
    for r in logged():
        assert r['rows_in'] >= 1 and r['fingerprint']


def test_a_refused_file_is_still_recorded(store, client):
    """The run most worth having in the history is the one the tool would not rank. A
    refusal is a scored run -- the rows exist, the coverage is real, the board is
    withheld -- so it is recorded like any other, band and all."""
    raw = csv_bytes(['lead_id', 'state'])
    posted = client.post('/rank', data={'csv': (io.BytesIO(raw), 'thin.csv')},
                         content_type='multipart/form-data')
    page = client.get(posted.headers['Location']).get_data(as_text=True)
    assert 'not ranked' in page
    entry = logged('upload')[0]
    assert entry['band'] == 'refuse' and entry['scored'] == 5


def test_an_unreadable_file_records_nothing(store, client):
    """Nothing was scored, so there is no run to describe. read_leads raised before any
    row reached the model, and a history row claiming 0% coverage on 0 rows would be a
    sentence about a file, not about a run."""
    posted = client.post('/rank', data={'csv': (io.BytesIO(b'a,b,c\n1,2,3\n'), 'x.csv')},
                         content_type='multipart/form-data')
    assert 'Could not read' in client.get(posted.headers['Location']).get_data(as_text=True)
    assert logged() == []


# ---------------------------------------------------------------------------
# 4. The store is optional and the bookkeeping is expendable. Both of these assert on the
# BOARD: the point is not that the failure is handled, it is that nobody notices it.
# ---------------------------------------------------------------------------
def test_a_failed_write_leaves_the_scoring_run_intact(store, client, monkeypatch):
    """The store is configured, and connecting to it blows up on every attempt. The rep
    still gets the board they uploaded a file to get."""
    def explode(*a, **kw):
        raise sqlite3.OperationalError('unable to open database file')
    monkeypatch.setattr(runs.sqlite3, 'connect', explode)

    page = upload(client, csv_bytes(COLUMNS))
    assert 'leads, ranked' in page
    assert 'L-0' in page
    assert runs.record('upload', 'abc', 1.0, 'ok', 5, 5, 0) is False
    assert runs.recent() is None


def test_a_store_that_raises_above_sqlite_still_leaves_the_run_intact(store, client, monkeypatch):
    """The outer guard, for anything runs.py never anticipated: the write itself is
    replaced with a function that raises a type sqlite3 would never produce. _record_run
    catches on top of runs.record because the contract is "the run survives", not "the
    store handles its own errors"."""
    def explode(**kw):
        raise RuntimeError('the store is on fire')
    monkeypatch.setattr(runs, 'record', explode)
    assert 'leads, ranked' in upload(client, csv_bytes(COLUMNS))


def test_no_store_configured_ranks_normally_and_health_says_so(client):
    """The sf_configured posture: unset is a supported way to run this, not a fault. The
    board is a board, the API answers, and /health says plainly that nothing is kept."""
    assert not runs.configured()
    assert 'leads, ranked' in upload(client, csv_bytes(COLUMNS))
    api = client.post('/api/score', json={'lead_id': 'A-1', 'channel': 'google'})
    assert api.status_code == 200 and json.loads(api.get_data())['lead_id'] == 'A-1'

    page = client.get('/health')
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert 'No run history is being kept' in body and runs.ENV_VAR in body


def test_a_configured_store_that_cannot_be_read_does_not_read_as_a_quiet_week(store, client,
                                                                              monkeypatch):
    """The state that must never be silent. Configured plus unreadable is a fault, and an
    empty page would be indistinguishable from a week where nobody uploaded anything."""
    monkeypatch.setattr(runs.sqlite3, 'connect',
                        lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError('gone')))
    assert app._health()['state'] == 'unavailable'
    body = client.get('/health').get_data(as_text=True)
    assert 'could not be read' in body


def test_a_working_empty_store_says_nothing_has_run_yet(store, client):
    assert app._health()['state'] == 'ok'
    body = client.get('/health').get_data(as_text=True)
    assert 'nothing has been scored yet' in body


# ---------------------------------------------------------------------------
# 5. Nothing about a score moved. The golden export test is the real tripwire for this;
# these two are the cheap local version of the same question.
# ---------------------------------------------------------------------------
def test_the_same_file_ranks_identically_with_and_without_a_store(client, tmp_path,
                                                                 monkeypatch):
    raw = csv_bytes(COLUMNS)
    without = upload(client, raw)
    monkeypatch.setenv(runs.ENV_VAR, str(tmp_path / 'runs.db'))
    with_store = upload(client, raw)
    assert logged('upload')
    # The board carries a fresh token in its links, which is the one thing that legitimately
    # differs between two identical uploads. Everything else has to match.
    assert _tokenless(without) == _tokenless(with_store)


def _tokenless(html):
    import re
    return re.sub(r'/(results|calibrate)/[A-Za-z0-9_-]+', '/\\1/TOKEN', html)


def test_reading_health_is_not_itself_a_run(store, client):
    upload(client, csv_bytes(COLUMNS))
    before = len(logged())
    client.get('/health')
    client.get('/health')
    assert len(logged()) == before
