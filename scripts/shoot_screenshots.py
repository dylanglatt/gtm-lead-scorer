"""Regenerates the screenshots in docs/, so they are reproducible rather than
hand-captured and cannot quietly go stale when the data or the UI changes.

    pip install -r requirements-dev.txt
    python -m playwright install chromium
    python scripts/shoot_screenshots.py

It starts the real app on a free port, drives a headless Chromium at the same 1400px
viewport the existing images use, and writes:

  docs/board.png    demo_leads.csv uploaded and ranked. THE hero image, so it has to show
                    a queue that is actually prioritized: the tier chips carry the counts
                    for the whole board and the frame stops after the top of the queue.
  docs/verdict.png  one lead with the full why panel — the lead the board just put first,
                    looked up from the file by the id on screen, so the two images are
                    always the same run of the same data.
  docs/mismatch.png the same board for a file whose schema the model only half matches,
                    framed on what the tool says about it rather than on the ranking.

All three are CLIPPED, not full-page. A full-page shot of 250 ranked leads is a thumbnail
of a table nobody can read; the frame is cut at a row boundary instead, which is what makes
the top of the queue legible at the size GitHub renders it."""
import csv
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
DOCS = os.path.join(ROOT, 'docs')
DEMO = os.path.join(ROOT, 'demo_leads.csv')
# A file the model can only half read: correct in every way except that its revenue column
# is named contractor_annual_revenue, so that column is ignored and every lead scores one
# field short. The shot of what the tool says about it is the point of the third image.
MISMATCH = os.path.join(ROOT, 'tests', 'fixtures', 'hearth_repro.csv')

VIEWPORT = 1400
ROWS_PAST_HOT = 5           # how far past the last Hot lead the hero image keeps going

# The CSV's column names, in the short form the single-lead form posts. FIELDS in app.py
# is the source of both spellings; these are the ones that show up in the address bar.
PARAMS = [('channel', 'channel'), ('icp_category', 'icp'),
          ('company_annual_revenue', 'revenue'), ('utm_medium', 'utm'),
          ('legacy_score', 'legacy'), ('state', 'state'), ('lead_id', 'lead_id')]


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _wait_for_health(base, proc, timeout=60):
    """/healthz exists for the host; it works just as well here. Polls until the model
    has finished loading, which is the slow part of starting this app."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f'the app exited before it served anything '
                             f'(status {proc.returncode})')
        try:
            with urllib.request.urlopen(base + '/healthz', timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.25)
    raise SystemExit(f'{base}/healthz never answered')


def _clip_to(page, selector, index=None, pad=0):
    """A clip rectangle from the top of the page to the bottom of one element.

    Paired with full_page=True at every call site, because a clip is otherwise capped at
    the viewport and the frame would land wherever 1000px happens to fall — mid-row.

    `pad` defaults to none: the board has to cut exactly at a row boundary, and a few
    pixels more let the top of the next row peek in, which reads as a mistake. The verdict
    card asks for a little air underneath instead."""
    locator = page.locator(selector)
    box = (locator.nth(index) if index is not None else locator.last).bounding_box()
    assert box, f'{selector} is not on the page'
    return {'x': 0, 'y': 0, 'width': VIEWPORT, 'height': int(box['y'] + box['height'] + pad)}


def shoot(page, base):
    os.makedirs(DOCS, exist_ok=True)

    # --- the board -------------------------------------------------------------
    page.goto(base + '/', wait_until='networkidle')
    page.locator('#csvfile').set_input_files(DEMO)     # fires change, which enables Rank
    page.locator('#ranklist').click()
    page.wait_for_url(f'{base}/results/**')
    page.wait_for_selector('table tbody tr')

    rows = page.locator('table tbody tr')
    total = rows.count()
    # Frame a few rows PAST the last Hot lead rather than a fixed number of rows. The
    # point of the image is that the queue is triaged, and the clearest evidence of that
    # is the boundary itself: the badge changing from Hot to Warm partway down the list,
    # a few rows below the tier chips that say how many of each there are. A fixed cut
    # would show that only by luck, and would stop showing it the moment the data moved.
    hot = page.locator('table tbody tr .pill.hot').count()
    last = min(hot + ROWS_PAST_HOT, total) - 1

    board = os.path.join(DOCS, 'board.png')
    page.screenshot(path=board, full_page=True,
                    clip=_clip_to(page, 'table tbody tr', last))

    top_id = rows.nth(0).locator('td b').inner_text().strip()
    counts = page.locator('.chips').first.inner_text().replace('\n', ' ')
    print(f'docs/board.png   {total} rows on the page, framed to {last + 1} '
          f'({hot} Hot + {ROWS_PAST_HOT})')
    print(f'                 {counts}')

    # --- the verdict -----------------------------------------------------------
    # The same lead the board just ranked first, so the two images tell one story. Its
    # values come from the file, not from a hand-written URL that could drift from it.
    with open(DEMO, newline='', encoding='utf-8') as fh:
        lead = next(r for r in csv.DictReader(fh) if r['lead_id'] == top_id)
    query = {param: (lead.get(column) or '') for column, param in PARAMS}
    query['go'] = '1'
    # time_zone is left out on purpose: the form derives it from the state, and the shot
    # should show that happening — it is labelled "(from state)" in the input column.
    page.goto(base + '/score?' + '&'.join(f'{k}={urllib.parse.quote(v)}'
                                          for k, v in query.items()),
              wait_until='networkidle')
    page.wait_for_selector('#verdict')

    verdict = os.path.join(DOCS, 'verdict.png')
    # The last card on this page is the why panel; the intake form below it is a
    # <details>, not a card, so this frames the verdict and the whole explanation.
    page.screenshot(path=verdict, full_page=True, clip=_clip_to(page, '.card', pad=16))
    score = page.locator('#verdict').get_attribute('data-score')
    tier = page.locator('#v-tier').inner_text()
    print(f'docs/verdict.png {top_id}: {score}% win chance, {tier}')

    # --- the mismatch notice ---------------------------------------------------
    page.goto(base + '/', wait_until='networkidle')
    page.locator('#csvfile').set_input_files(MISMATCH)
    page.locator('#ranklist').click()
    page.wait_for_url(f'{base}/results/**')
    page.wait_for_selector('.matchnote')
    mismatch = os.path.join(DOCS, 'mismatch.png')
    # Only a few rows: the subject is the banner, and a long queue under it would read as
    # the subject instead. Enough of the board to show it did still rank.
    page.screenshot(path=mismatch, full_page=True,
                    clip=_clip_to(page, 'table tbody tr', 5))
    said = page.locator('.matchnote').inner_text().split('\n')[0]
    print(f'docs/mismatch.png {said}')


def main():
    port = _free_port()
    base = f'http://127.0.0.1:{port}'
    env = dict(os.environ, PORT=str(port), HOST='127.0.0.1')
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, 'app.py')],
                            cwd=ROOT, env=env)
    try:
        _wait_for_health(base, proc)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            # No device_scale_factor: the committed images are 1400px wide at 1x, and a
            # retina shot would quadruple the file size of something GitHub renders small.
            page = browser.new_page(viewport={'width': VIEWPORT, 'height': 1000},
                                    color_scheme='light')
            shoot(page, base)
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == '__main__':
    main()
