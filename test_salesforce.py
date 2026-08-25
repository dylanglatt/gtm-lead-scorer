"""Salesforce integration: field mapping, the provenance note on the ranked board, and the
rate limit that protects the connected org's API budget.

/rank/salesforce and /salesforce/leads both call _sf_query_leads(), which does the real
network work (token exchange, then the SOQL query). Every test below monkeypatches that one
function rather than mocking urllib -- the network call itself is exercised by hand against
a real Developer Edition org (see scripts/seed_salesforce_leads.py); what belongs in a suite
that runs on every commit is everything downstream of a Salesforce response: field mapping,
ranking, the "Pulled live from Salesforce" note, and the rate limit, all of which are
ordinary Python this repo can actually check without touching the network.
"""
import pytest

import app


@pytest.fixture()
def client():
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _reset_sf_rate_limit():
    """Every test starts with a full rate-limit budget, and leaves one behind for the next
    -- otherwise which test runs first would decide which one trips the cap."""
    app._SF_CALL_TIMES.clear()
    yield
    app._SF_CALL_TIMES.clear()


def sf_lead(id_, **fields):
    """A raw Salesforce Lead record, unmapped -- Id plus whatever SF_LEAD_FIELDS asks for,
    defaulting the rest to None the way an org with those fields genuinely blank would."""
    rec = {'Id': id_, 'LeadSource': None, 'AnnualRevenue': None, 'State': None, 'Rating': None}
    rec.update(fields)
    return rec


ORG = 'https://orgfarm-test-dev-ed.develop.my.salesforce.com'
GOOD_LEADS = [
    sf_lead(f'00Q00000000000{i}AAA', LeadSource='google', AnnualRevenue=2_500_000,
            State='Texas', Rating='High Value')
    for i in range(10)
]


def rank_salesforce(client, monkeypatch, records, org=ORG):
    """Pull-and-rank through the real route, with _sf_query_leads swapped for canned data --
    same Post/Redirect/Get shape /rank and /rank/sample use, so this exercises the exact
    path a click on 'Pull from Salesforce' takes."""
    monkeypatch.setattr(app, '_sf_query_leads', lambda: (records, org))
    posted = client.post('/rank/salesforce')
    assert posted.status_code == 303, 'Salesforce pull did not Post/Redirect/Get'
    page = client.get(posted.headers['Location'])
    assert page.status_code == 200
    return page.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Field mapping: Salesforce's own field names, not scorer's, have to resolve -- CRM_ALIASES
# extending API_HEADER_MAP the same way test_schema_match.py holds CSV headers to.
# ---------------------------------------------------------------------------
def test_native_salesforce_fields_score_the_same_as_native_scorer_fields():
    sf_style = app._api_map_lead({'Id': '00Q1', 'LeadSource': 'google',
                                   'AnnualRevenue': 2_500_000, 'State': 'Texas',
                                   'Rating': 'High Value'})
    native = {'lead_id': '00Q1', 'channel': 'google', 'company_annual_revenue': 2_500_000,
              'state': 'Texas', 'icp_category': 'High Value'}
    scored_sf = app.score_row(sf_style, app.APPLIED['cut'])
    scored_native = app.score_row(native, app.APPLIED['cut'])
    assert not scored_sf.get('error') and not scored_native.get('error')
    assert scored_sf['score'] == scored_native['score']


def test_salesforce_lead_id_is_read_from_the_id_field():
    mapped = app._api_map_lead({'Id': '00Q000000012345AAA', 'LeadSource': 'meta'})
    assert mapped['lead_id'] == '00Q000000012345AAA'


def test_salesforce_native_vocabulary_is_flagged_not_guessed():
    """'Web' / 'Hot' are real Salesforce LeadSource/Rating values this model was never
    trained on -- same 'never guess' contract test_scorer.py holds for CSV uploads."""
    mapped = app._api_map_lead({'Id': '00Q9', 'LeadSource': 'Web', 'Rating': 'Hot'})
    scored = app.score_row(mapped, app.APPLIED['cut'])
    assert not scored.get('error')
    assert scored['confidence'] != 'High'


# ---------------------------------------------------------------------------
# The button: visible only with all three env vars set.
# ---------------------------------------------------------------------------
def test_pull_from_salesforce_button_hidden_without_full_config(client, monkeypatch):
    monkeypatch.delenv('SF_LOGIN_URL', raising=False)
    monkeypatch.delenv('SF_CONSUMER_KEY', raising=False)
    monkeypatch.delenv('SF_CONSUMER_SECRET', raising=False)
    assert 'Pull from Salesforce' not in client.get('/').get_data(as_text=True)


def test_pull_from_salesforce_button_shown_with_full_config(client, monkeypatch):
    monkeypatch.setenv('SF_LOGIN_URL', 'https://example.my.salesforce.com')
    monkeypatch.setenv('SF_CONSUMER_KEY', 'k')
    monkeypatch.setenv('SF_CONSUMER_SECRET', 's')
    assert 'Pull from Salesforce' in client.get('/').get_data(as_text=True)


# ---------------------------------------------------------------------------
# Provenance: a Salesforce-sourced board says so, ranked or declined; a CSV/sample board
# never does, since it has nothing to attribute.
# ---------------------------------------------------------------------------
def test_ranked_board_from_salesforce_shows_where_it_came_from(client, monkeypatch):
    page = rank_salesforce(client, monkeypatch, GOOD_LEADS)
    assert 'Pulled live from' in page and 'Salesforce' in page
    assert 'orgfarm-test-dev-ed.develop.my.salesforce.com' in page
    assert '/salesforce/leads' in page


def test_declined_salesforce_board_still_shows_where_it_came_from(client, monkeypatch):
    sparse = [sf_lead(f'00Q{i}') for i in range(10)]        # every scorable field blank
    page = rank_salesforce(client, monkeypatch, sparse)
    assert 'Pulled live from' in page


def test_sample_file_board_carries_no_salesforce_note(client):
    posted = client.post('/rank/sample')
    page = client.get(posted.headers['Location']).get_data(as_text=True)
    assert 'Pulled live from' not in page


# ---------------------------------------------------------------------------
# Failure paths: a bad token/query or an empty org declines instead of crashing or ranking
# nothing as if it were something.
# ---------------------------------------------------------------------------
def test_salesforce_error_is_declined_not_a_crash(client, monkeypatch):
    def fail():
        raise RuntimeError('Salesforce auth failed (401): invalid_client_id')
    monkeypatch.setattr(app, '_sf_query_leads', fail)
    posted = client.post('/rank/salesforce')
    page = client.get(posted.headers['Location']).get_data(as_text=True)
    assert 'Could not pull from Salesforce' in page


def test_empty_org_is_declined_not_ranked(client, monkeypatch):
    monkeypatch.setattr(app, '_sf_query_leads', lambda: ([], ORG))
    posted = client.post('/rank/salesforce')
    page = client.get(posted.headers['Location']).get_data(as_text=True)
    assert 'no Lead records' in page


def test_raw_json_view_502_on_salesforce_error(client, monkeypatch):
    def fail():
        raise RuntimeError('Salesforce query failed (500): boom')
    monkeypatch.setattr(app, '_sf_query_leads', fail)
    resp = client.get('/salesforce/leads')
    assert resp.status_code == 502


def test_raw_json_view_reports_the_org(client, monkeypatch):
    monkeypatch.setattr(app, '_sf_query_leads', lambda: (GOOD_LEADS, ORG))
    resp = client.get('/salesforce/leads')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['org'] == ORG
    assert body['source'] == 'salesforce'
    assert body['count'] == len(GOOD_LEADS)


# ---------------------------------------------------------------------------
# The rate limit: caps how often the connected org gets hit, not who's asking, and is
# shared across both routes since both hit the same org.
# ---------------------------------------------------------------------------
def test_salesforce_pull_is_refused_past_the_hourly_cap(client, monkeypatch):
    monkeypatch.setattr(app, 'SF_MAX_CALLS_PER_HOUR', 3)
    calls = {'n': 0}

    def fake_query():
        calls['n'] += 1
        return GOOD_LEADS, ORG
    monkeypatch.setattr(app, '_sf_query_leads', fake_query)

    for _ in range(3):
        posted = client.post('/rank/salesforce')
        page = client.get(posted.headers['Location']).get_data(as_text=True)
        assert 'hit its cap' not in page

    posted = client.post('/rank/salesforce')
    page = client.get(posted.headers['Location']).get_data(as_text=True)
    assert 'hit its cap' in page
    assert calls['n'] == 3, 'a refused call must never reach Salesforce'


def test_raw_json_view_shares_the_rate_limit_window_with_the_ranked_board(client, monkeypatch):
    monkeypatch.setattr(app, 'SF_MAX_CALLS_PER_HOUR', 1)
    monkeypatch.setattr(app, '_sf_query_leads', lambda: (GOOD_LEADS, ORG))
    client.post('/rank/salesforce')                          # spends the one call
    resp = client.get('/salesforce/leads')
    assert resp.status_code == 429
