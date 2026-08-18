"""What the tool DOES about a file that does not match the model.

test_schema_match.py pins the measure; this pins the response, and the response is the
whole point. The measure existed implicitly before — every row already carried its flags —
and the tool still handed back a confident board for a file it could barely read. These
tests go through /rank, because the decision is a property of the product, not of the
scoring engine: the engine's contract is unchanged and still answers for every row.

Three outcomes, one of which is a refusal. The refusal is the feature.
"""
import io
import os

import pytest

import app
import scorer

_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(_DIR, 'tests', 'fixtures')


@pytest.fixture()
def client():
    return app.app.test_client()


def upload(client, raw, name='upload.csv'):
    """A file through the real upload path, ending on the page a user would be looking at."""
    posted = client.post('/rank', data={'csv': (io.BytesIO(raw), name)},
                         content_type='multipart/form-data')
    assert posted.status_code == 303, 'upload did not Post/Redirect/Get'
    page = client.get(posted.headers['Location'])
    assert page.status_code == 200
    return page.get_data(as_text=True)


def fixture(name):
    with open(os.path.join(FIXTURES, name), 'rb') as fh:
        return fh.read()


def repo_file(name):
    with open(os.path.join(_DIR, name), 'rb') as fh:
        return fh.read()


def has_board(html):
    return '<table>' in html


def has_notice(html):
    # The rendered element, not the stylesheet rule of the same name, which is on every
    # page. The first version of this helper matched the CSS and passed everywhere.
    return 'class="runsum matchnote"' in html


# ---------------------------------------------------------------------------
# Files that match. Neither may gain a banner.
# ---------------------------------------------------------------------------
def test_the_demo_file_ranks_with_nothing_new_on_the_page(client):
    html = upload(client, repo_file('demo_leads.csv'))
    assert has_board(html)
    assert not has_notice(html)


def test_the_messy_fixture_still_ranks_and_keeps_every_row(client):
    """The hard requirement, at the route this time. Nineteen rows in, nineteen out, no
    banner: row-level mess is not a schema mismatch and must not be treated as one."""
    html = upload(client, repo_file('leads_messy_fixture.csv'))
    assert has_board(html)
    assert not has_notice(html)
    assert '<span class="num">19</span> leads, ranked' in html


# ---------------------------------------------------------------------------
# The middle band: rank it, and say the order is rough.
# ---------------------------------------------------------------------------
def test_a_renamed_column_ranks_with_a_notice_that_names_the_column(client):
    html = upload(client, fixture('hearth_repro.csv'))
    assert has_board(html), 'a file this close to right should still be ranked'
    assert has_notice(html)
    assert 'contractor_annual_revenue' in html, 'the notice must name the ignored column'
    assert 'Company revenue' in html, 'and the field it could not fill'
    # One unplaceable column and one unfillable field is a rename, offered as a question.
    assert 'rename it to' in html and 'company_annual_revenue' in html
    assert 'High confidence' in html


def test_the_notice_survives_a_reload_of_the_stored_board(client):
    """The banner is a property of the file, so it belongs in the stored payload and has
    to come back on a plain GET — not be a one-off rendered at upload time."""
    posted = client.post('/rank', data={'csv': (io.BytesIO(fixture('hearth_repro.csv')),
                                                'h.csv')},
                         content_type='multipart/form-data')
    location = posted.headers['Location']
    assert has_notice(client.get(location).get_data(as_text=True))
    assert has_notice(client.get(location).get_data(as_text=True))


# ---------------------------------------------------------------------------
# The refusal.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('name', ['foreign_vocabulary.csv', 'half_familiar.csv',
                                  'all_blank_values.csv'])
def test_a_file_the_model_cannot_read_is_not_ranked(client, name):
    html = upload(client, fixture(name))
    assert not has_board(html), 'a board was drawn for a file the model could not read'
    assert 'not ranked' in html
    # A dead end is not a refusal. It has to say what would have worked, and it has to
    # get those names from the scorer rather than from a copy written into the template.
    for field in scorer.KEY_FIELDS:
        assert field in html, f'the refusal should name {field}'
    assert 'Score the sample file' in html


@pytest.mark.parametrize('raw,why', [
    (b'widget_id,colour,sprocket\nW-1,red,12\n', 'no recognizable columns'),
    (b'', 'an empty file'),
    (b'lead_id,channel\n', 'a header with no rows'),
])
def test_every_refusal_offers_the_sample_file(client, raw, why):
    """The way out has to be on ALL of them, not just the one that measures coverage.

    csv_io.read_leads refuses three files at the file level before the match check ever
    runs, and the sample button used to hang off the schema detail — which only the
    vocabulary-mismatch refusal carries. So these three ended at a dead end with nothing
    to click, which is the state most likely to make somebody close the tab."""
    html = upload(client, raw)
    assert not has_board(html), f'{why} should not produce a board'
    assert 'Could not read that CSV' in html, f'{why} lost its message'
    assert 'Score the sample file' in html, f'{why} left the visitor with nowhere to go'
    assert '/rank/sample' in html


def test_the_refusal_offers_the_sample_file_as_a_way_out(client):
    html = upload(client, fixture('foreign_vocabulary.csv'))
    assert app.url_for.__name__  # sanity: url_for is what the template used
    assert '/rank/sample' in html


def test_a_refused_file_still_accounts_for_every_row_it_read(client):
    """A refusal withholds the RANKING. It may not withhold the rows.

    score_rows promises that a row it could not score still comes back carrying its
    reason, and the first version of this feature broke that promise one level up: the
    queue was discarded on refusal, so a file of 100 rows with 99 missing ids reported
    "25% across your 1 leads" and never mentioned the other 99 at all. The page became the
    one place a lead could disappear.

    The counts come from the same _summarize the ranked board uses, so there is no second
    set of numbers to drift."""
    rows = ['ID-%d,google' % i for i in range(1, 6)] + [' ,google'] * 95
    html = upload(client, ('lead_id,channel\n' + '\n'.join(rows)).encode())

    assert not has_board(html), 'the ranking is still withheld'
    assert 'not ranked' in html
    # The true shape of the upload, not the shape of the part that happened to score.
    assert '<span class="num">100</span> rows read' in html
    assert '<span class="num">5</span> scored' in html
    assert '<span class="num">95</span> could not be scored' in html
    # And why, in the words the run summary already uses, plus the rows themselves.
    assert 'missing lead ID' in html
    assert 'no lead_id, so this row could not be scored' in html
    # The coverage sentence must not pass the scored count off as the file.
    assert 'across your 5 leads' not in html
    assert 'it could score at all' in html


def test_a_file_with_no_scorable_rows_is_refused_not_divided_by_zero(client):
    html = upload(client, b'lead_id,channel,icp_category\n,google,Ideal\n,google,Ideal\n')
    assert not has_board(html)
    assert 'not ranked' in html


def test_an_unreadable_file_still_fails_the_way_it_always_did(client):
    """The refusal is a new sibling of this one, not a replacement. A file with no
    recognizable columns at all is caught earlier, by csv_io, and keeps its own wording."""
    html = upload(client, b'widget_id,colour,sprocket\nW-1,red,12\n')
    assert not has_board(html)
    assert 'Could not read that CSV' in html
    assert 'no recognizable columns' in html


# ---------------------------------------------------------------------------
# The engine is untouched. This is the intent drift made explicit.
# ---------------------------------------------------------------------------
def test_the_engine_still_answers_for_a_file_the_route_declines():
    """test_scorer.test_a_file_of_junk_still_answers asserts every row comes back carrying
    its reason, and it still does — that test calls read_leads and score_rows directly.
    What changed is one level up: the ROUTE will not draw a board out of those answers.
    Both are true at once, and this is where they are pinned together."""
    leads, _report = app.read_leads(fixture('foreign_vocabulary.csv'))
    results = app.score_rows([lead for lead, _why in leads])
    assert len(results) == 5
    assert all(r.get('score') is not None for r in results)
    assert all(r['confidence'] == 'Low' for r in results)


# ---------------------------------------------------------------------------
# The row cap.
# ---------------------------------------------------------------------------
def test_a_file_with_too_many_rows_is_refused_before_it_is_scored(client, monkeypatch):
    """Nothing bounded row count before this: the 10MB body limit allows well over 100k
    leads, and all of them would be scored, sorted, stored and rendered. Tested against a
    lowered cap rather than by building a 100k-row file, since the cap is the behaviour."""
    monkeypatch.setattr(app, 'MAX_ROWS', 2)
    html = upload(client, repo_file('demo_leads.csv'))
    assert not has_board(html)
    assert 'ranks up to 2 at a time' in html
    assert 'Score the sample file' in html


# ---------------------------------------------------------------------------
# One click to a real board.
# ---------------------------------------------------------------------------
def test_the_sample_button_produces_the_same_board_as_uploading_the_file(client):
    """The sample route and an upload of the same bytes go through one function, and this
    is what says so. If they ever diverge, the button is showing something the file does
    not, which is worse than not having the button."""
    uploaded = upload(client, repo_file(app.SAMPLE_FILE))
    posted = client.post('/rank/sample')
    assert posted.status_code == 303
    sampled = client.get(posted.headers['Location']).get_data(as_text=True)

    def board(html):
        start = html.index('<table>')
        return html[start:html.index('</table>', start)]
    assert board(sampled) == board(uploaded)
    assert not has_notice(sampled)
