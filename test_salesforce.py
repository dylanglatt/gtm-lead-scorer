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
# The integration user's own Id, which the pull now returns alongside the records: it comes
# out of the identity URL Salesforce hands back with the token. Writeback compares
# LastModifiedById against it to tell this tool's own edits from a person's, so every stub
# below has to carry one -- a pull with no identity is a pull writeback refuses to act on.
INTEGRATION_USER = '0055g00000ABCDEAA3'
HUMAN_USER = '0055g00000ZZZZZAA1'
GOOD_LEADS = [
    sf_lead(f'00Q00000000000{i}AAA', LeadSource='google', AnnualRevenue=2_500_000,
            State='Texas', Rating='High Value')
    for i in range(10)
]


def rank_salesforce(client, monkeypatch, records, org=ORG):
    """Pull-and-rank through the real route, with _sf_query_leads swapped for canned data --
    same Post/Redirect/Get shape /rank and /rank/sample use, so this exercises the exact
    path a click on 'Pull from Salesforce' takes."""
    monkeypatch.setattr(app, '_sf_query_leads', lambda: (records, org, INTEGRATION_USER))
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
    monkeypatch.setattr(app, '_sf_query_leads', lambda: ([], ORG, INTEGRATION_USER))
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
    monkeypatch.setattr(app, '_sf_query_leads', lambda: (GOOD_LEADS, ORG, INTEGRATION_USER))
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
        return GOOD_LEADS, ORG, INTEGRATION_USER
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
    monkeypatch.setattr(app, '_sf_query_leads', lambda: (GOOD_LEADS, ORG, INTEGRATION_USER))
    client.post('/rank/salesforce')                          # spends the one call
    resp = client.get('/salesforce/leads')
    assert resp.status_code == 429

# ---------------------------------------------------------------------------
# The lift. Two custom Lead fields and two widened standard picklists are the whole point
# of the schema work: before them a Salesforce-sourced lead failed all four KEY_FIELDS and
# could not clear Low confidence, however clean the record was.
# ---------------------------------------------------------------------------
def test_a_fully_mapped_salesforce_lead_reaches_high_confidence():
    """The before/after in one test, because the after alone proves nothing -- High
    confidence on a lead nobody could reach before is the claim, and it needs its own
    counterexample standing next to it."""
    before = app._api_map_lead(sf_lead('00Q1', LeadSource='google', AnnualRevenue=2_500_000,
                                       State='Texas', Rating='High Value'))
    after = app._api_map_lead(dict(sf_lead('00Q1', LeadSource='google',
                                           AnnualRevenue=2_500_000, State='Texas',
                                           Rating='High Value'),
                                   Marketing_Channel__c='brand', Prior_Score__c=72.5))
    was = app.score_row(before, app.APPLIED['cut'])
    now = app.score_row(after, app.APPLIED['cut'])
    assert was['confidence'] == 'Low'
    assert sorted(was['unusable']) == ['legacy_score', 'utm_medium']
    assert now['confidence'] == 'High'
    assert now['unusable'] == []


def test_the_two_custom_fields_need_an_explicit_alias_to_resolve():
    """_norm_header turns 'Marketing_Channel__c' into 'marketing_channel_c', never
    'utm_medium'. No custom field on any object can resolve by accident, which is why both
    are named in CRM_ALIASES rather than assumed to work."""
    assert app._norm_header('Marketing_Channel__c') == 'marketing_channel_c'
    assert app._norm_header('Prior_Score__c') == 'prior_score_c'
    assert app.CRM_ALIASES['marketing_channel_c'] == 'utm_medium'
    assert app.CRM_ALIASES['prior_score_c'] == 'legacy_score'


def test_the_pull_asks_for_everything_writeback_needs():
    """SF_LEAD_FIELDS is one string in a SOQL query and three separate obligations. A field
    dropped from it fails somewhere far away -- an unread input silently costs confidence,
    a missing owned field makes every record look like it needs writing, and a missing
    audit field makes the clobber check undecidable."""
    asked = set(app.SF_LEAD_FIELDS.split(','))
    assert 'Id' in asked
    assert {'Marketing_Channel__c', 'Prior_Score__c'} <= asked
    assert set(app.SF_WRITE_FIELDS) <= asked, 'writeback cannot diff what it did not read'
    assert {'LastModifiedById', 'LastModifiedDate'} <= asked


# ---------------------------------------------------------------------------
# Writeback. Every test here monkeypatches _sf_token and _sf_patch -- the two functions
# that touch the network on a write -- so the batching, the rate limit and the per-record
# result handling are all exercised, and nothing reaches an org.
# ---------------------------------------------------------------------------
@pytest.fixture()
def sent(monkeypatch):
    """Captures every PATCH body the tool builds, and answers each one with a success per
    record. Returns the list of bodies, so a test asserting 'nothing was written' asserts
    on an empty list rather than on the absence of an error."""
    bodies = []

    def fake_patch(instance_url, token, body):
        bodies.append(body)
        return [{'id': r['Id'], 'success': True, 'errors': []} for r in body['records']]
    monkeypatch.setattr(app, '_sf_token', lambda: ('tok', ORG, INTEGRATION_USER))
    monkeypatch.setattr(app, '_sf_patch', fake_patch)
    return bodies


def scored_lead(i, **over):
    """A Lead that resolves every scoring field, so it reaches High confidence and has a
    verdict worth writing. `over` sets the tool-owned or audit fields a given test needs."""
    rec = sf_lead(f'00Q00000000000{i}AAA', LeadSource='google', AnnualRevenue=2_500_000,
                  State='Texas', Rating='High Value')
    rec.update({'Marketing_Channel__c': 'brand', 'Prior_Score__c': 70.0,
                'LeadScorer_Score__c': None, 'LeadScorer_Tier__c': None,
                'LeadScorer_Next_Action__c': None, 'LeadScorer_Confidence__c': None,
                'LeadScorer_Scored_At__c': None, 'LeadScorer_Model_Version__c': None,
                'LastModifiedById': INTEGRATION_USER,
                'LastModifiedDate': '2026-08-01T00:00:00.000+0000'})
    rec.update(over)
    return rec


def already_written(i, **over):
    """A record this tool has already scored: the six owned fields carry exactly what
    _sf_verdict would compute for it, so a second pull must write nothing."""
    rec = scored_lead(i)
    verdict = app._sf_verdict(app.score_row(app._api_map_lead(rec), app.APPLIED['cut']))
    rec.update(verdict)
    rec['LeadScorer_Scored_At__c'] = '2026-08-20T12:00:00.000+0000'
    rec['LastModifiedById'] = INTEGRATION_USER
    rec['LastModifiedDate'] = '2026-08-20T12:00:00.000+0000'
    rec.update(over)
    return rec


def board(client, monkeypatch, records):
    """Pull, rank, and return (token, page) so a test can read the panel and then post the
    write through the real route."""
    monkeypatch.setattr(app, '_sf_query_leads', lambda: (records, ORG, INTEGRATION_USER))
    posted = client.post('/rank/salesforce')
    location = posted.headers['Location']
    page = client.get(location).get_data(as_text=True)
    return location.rsplit('/', 1)[-1], page


def test_the_dry_run_sends_no_write_at_all(client, monkeypatch, sent):
    """Rendering the board is the dry run. Nothing leaves the process until somebody posts
    the form, which is the difference between a preview and an announcement."""
    token, page = board(client, monkeypatch, [scored_lead(i) for i in range(3)])
    assert 'would change' in page
    assert sent == [], 'drawing the panel wrote to Salesforce'


def test_a_refused_board_writes_nothing(client, monkeypatch, sent):
    """Below MATCH_REFUSE the tool would not put these leads in an order. Writing the same
    scores into the CRM is that refusal being quietly reversed by a different route, so
    there is no panel and the route itself declines to act."""
    sparse = [sf_lead(f'00Q{i}') for i in range(10)]          # every scorable field blank
    token, page = board(client, monkeypatch, sparse)
    assert 'not ranked' in page
    assert 'would change' not in page
    posted = client.post(f'/results/{token}/writeback')
    assert posted.status_code == 303
    assert sent == [], 'a declined board reached Salesforce'


def test_an_unchanged_record_is_not_written(client, monkeypatch, sent):
    """Idempotency, through the real route: a record whose six values already match is
    reported as matching and left out of the batch entirely."""
    token, page = board(client, monkeypatch, [already_written(i) for i in range(4)])
    assert '<span class="num">0</span> would change' in page
    assert '<span class="num">4</span> already match' in page
    assert '/writeback' not in page, 'offered a confirm button with nothing to write'
    client.post(f'/results/{token}/writeback')
    assert sent == [], 'rewrote records that already matched'


def test_a_manually_overridden_record_is_skipped_and_reported(client, monkeypatch, sent):
    """Somebody edited the record in Salesforce after this tool last scored it. The tool
    does not know WHICH field they touched -- Salesforce does not say without field
    history -- so it declines to write and says so rather than guessing it was safe."""
    touched = already_written(1, LeadScorer_Score__c=0.1,
                              LastModifiedById=HUMAN_USER,
                              LastModifiedDate='2026-08-25T09:00:00.000+0000')
    token, page = board(client, monkeypatch, [touched, scored_lead(2)])
    assert 'left alone' in page
    assert 'edited in Salesforce since this tool last scored it' in page
    client.post(f'/results/{token}/writeback')
    written = {r['Id'] for body in sent for r in body['records']}
    assert touched['Id'] not in written, 'overwrote a human'
    assert scored_lead(2)['Id'] in written, 'one skipped record stopped the others'


def test_a_record_this_tool_has_never_written_is_not_treated_as_overridden(client,
                                                                          monkeypatch, sent):
    """A blank Scored_At means there is nothing of ours to clobber. Without this the very
    first write would be skipped as an override of a score that was never there."""
    fresh = scored_lead(1, LastModifiedById=HUMAN_USER,
                        LastModifiedDate='2026-08-26T09:00:00.000+0000')
    token, page = board(client, monkeypatch, [fresh])
    assert 'would change' in page
    client.post(f'/results/{token}/writeback')
    assert {r['Id'] for body in sent for r in body['records']} == {fresh['Id']}


def test_a_partial_failure_is_reported_per_record(client, monkeypatch):
    """allOrNone=false, so one record failing a validation rule cannot lose the rest -- and
    which one failed, with Salesforce's own message, reaches the page."""
    def half_fail(instance_url, token, body):
        return [{'id': r['Id'], 'success': i != 1,
                 'errors': [] if i != 1 else
                 [{'message': 'Prior_Score__c: value outside of valid range'}]}
                for i, r in enumerate(body['records'])]
    monkeypatch.setattr(app, '_sf_token', lambda: ('tok', ORG, INTEGRATION_USER))
    monkeypatch.setattr(app, '_sf_patch', half_fail)

    token, _ = board(client, monkeypatch, [scored_lead(i) for i in range(3)])
    client.post(f'/results/{token}/writeback')
    page = client.get(f'/results/{token}').get_data(as_text=True)
    assert '<span class="num">2</span> record(s) written back' in page
    assert '<span class="num">1</span> failed' in page
    assert 'value outside of valid range' in page


def test_a_transport_failure_mid_write_does_not_lose_the_batches_before_it(client,
                                                                          monkeypatch):
    """An expired token on the second call says nothing about the first. Letting it out of
    the loop would report a partial write as if nothing had happened, which is the exact
    swallowing rule 5 exists to prevent."""
    calls = {'n': 0}

    def fail_second(instance_url, token, body):
        calls['n'] += 1
        if calls['n'] == 2:
            raise RuntimeError('Salesforce write failed (401): Session expired or invalid')
        return [{'id': r['Id'], 'success': True, 'errors': []} for r in body['records']]
    monkeypatch.setattr(app, '_sf_token', lambda: ('tok', ORG, INTEGRATION_USER))
    monkeypatch.setattr(app, '_sf_patch', fail_second)
    monkeypatch.setattr(app, 'SF_WRITE_BATCH', 2)

    token, _ = board(client, monkeypatch, [scored_lead(i) for i in range(6)])
    client.post(f'/results/{token}/writeback')
    page = client.get(f'/results/{token}').get_data(as_text=True)
    assert '<span class="num">2</span> record(s) written back' in page, \
        'the first batch was lost with the second'
    assert 'Session expired' in page
    assert 'not attempted' in page


def test_all_or_none_is_false_on_every_batch(client, monkeypatch, sent):
    """Stated in the body, not assumed from the default. Salesforce's default is false, but
    a caller that never says so is one release note away from losing 199 good records."""
    token, _ = board(client, monkeypatch, [scored_lead(i) for i in range(3)])
    client.post(f'/results/{token}/writeback')
    assert sent and all(body['allOrNone'] is False for body in sent)


def test_a_write_larger_than_one_batch_is_split(client, monkeypatch, sent):
    """SF_LEAD_LIMIT means a real pull never reaches 200 today, so the chunking is only
    reachable by patching the batch size down -- the same way the rate-limit test reaches
    its cap. Untested chunking is chunking that works until the pull cap is raised."""
    monkeypatch.setattr(app, 'SF_WRITE_BATCH', 2)
    token, _ = board(client, monkeypatch, [scored_lead(i) for i in range(5)])
    client.post(f'/results/{token}/writeback')
    assert [len(body['records']) for body in sent] == [2, 2, 1]


def test_writeback_shares_the_hourly_budget_with_the_pull(client, monkeypatch, sent):
    """Every batch is a real call against the same org the pull hits, so it spends from the
    same window -- checked before the call, so the cap bounds usage rather than describing
    it. Records past the cap are reported as not attempted, which is a different sentence
    from records that failed."""
    monkeypatch.setattr(app, 'SF_MAX_CALLS_PER_HOUR', 2)
    monkeypatch.setattr(app, 'SF_WRITE_BATCH', 2)
    token, _ = board(client, monkeypatch, [scored_lead(i) for i in range(5)])   # spends 1
    client.post(f'/results/{token}/writeback')
    assert [len(body['records']) for body in sent] == [2], 'the cap did not stop the write'
    page = client.get(f'/results/{token}').get_data(as_text=True)
    assert 'not attempted' in page


def test_the_write_stamps_scored_at_and_the_model_version(client, monkeypatch, sent):
    """The two fields that make a stored score interpretable later: when it was computed,
    and by which model. Scored_At is stamped at send time and is deliberately not part of
    the match comparison -- see _sf_verdict."""
    token, _ = board(client, monkeypatch, [scored_lead(1)])
    client.post(f'/results/{token}/writeback')
    record = sent[0]['records'][0]
    assert record['attributes'] == {'type': 'Lead'}
    assert record['LeadScorer_Model_Version__c'] == app.MODEL_VERSION
    assert record['LeadScorer_Scored_At__c'].endswith('+00:00')
    assert set(record) - {'attributes', 'Id'} <= set(app.SF_WRITE_FIELDS), \
        'writeback touched a field it does not own'


def test_a_row_that_could_not_be_scored_is_never_written(client, monkeypatch, sent):
    """No id, no write -- the batch rule score_row already enforces, carried through to the
    CRM. A row with no lead_id cannot be reconciled back to a record anyway."""
    nameless = scored_lead(1)
    nameless['Id'] = ''
    token, page = board(client, monkeypatch, [nameless] + [scored_lead(i) for i in range(2, 5)])
    client.post(f'/results/{token}/writeback')
    assert '' not in {r['Id'] for body in sent for r in body['records']}
    assert len({r['Id'] for body in sent for r in body['records']}) == 3


def test_a_pull_with_no_identity_refuses_to_write(client, monkeypatch, sent):
    """Without the integration user's own Id there is no way to tell this tool's edits from
    a person's, which makes the clobber check undecidable. An undecidable safety check is a
    reason not to write, not a reason to write carefully."""
    monkeypatch.setattr(app, '_sf_query_leads', lambda: ([scored_lead(1)], ORG, ''))
    posted = client.post('/rank/salesforce')
    token = posted.headers['Location'].rsplit('/', 1)[-1]
    page = client.get(posted.headers['Location']).get_data(as_text=True)
    assert 'Write-back is unavailable' in page
    client.post(f'/results/{token}/writeback')
    assert sent == []


def test_a_csv_board_offers_no_writeback(client):
    """Nothing to write back to. The panel is keyed off the payload carrying an org, not
    off a flag somebody could set on the wrong board."""
    posted = client.post('/rank/sample')
    page = client.get(posted.headers['Location']).get_data(as_text=True)
    # Asserted on the form action rather than on prose: the shared stylesheet mentions
    # write-back in a comment on every page, and a test that reads a CSS comment as a
    # feature is a test that passes for the wrong reason.
    assert '/writeback' not in page and 'would change' not in page
