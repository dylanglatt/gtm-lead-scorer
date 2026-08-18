"""The tests that hold the promises this tool makes.

    pip install -r requirements-dev.txt
    pytest

Each one is here because of something that went wrong, or something a reviewer is
entitled to check without reading every line:

  parity          the same lead, typed and uploaded, must score the same. It did not once.
  ingestion       one input row, one output row, from the fixture that is broken 19 ways.
  unknown values  lower confidence, never the score.
  a blank state   is the one blank that DOES move the score.
  junk            sinks. It must never rank.
  debug           stays off.
  vocabulary      tier name, action and colour all come from scorer.TIERS and nowhere else.

The messy fixture carries its own expectations column, so the fixture is the assertion
source: adding a row to it adds a test, and no expectation lives in two places.
"""
import csv
import io
import os
import re

import pytest

import app
import csv_io
import scorer

_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(_DIR, 'leads_messy_fixture.csv')
DEMO = os.path.join(_DIR, 'demo_leads.csv')


@pytest.fixture()
def client():
    return app.app.test_client()


def _rank(rows, header=None):
    """A list of dicts -> the bytes of a CSV -> ranked results, through the real upload
    path. Nothing here shortcuts read_leads: the point is to test the batch intake."""
    header = header or list(rows[0])
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header)
    w.writeheader()
    w.writerows(rows)
    leads, _report = app.read_leads(buf.getvalue().encode('utf-8'))
    return app.score_rows([lead for lead, _why in leads])


def _page_verdict(html):
    """(win %, tier key, confidence) as the single-lead page actually renders them."""
    score = re.search(r'id="verdict" data-score="(\d+)"', html)
    tier = re.search(r'class="pill big (\w+)" id="v-tier"', html)
    conf = re.search(r'<span class="pill conf">(\w+) confidence</span>', html)
    assert score and tier and conf, 'the verdict card did not render'
    return int(score.group(1)), tier.group(1), conf.group(1)


# ---------------------------------------------------------------------------
# 1. Form and batch parity.
#
# THE bug this is here for: state_missing was computed one way for a typed lead and
# another for an uploaded one, so the same record came back Warm from the form and Cold
# from the CSV. Both cases are exercised — a lead with a state and a lead without —
# because the blank one is the shape that broke.
# ---------------------------------------------------------------------------
PARITY_LEAD = {'channel': 'google', 'icp': 'High Value',
               'revenue': '$1,000,000 to $4,999,999', 'utm': 'brand',
               'legacy': '70', 'state': 'TX', 'lead_id': 'P-1'}


@pytest.mark.parametrize('state', ['TX', ''], ids=['state on file', 'no state'])
def test_the_same_lead_scores_the_same_typed_and_uploaded(client, state):
    typed = dict(PARITY_LEAD, state=state)
    html = client.get('/score', query_string=typed).get_data(as_text=True)
    form_pct, form_tier, form_conf = _page_verdict(html)

    # The upload carries the same values under the same short column names the form uses.
    uploaded = _rank([{'lead_id': typed['lead_id'], 'channel': typed['channel'],
                       'icp': typed['icp'], 'revenue': typed['revenue'],
                       'utm': typed['utm'], 'legacy': typed['legacy'],
                       'state': typed['state']}])
    assert len(uploaded) == 1
    row = uploaded[0]
    assert (form_pct, form_tier, form_conf) == (
        int(round(row['score'] * 100)), row['tier'], row['confidence'])


def test_one_lead_and_a_batch_of_one_are_the_same_call():
    """score_leads batches the model call; it must not change the answer."""
    lead = {'lead_id': 'P-2', 'channel': 'meta', 'icp_category': 'Low Value',
            'company_annual_revenue': 'Less than $250,000', 'utm_medium': 'prospecting',
            'legacy_score': '30', 'state': ''}
    assert scorer.score_lead(dict(lead)) == scorer.score_leads([dict(lead)])[0]


# ---------------------------------------------------------------------------
# 2. Messy CSV ingestion. The fixture is broken on purpose, one way per row, and its
# `expectations` column says what each row should do. Read it before changing this test.
# ---------------------------------------------------------------------------
def _fixture_rows():
    """The fixture's own rows, in file order, skipping the blank ones read_leads skips."""
    with open(FIXTURE, newline='', encoding='utf-8') as fh:
        rows = [r for r in csv.DictReader(fh) if any((v or '').strip() for v in r.values()
                                                     if isinstance(v, str))]
    return rows


def test_every_messy_row_produces_exactly_one_result():
    raw = open(FIXTURE, 'rb').read()
    leads, report = app.read_leads(raw)
    results = app.score_rows([lead for lead, _why in leads])
    expected = _fixture_rows()
    assert len(leads) == len(expected) == len(results), 'a row was dropped or invented'
    assert report['blank'] == 2, 'the two blank lines should be skipped, not scored'
    # Every id in the file comes back, duplicates included — collapsing them would lose
    # a lead, which is the one thing ingestion must never do.
    assert ([str(r.get('lead_id') or '') for r in results]
            == [(row['lead_id'] or '').strip() for row in expected])


def test_the_fixtures_expectations_column_holds():
    raw = open(FIXTURE, 'rb').read()
    leads, _report = app.read_leads(raw)
    results = app.score_rows([lead for lead, _why in leads])
    for r, (_lead, notes) in zip(results, leads):
        r['row_notes'] = notes

    checked = 0
    for row, r in zip(_fixture_rows(), results):
        expectation = (row.get('expectations') or '').strip()
        if not expectation:
            continue                      # M-17 is the short row: its cell is missing
        status, _, level = expectation.partition('|')
        where = f"{row['lead_id'] or '(no id)'} expects {expectation!r}"
        notes = ' · '.join(r.get('row_notes', []))
        if status == 'clean':
            assert not r.get('flags'), where
        elif status == 'normalized':
            assert r.get('normalized'), where
            assert not r.get('flags'), where
        elif status == 'flagged':
            assert r.get('flags'), where
        elif status == 'duplicate':
            assert 'duplicate lead_id' in notes, where
        elif status == 'unscored_no_id':
            assert r.get('error'), where
        elif status == 'ragged':
            assert 'cell(s)' in notes, where
        else:
            raise AssertionError(f'unknown expectation {status!r} on {row["lead_id"]}')
        if level:
            assert r['confidence'] == level.capitalize(), where
        checked += 1
    assert checked >= 17, 'the expectations column stopped being read'


def test_a_file_of_junk_still_answers():
    """The ENGINE answers: every row comes back carrying its reason, nothing crashes.

    The docstring used to say "not an empty page" as well, and that half is no longer
    true. This file measures 0.00 usable cells, so /rank now declines to draw a board out
    of these answers — test_schema_guard.py covers that, and
    test_the_engine_still_answers_for_a_file_the_route_declines pins the two halves
    together. Both are deliberate: the scorer's contract is that every row gets a result,
    and the product's contract is that a result built from nothing is not shown as a
    ranking. This test is about the first one, which has not changed.
    """
    results = _rank([{'lead_id': 'J-1', 'channel': '', 'icp': '', 'revenue': '',
                      'utm': '', 'legacy': 'inf', 'state': 'Banana'}])
    assert len(results) == 1
    assert not results[0].get('error')
    assert results[0]['confidence'] == 'Low'
    assert results[0]['flags']


# ---------------------------------------------------------------------------
# 3. Unknown values: confidence, not score.
# ---------------------------------------------------------------------------
BASE_LEAD = {'lead_id': 'U-1', 'channel': 'google', 'icp_category': 'High Value',
             'company_annual_revenue': '$1,000,000 to $4,999,999',
             'utm_medium': 'brand', 'legacy_score': '70', 'state': 'TX'}


def test_an_unrecognized_value_costs_confidence_but_not_the_score():
    known = scorer.score_lead(dict(BASE_LEAD))
    youtube = scorer.score_lead(dict(BASE_LEAD, channel='youtube'))
    vimeo = scorer.score_lead(dict(BASE_LEAD, channel='vimeo'))

    # The encoder is fit with handle_unknown='ignore', so an unseen value contributes
    # nothing at all — which means WHICH unseen value it is cannot matter.
    assert youtube['score'] == vimeo['score']
    assert youtube['confidence'] == 'Medium' and known['confidence'] == 'High'
    assert youtube['flags'] and not known['flags']


def test_a_recognized_value_does_move_the_score():
    """The other half of the claim above: the model is not ignoring the field itself."""
    google = scorer.score_lead(dict(BASE_LEAD))
    tiktok = scorer.score_lead(dict(BASE_LEAD, channel='tiktok'))
    assert google['score'] != tiktok['score']
    assert google['confidence'] == tiktok['confidence'] == 'High'


# ---------------------------------------------------------------------------
# 4. The one blank that is a signal.
# ---------------------------------------------------------------------------
def test_a_blank_state_moves_the_score_and_is_not_a_flag():
    on_file = scorer.score_lead(dict(BASE_LEAD, state='TX'))
    missing = scorer.score_lead(dict(BASE_LEAD, state=''))
    assert missing['score'] < on_file['score'], 'no state on file is a trained signal'
    # It costs no confidence: the model used it, it just used it against them.
    assert missing['confidence'] == on_file['confidence'] == 'High'
    assert not missing['flags']
    # And the why panel says so rather than hiding it.
    assert any(w['factor'] == 'State: missing' for w in missing['why'])


# ---------------------------------------------------------------------------
# 5. Junk sinks.
# ---------------------------------------------------------------------------
JUNK = {'channel': 'carrier pigeon', 'icp_category': 'Nonesuch',
        'company_annual_revenue': 'banana', 'utm_medium': '???',
        'legacy_score': '99999'}


def test_junk_lands_in_cold_at_low_confidence():
    blank = scorer.score_lead({'lead_id': 'J-blank'})
    garbage = scorer.score_lead(dict(JUNK, lead_id='J-garbage', state=''))
    for r in (blank, garbage):
        assert r['tier'] == 'cold'
        assert r['confidence'] == 'Low'


def test_junk_carrying_a_state_is_still_low_confidence_and_still_below_a_real_lead():
    """The honest edge of the rule above. A state that is PRESENT but not a state —
    'Atlantis' — leaves state_missing at 0, because the model was fit on whether a state
    was recorded, not on whether it was spelled correctly (scorer._unusable says so where
    it flags the value). So this row keeps the one signal the empty row loses, and it
    lands in the middle of the board rather than at the bottom of it.

    What must hold is the promise the tool actually makes: it is flagged, it is Low
    confidence, and it never outranks a lead the model could read."""
    garbage = scorer.score_lead(dict(JUNK, lead_id='J-atlantis', state='Atlantis'))
    good = scorer.score_lead(dict(BASE_LEAD))
    assert garbage['confidence'] == 'Low'
    assert garbage['tier'] in ('cool', 'cold')
    # An out-of-range prior score used to saturate the model and take the top of the
    # queue at High confidence. Ranked against a real lead, junk must come second.
    assert sorted([garbage, good], key=app.rank_key)[0] is good


# ---------------------------------------------------------------------------
# 6. The debugger stays off. debug=True serves Werkzeug's interactive console, which is
# a remote shell for anyone who can reach the port, and this app is deployed publicly.
# ---------------------------------------------------------------------------
def test_debug_is_off():
    assert app.app.debug is False
    # Read the code, not the comments — the comment above app.run() names debug=True in
    # order to explain why it is not used, and that must not read as a violation.
    source = open(os.path.join(_DIR, 'app.py'), encoding='utf-8').read()
    code = '\n'.join(line.split('#')[0] for line in source.splitlines())
    assert 'debug=False' in code
    assert 'debug=True' not in code


# ---------------------------------------------------------------------------
# 7. One tier vocabulary. A tier's name, its action line and its colour all hang off the
# same key in scorer.TIERS, and every surface reads from there.
# ---------------------------------------------------------------------------
def test_tier_name_action_and_colour_all_come_from_one_list():
    css = open(os.path.join(_DIR, 'templates', 'base.html'), encoding='utf-8').read()
    for tier in scorer.TIERS:
        key = tier['key']
        assert scorer.TIER[key] is tier
        assert tier['name'] and tier['action']
        for part in ('bg', 'fg', 'br'):
            assert f'--{key}-{part}:' in css, f'no {part} colour for the {key} tier'
    assert app.TIERS is scorer.TIERS
    assert scorer.CUT_KEYS == [t['key'] for t in scorer.TIERS][:-1]


def test_no_template_hardcodes_a_tier_name_or_action():
    """The words reach the page through the list or not at all, so renaming a tier in
    scorer.py renames it everywhere at once."""
    words = ([t['name'] for t in scorer.TIERS] + [t['action'] for t in scorer.TIERS])
    for name in sorted(os.listdir(os.path.join(_DIR, 'templates'))):
        text = open(os.path.join(_DIR, 'templates', name), encoding='utf-8').read()
        for word in words:
            assert word not in text, f'{name} spells out {word!r} instead of reading it'


def test_a_moved_cutoff_retiers_without_rescoring():
    """Re-tiering is tier_for over a stored score. Name and action must follow the key."""
    r = scorer.score_lead(dict(BASE_LEAD))
    colder = app._retier(r, {'hot': 0.99, 'warm': 0.98, 'cool': 0.97})
    assert colder['score'] == r['score']
    assert colder['tier'] == 'cold'
    assert colder['tier_name'] == scorer.TIER['cold']['name']
    assert colder['action'] == scorer.TIER['cold']['action']


# ---------------------------------------------------------------------------
# 8. The deployed surface: the health check, the upload limit, and the two couplings
# between the shipped app and the scripts that built its data.
# ---------------------------------------------------------------------------
def test_healthz_is_cheap_and_says_ok(client):
    r = client.get('/healthz')
    assert r.status_code == 200
    assert r.get_data(as_text=True) == 'ok'


def test_an_oversized_upload_is_a_sentence_not_a_stack_trace(client):
    limit = app.app.config['MAX_CONTENT_LENGTH']
    big = b'lead_id,channel\n' + b'X-1,google\n' * (limit // 10)
    assert len(big) > limit
    r = client.post('/rank', data={'csv': (io.BytesIO(big), 'huge.csv')},
                    content_type='multipart/form-data')
    assert r.status_code == 413
    body = r.get_data(as_text=True)
    assert 'too large' in body and 'Traceback' not in body


def test_the_training_prep_matches_the_scorers_prep():
    """train_and_save.prep is a second copy of scorer._prep_dict, written out so that
    fitting a model does not require a fitted model to import. This is what stops the two
    from drifting: a lead prepared for training and the same lead prepared for scoring
    have to be the same row."""
    import pandas as pd

    import train_and_save

    rows = [dict(BASE_LEAD, time_zone='Central'),
            dict(BASE_LEAD, state='', time_zone='', legacy_score=''),
            dict(BASE_LEAD, utm_medium='BRAND ', company_annual_revenue='',
                 icp_category='Unknown', legacy_score='99999')]
    df = pd.DataFrame([{k: r.get(k, '') for k in
                        ['channel', 'icp_category', 'company_annual_revenue',
                         'utm_medium', 'time_zone', 'legacy_score', 'state']}
                       for r in rows])
    trained = train_and_save.prep(df, scorer.LEG_MED)
    for i, row in enumerate(rows):
        scored = scorer._prep_dict(row)
        for col in train_and_save.FEATURES:
            assert trained[col].iloc[i] == scored[col], f'{col} differs on row {i}'


def test_the_generators_time_zones_agree_with_the_app():
    """The generator writes a time_zone into the training data; the app derives one from
    the state a rep types. If those two tables disagree, the model is fit on one zone and
    scored on another."""
    import make_synthetic_data

    for code, zone, _weight in make_synthetic_data.STATE_POOL:
        assert scorer.STATE_TZ[code] == zone


def test_every_offered_option_is_a_level_the_model_knows():
    """A dropdown must not suggest a value that flags the moment it is picked. The one
    exception is documented in app.OFFERED: no training lead was over $25m, so the top
    revenue band is offered anyway and folded down by scorer.REVENUE_ALIASES."""
    documented = {('company_annual_revenue', '$25,000,000 and greater')}
    unfitted = {(field, value) for field, values in app.OFFERED.items()
                for value in values if value not in scorer.KNOWN.get(field, set())}
    assert unfitted == documented


def test_the_demo_file_ranks_and_spreads(client):
    """The file the README tells a stranger to try first. It has to rank without a single
    failure, and it has to produce a queue that is actually prioritized — a board where
    most of the list is Hot is not a triage tool."""
    results = app.score_rows(list(csv.DictReader(open(DEMO, newline='', encoding='utf-8'))))
    assert len(results) == 250
    assert not any(r.get('error') for r in results)
    hot = [r for r in results if r['tier'] == 'hot']
    assert 0.05 <= len(hot) / len(results) <= 0.18, 'the Hot tier is not a short list'
    assert len({r['tier'] for r in results}) == 4, 'every tier should be represented'
    assert max(r['score'] for r in results) < 0.85, 'no inbound lead is a 90% certainty'
