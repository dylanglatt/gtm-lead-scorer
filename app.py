"""GTM Lead Scorer — the web tool. Run: python app.py -> http://127.0.0.1:5000

PRESENTATION ONLY. Every number and message on the page comes from scorer.score_lead();
this file decides how it looks, never what it says. Tier names, actions and colour keys
come from scorer.TIERS and are never restated here.

THE THREE PATHS THROUGH THIS FILE

  One typed lead                          GET /score          -> score()      ~line 916
    read the form params
    time_zone_for(state)   derive the time zone; the rep is not asked for one
    scorer.score_lead      normalize -> score -> tier -> confidence -> why
    _ctx + PAGE            render the verdict card

  A CSV of leads                          POST /rank          -> rank()       ~line 1035
    read_leads             bytes -> per-lead dicts. Survives anything short of an
                           unreadable file; every mess is a per-row note        ~line 970
    scorer.score_lead      per row, inside a try/except: a row can never kill the run
    _summarize             N in / scored / flagged by kind / confidence split  ~line 1007
    stash in _RESULTS, redirect to GET /results/<token>  (Post/Redirect/Get)
    results()              re-tier vs APPLIED, filter, paginate, render        ~line 1114

  A lead (or batch) from another system   POST /api/score     -> api_score()
    _api_map_lead           JSON keys -> scorer fields, via the SAME HEADER_MAP the CSV
                            path uses — a Clay/HubSpot/Salesforce-shaped payload just works
    score_row / score_rows  same batch entry point rank() feeds, same APPLIED cutoffs
    JSON out, no page, no token — built for a workflow step to call inline

  Manager · Calibration                   GET /calibrate      -> calibrate()  ~line 1158
    the only place cutoffs change. Apply writes APPLIED; nothing is re-scored.

  Pipeline health                         GET /health         -> health()
    NOT an intake and not /healthz. Every path above writes one row per run through
    _summarize (see _record_run); this reads them back per source and flags a run whose
    schema fingerprint moved, or whose coverage crossed below MATCH_NOTICE, since the run
    before it. The store is runs.py and is absent unless RUN_HISTORY_DB is set.

WHERE THINGS LIVE
  FIELDS        ~63    every field: label, scorer name, URL param, combo-box options.
                      Also drives the CSV header map — a field hidden from the FORM must
                      stay in this list or the batch path stops reading its column.
  OFFERED       ~101   what the combo boxes SUGGEST. Curated by hand; not what's accepted.
  APPLIED        ~26   the team-wide cutoffs in force. Only Manager · Calibration writes it.
  PAGE          ~152   the entire UI: one Jinja template, styles included.
  _RESULTS      ~149   ranked boards held between the POST and the GET, newest 64.

Two names you will see everywhere in the template:
  one   the values the rep typed, keyed by URL param — repopulates the form and the
        "what you gave it" column. Also carries the derived time zone.
  qv    the ranked-list view model: rows for THIS page, chips, counts, pager, links.

Grep '# DEMO:' for the spots most likely to be hit live."""
from flask import Flask, request, render_template, redirect, url_for, Response
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
import io, os, csv, math, re, json, secrets, collections, logging, traceback, threading, time
import datetime, hashlib
import urllib.request, urllib.parse, urllib.error
import scorer

# The parts that need no request and no app. Imported rather than defined here so this file
# is the routing layer and little else; the names are re-exported below, because the tests
# and the templates reach several of them through `app.`.
import csv_io, flags, format as fmt, runs
from csv_io import _norm_header, _decode, _dialect, _rows
from flags import _KINDS, _flag_kind, _MISSING_FLAGS, BAD, MISSING, \
                  _flag_severity, _worst_severity, _row_flags
from format import _title, _rate_pct, _pct, _why_rows, _unused_rows, _usable, \
                  WHY_KEY, SCORE_FIELDS_TOTAL

# csv's default field cap is 128KB, and it counts an UNTERMINATED QUOTE as one enormous
# field — the quote swallows the rest of the file. That is a plausible real export, not a
# synthetic edge case, and it used to raise _csv.Error out of read_leads and escape the
# route as a 500. Raise the cap to something a real CSV can reach, and read_leads catches
# csv.Error on top of this so a pathological file still fails as a message, never a crash.
csv.field_size_limit(10_000_000)

app=Flask(__name__)

# 10MB. A real CRM export of 50,000 leads is a few megabytes, so this is generous for the
# job and still small enough that a public URL cannot be filled up by whatever somebody
# decides to drag onto the page. Werkzeug enforces it while reading the request, so the
# bytes never land anywhere; without it the upload is unbounded. The handler further down
# turns the refusal into a sentence — see _too_big.
app.config['MAX_CONTENT_LENGTH']=10*1024*1024

TIERS=scorer.TIERS                  # [{key,name,action}] hottest -> coldest. THE vocabulary.
CUT_KEYS=scorer.CUT_KEYS            # the three movable boundaries: hot, warm, cool
CONF_KEYS=[c.lower() for c in scorer.CONF_LEVELS]
PAGE_SIZES=[25,50,100]
PAGE_SIZE=50                        # default rows per page; the rep can pick from PAGE_SIZES

# ---------------------------------------------------------------------------
# HOW WELL AN UPLOAD MATCHES THE MODEL, and what to do when it does not.
#
# scorer.normalize_category states the rule this file needs: flag what we do not know,
# never guess. That rule runs per VALUE. It was missing per FILE, and the gap showed:
# an export whose revenue column is named contractor_annual_revenue has that column
# ignored by the header map, so every row is missing one field, every row still scores,
# and the board comes back tiered and confident with nothing on the page to say why the
# top lead is a 51%. handle_unknown='ignore' means an unfamiliar value contributes zero
# rather than raising — deliberate, and documented in scorer.py — but zero times a whole
# column is a board built out of the intercept.
#
# The measure is USABLE CELLS: over the rows that scored, how many of the four
# scorer.KEY_FIELDS the model could actually read, out of all of them.
#
#   coverage = usable cells / (scored rows x len(scorer.KEY_FIELDS))
#
# A cell is unusable when the column is absent, the value is blank, or the value is not
# one the model was fit on. That is exactly what scorer._unusable already decides per row,
# so this counts what the scorer already said rather than deciding it a second time.
#
# THE THING THAT MAKES IT WORK is counting an ABSENT COLUMN as unusable on every row. The
# obvious metric — what share of the values we did see were recognized — cannot see a
# missing column at all, because a column that is not there contributes no values to
# judge. On the export above that metric reads 0.879 and calls the file fine. This one
# reads 0.75 and says the revenue column was not read.
#
# Values pooled by the encoder's min_frequency=20 count as USABLE. Pooling is not
# dropping: those values share one fitted coefficient, so they carry a real if coarse
# signal, unlike an unknown value which contributes literally nothing. Counting them
# against a file would penalise it for containing exactly the CRM artefacts the model was
# trained on — it drops a correctly-mapped export from 0.99 to 0.89 for no reason.
MATCH_REFUSE=0.35   # below this the model is ranking its own intercept, so do not rank at
                    # all. Measured: a file in a foreign vocabulary scores 0.00, one with
                    # a single usable field 0.25, and nothing real lands in 0.25-0.75.
MATCH_NOTICE=0.80   # below this, still rank, but say the order is rough. Measured:
                    # demo_leads.csv 0.98, leads_messy_fixture.csv 0.89 — the file that is
                    # broken nineteen ways on purpose keeps its board, with room to spare.

# Nothing bounded row count before this: MAX_CONTENT_LENGTH caps the BYTES at 10MB, which
# is somewhere north of 100k lead rows, and every one of them is scored, sorted, held in
# _RESULTS and rendered. The cap is generous for the job and refuses in the same plain
# sentence the size limit uses, rather than by running out of memory in front of a visitor.
MAX_ROWS=100_000

# A refused file still has to account for every row it read. Listing them is the proof
# nothing was quietly dropped, but a 100k-row file's worth of them is not a page — so the
# rows are listed up to here and counted past it. The COUNTS are always complete.
DECLINED_SHOWN=200

# /api/score per-request cap. Generous for a Clay enrichment step or a Zapier batch step
# (both call per-record or in small batches), small enough that one request can't hold the
# process the way a 100k-row CSV upload is allowed to (that path streams to a page over
# several seconds; this one is meant to return inside a workflow step's own timeout).
API_MAX_LEADS=2000

# The file the "score the sample" button runs. Named here rather than in the template so
# the button and the README point at the same thing.
SAMPLE_FILE='demo_leads.csv'

# The team-wide tier cutoffs. Defaults come from meta.json via scorer; a manager moves
# them in Manager · Calibration and every view reads from here, so one setting drives the
# board and the single-lead verdict alike. Module-level state like _RESULTS below, and
# team-wide means exactly that: this is one process serving a public demo, so a cutoff one
# visitor applies is the cutoff the next visitor sees, and a restart puts it back to the
# meta.json defaults. That is the right shape for a shared setting a manager owns and the
# wrong shape for per-user preferences, which is why nothing else lives up here.
APPLIED={'cut':dict(scorer.DEFAULT_CUTOFFS)}
# Read by: results() (re-tiers the board), _ctx (the verdict), calibrate(). Written by
# exactly one function, apply_cutoffs(). Anything scoring OUTSIDE a request — the test
# suite, a script — deliberately ignores it and reads scorer.DEFAULT_CUTOFFS instead, so a
# number produced off-request cannot depend on what somebody dragged a slider to.

# Short column heading per score-driving field, for the "why" table.
BASE_PCT=round(scorer.BASE*100,1)  # read from meta, never hardcoded

# Which fitted model produced a number, short enough for a CRM text field. Written onto
# every Lead this tool scores back into Salesforce, because a score is only comparable to
# another score from the same model, and a CRM keeps numbers long after anybody remembers
# which run made them.
#
# DERIVED from the artifact, not declared in meta.json. A hand-maintained version string
# is a thing somebody forgets to bump on the one retrain where it mattered; a content hash
# cannot be forgotten, and it moves when and only when model.joblib moves — which is
# exactly the question "are these two scores comparable" is asking. Hashed once at import
# alongside the model itself: the file cannot change under a running process.
def _model_version():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'model.joblib'), 'rb') as fh:
            return 'model-'+hashlib.sha256(fh.read()).hexdigest()[:12]
    except Exception:
        # A missing artifact is already E500 at import and this process is dead either
        # way. This exists so the version string can never be the thing that kills it.
        return 'model-unknown'
MODEL_VERSION=_model_version()

# friendly label, scorer field name, URL param, example.
# The single-lead form is a GET, so the param is what shows up in the address bar —
# keep them short and legible. scorer.py still sees its own field names, unchanged.
# Every field the tool knows, in one list.
#   label  what a rep sees — plain words, no jargon
#   field  the name scorer expects
#   param  the URL/query name (the single-lead form is a GET, so this shows in the bar)
#   eg     placeholder text
#   opts   which vocabulary fills the combo box, or None for free text
#   form   False = still read from an uploaded CSV, just not typed on the form
# The CSV header map is built from this list too, so a field dropped from the FORM must
# stay in FIELDS or the batch path would stop recognizing its column.
FIELDS=[dict(label='Source',         field='channel',                   param='channel',
             eg='google',                        opts='channel'),
        dict(label='Fit',            field='icp_category',              param='icp',
             eg='High Value',                    opts='icp_category'),
        dict(label='Company revenue',field='company_annual_revenue', param='revenue',
             eg='$1,000,000 to $4,999,999',      opts='company_annual_revenue'),
        dict(label='Marketing channel', field='utm_medium',             param='utm',
             eg='brand',                         opts='utm_medium'),
        dict(label='Prior score',    field='legacy_score',              param='legacy',
             eg='55',                            opts=None, numeric=True),
        dict(label='State',          field='state',                     param='state',
             eg='TX',                            opts='state'),
        # Time zone is worked out from State on the form, so a rep never types it. It
        # stays here because an uploaded CSV still supplies its own time_zone column.
        dict(label='Time zone',      field='time_zone',                 param='tz',
             eg='Eastern',                       opts='time_zone', form=False)]
for _f in FIELDS: _f.setdefault('form',True); _f.setdefault('numeric',False)
FORM_FIELDS=[f for f in FIELDS if f['form']]
SCORE_PARAMS=[f['param'] for f in FIELDS]+['lead_id']
FIELD_NAMES={f['field'] for f in FIELDS}

# Combo-box vocabularies. On the FORM these are now the whole menu: the five categorical
# fields are searchable single-selects, so a rep picks a listed value or picks
# "Unknown / Other", and cannot commit a free-text one. Nothing about ACCEPTANCE changed —
# the CSV batch path is untouched and still routes every messy value it meets through
# scorer.normalize_category, so off-list spellings degrade exactly as they always did.
STATES=['AL','AK','AZ','AR','CA','CO','CT','DE','DC','FL','GA','HI','ID','IL','IN','IA',
        'KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM',
        'NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA',
        'WV','WI','WY']

# What each combo box OFFERS. Curated by hand, not scraped from the data: the training
# vocabulary is full of data-quality artefacts ('expired or invalid attributer license',
# 'rd', '--', 'Notify Admin') that are real rows in a CRM export but nonsense to offer a
# rep as a choice. Offering is not accepting — anything typed still goes through
# scorer.normalize_category, so an excluded value like 'rd' still scores exactly as it
# always did. This list only decides what the dropdown suggests.
#
# Every value here must exist in the model's fitted vocabulary, or we'd suggest something
# that immediately flags. test_offered_options_are_all_real_levels pins that.
OFFERED={
  'channel':['google','meta','bing','tiktok'],
  'icp_category':['High Value','Ideal','Low Value','Unknown'],
  # Bands in money order, not alphabetical — $10m must not sort next to $1m.
  # 'Self-Serve Signup' is excluded: it is a product, not a revenue band.
  # Filtered to the bands the model was actually fit on: no lead in the training data
  # was over $25m, so offering '$25,000,000 and greater' would flag the moment a rep
  # picked it. Typing a number that large still buckets — it just isn't suggested.
  # KNOWN EXCEPTION: '$25,000,000 and greater' is offered even though the model was
  # never fit on it — no training lead was that large. Picking it flags the lead and
  # drops confidence one level, exactly as typing "30M" already did. Offered anyway so
  # the list isn't silently missing the top of the scale.
  'company_annual_revenue':[b for _,_,b in scorer.REVENUE_BANDS],
  # Excluded: 'expired or invalid attributer license' and 'rd' (tracking artefacts),
  # 'other campaigns' (a catch-all bucket), and the '..._lead_generation' variants
  # (machine-written duplicates of prospecting / retargeting).
  'utm_medium':['brand','nonbrand','paid search','organic search','organic social',
                'prospecting','retargeting','cpc','ppc','pmax'],
  # Not rendered today — time zone is derived from state — but curated so re-enabling
  # the field can't put '--' or 'Notify Admin' in front of a rep.
  'time_zone':['Eastern','Central','Mountain','Arizona','Pacific','Alaska','Hawaii'],
}
OPTIONS=dict(OFFERED, state=STATES)

# State -> time zone now lives in scorer.py, because BOTH intake paths need it: a typed
# lead and an uploaded row must reach the same zone from the same state. scorer applies it
# inside normalize_lead, so there is no call site here that could drift. Re-exported under
# the old names so everything downstream — and the tests — keep one spelling.
STATE_TZ=scorer.STATE_TZ
STATE_NAMES=scorer.STATE_NAMES
STATE_CODES=scorer.STATE_CODES
normalize_state=scorer.normalize_state
time_zone_for=scorer.time_zone_for

# ---------------------------------------------------------------------------
# What the five searchable dropdowns render. One table, built from OPTIONS above, so
# the vocabulary still has exactly one source and the JS is only a view of it.
#
#   v  the value POSTed — byte-identical to what the free-text input posted before
#   l  what the rep reads (State shows "Texas" and posts "TX"; everywhere else l == v)
#   s  extra search text, so State matches on the name AND the code
# ---------------------------------------------------------------------------
STATE_LABELS={code:_title(name) for name,code in STATE_NAMES.items()}

# The value that means "no answer" for every one of the five fields. Not a guess and not
# a per-field special case: scorer._missing() is the rule, and '' is the one value it
# calls missing for all five. It matters that this is NOT the string 'Unknown' — that is
# a REAL, scoreable level for Fit (scorer.REAL_LEVELS: a value the CRM records as an
# answer, 25.7% close rate), so posting it would quietly record an answer the rep did
# not give. Empty is what a blank box has always posted.
UNKNOWN_VALUE=''
UNKNOWN_LABEL='Unknown / Other'

# Fit's 'Unknown' is a real level and reads as plain "Unknown" — label == value, like
# every field except State. It sits in the list proper; the blank-it-out choice is the
# pinned row below the divider, set apart by position and its muted italic styling
# rather than by wording.

def combo_label(key, value):
    """What a trigger shows for an already-stored value. Falls back to the raw string, so
    an older link carrying a hand-typed ?channel=youtube still displays exactly what it
    will re-post — the form never silently rewrites a value it didn't offer."""
    v=str(value or '').strip()
    if not v: return ''
    for o in COMBO.get(key,()):
        if o['v']==v: return o['l']
    return v

def _combo_options(key):
    """The option list for one vocabulary, in the order it is offered."""
    if key=='state':
        return [{'v':c,'l':STATE_LABELS.get(c,c),'s':c} for c in STATES]
    return [{'v':o,'l':o,'s':''} for o in OPTIONS[key]]
# Keyed by PARAM ('icp'), never by the scorer's field name ('icp_category'). The internal
# names are jargon and must not reach the markup — test_no_jargon_reaches_the_screen.
COMBO={f['param']:_combo_options(f['opts']) for f in FORM_FIELDS if f['opts']}

def _input_rows(r, one, derived=()):
    """(label, value, note, flag) for every intake field — the right-hand column of Why.
    Flags come from score_lead's unusable/flags pair, keyed by field, so a field the model
    couldn't use is called out next to the value that caused it. `derived` names the
    params the tool worked out itself rather than the rep typing them."""
    flags=dict(zip(r.get('unusable',[]), r.get('flags',[])))
    out=[]
    for f in FIELDS:
        val=(one.get(f['param']) or '').strip()
        note='from state' if f['param'] in derived else ''
        # An empty State box is the ONE blank that changes the number, so it says so
        # right where the box is empty rather than only in the why panel. It is not a
        # flag: the model used it, at full confidence — it just used it against them.
        if f['field']=='state' and not val: note='counted as no state on file'
        out.append((f['label'], val, note, flags.get(f['field'],'')))
    return out

# Ranked CSV results live here between the POST that builds them and the GET that shows
# them, so the results page is never the direct response to a POST. Keyed by a one-shot
# unguessable token, and the oldest boards fall out once the cap is reached.
#
# 64, not 8: this is a public demo now, so the store is shared by everyone who is on the
# page at once rather than by one person on a laptop. 8 meant a second visitor uploading a
# file could push a first visitor's board out from under them mid-read, and the page they
# came back to said the ranking had expired. 64 boards is a few megabytes of dicts and
# covers a realistic burst; the token is still one-shot and nothing is written to disk.
_RESULTS={}
_RESULTS_MAX=64
# One worker now serves eight threads, so insert-and-evict is a genuine critical section:
# two uploads at the cap can otherwise pick the same oldest key (KeyError) or mutate the
# dict mid-iteration (RuntimeError), either of which is a 500 on the upload path. Reads are
# a single atomic .get() and do not take it.
_RESULTS_LOCK=threading.Lock()

# The page is six files under templates/ now. base.html holds the shell - head, CSS, top
# bar and the page script - and each view fills its {% block content %}. _intake.html is
# shared by the two rep-facing views. Every value still reaches the browser through {{ }},
# so Jinja escapes it exactly once; nothing here escapes by hand.

def _cuts(form):
    """Cutoffs off the query string, as 0-1 scores. One unit everywhere in the UI: the
    strip, the steppers and these params are all win-out-of-100, matching the score the
    verdict shows. Junk and out-of-range values fall back to the default for that tier,
    then the three are clamped into hot >= warm >= cool so a hand-edited URL can't
    invert the scale."""
    d=dict(APPLIED['cut'])
    for k in CUT_KEYS:
        try: v=float(form.get('c_'+k))
        except (TypeError,ValueError): continue
        # rounded so a default rendered as 41.48 parses back to exactly 0.4148
        if 0<=v<=100: d[k]=round(v/100.0, 6)
    for a,b in zip(CUT_KEYS, CUT_KEYS[1:]):     # hot vs warm, warm vs cool
        d[b]=min(d[b], d[a])
    return d

def _hist(rows):
    """Leads per whole score point, 0-100, over the scorable rows of a board.
    Bucketed by floor, so hist[c:] is exactly the set of leads that a whole-number
    cutoff c calls that tier or hotter — the drag preview can't drift from tier_for()."""
    h=[0]*101
    for r in rows:
        if r.get('error') or r.get('score') is None: continue
        h[min(100,int(r['score']*100))]+=1
    return h

def _volumes(hist, pct, counts=None):
    """Per tier: how many leads it holds, what share of the board that is, and the score
    it starts at — the manager's readout. Same list and order as scorer.TIERS, so the
    calibration cards carry the shipped names, actions and colours.

    `counts` overrides the histogram tally. The view passes it on first paint so the cards
    mirror the board exactly: the defaults out of meta.json are 41.48/26.69/12.39, and
    counting those at the whole numbers the manager sees would disagree with the chips by
    tens of leads. Once a boundary is dragged the cutoffs ARE whole numbers and the
    histogram is exact, which is what the browser recomputes with."""
    n=(sum(counts.values()) if counts else sum(hist)) or 1
    out=[]
    for i,t in enumerate(TIERS):
        lo=0 if i==len(TIERS)-1 else pct[CUT_KEYS[i]]
        hi=101 if i==0 else pct[CUT_KEYS[i-1]]
        c=counts.get(t['key'],0) if counts else sum(hist[lo:hi])
        out.append(dict(t, count=c, pct=int(round(c*100.0/n)), floor=lo,
                        width=min(100,hi)-lo))   # the segment's geometry, so the bar
    return out                                   # paints right before any JS runs

def _ctx(**kw):
    """Everything the one template can see. Defaults first, caller's overrides second,
    then the few values that must be computed AFTER the override (cut_pct tracks whichever
    cutoffs are in play; score_pct drives the hero number)."""
    base=dict(single=None,qv=None,cal=None,schema=None,summary=None,declined=None,
              health=None,
              offer_sample=False,
              # Checked fresh per request (not cached at import) so setting the env vars
              # and restarting is all it takes for the Salesforce button to appear -- no
              # code change, no redeploy step beyond the restart itself.
              sf_configured=bool(os.environ.get('SF_CONSUMER_KEY')
                                 and os.environ.get('SF_CONSUMER_SECRET')
                                 and os.environ.get('SF_LOGIN_URL')),
              view='rep',one={},cut=APPLIED['cut'],fields=FIELDS,
              form_fields=FORM_FIELDS,options=OPTIONS,combo=COMBO,derived_params=(),
              unknown_label=UNKNOWN_LABEL,unknown_value=UNKNOWN_VALUE,combo_label=combo_label,
              tiers=TIERS,cut_keys=CUT_KEYS,
              tier_name=lambda k: scorer.TIER[k]['name'],
              score_params=SCORE_PARAMS,why_rows=_why_rows,unused_rows=_unused_rows,
              row_flags=_row_flags,worst_severity=_worst_severity,
              input_rows=_input_rows,usable=_usable,total=SCORE_FIELDS_TOTAL,
              base_pct=BASE_PCT,default_pct=_pct(scorer.DEFAULT_CUTOFFS))
    base.update(kw)
    base['cut_pct']=_pct(base['cut'])                      # after update, so it tracks edits
    s=base['single']
    base['score_pct']=(None if not s or s.get('error')
                       else int(round(s['score']*100)))    # the hero number
    # The gear in the top bar calibrates the board on screen; with none open it falls
    # through to the newest one that was ranked.
    tok=base['qv']['token'] if base['qv'] else None
    base['calibrate_url']=url_for('calibrate', token=tok) if tok else url_for('calibrate')
    return base

# A representative example lead so the landing form is GENUINELY pre-filled — the visible
# values are the values that get submitted, so clicking Score on load returns a real result
# instead of "0 of 5 fields usable". Keyed by form param. A rep can edit or Reset from here.
EXAMPLE_LEAD={'channel':'google','icp':'High Value','revenue':'$1,000,000 to $4,999,999',
              'utm':'brand','legacy':'70','state':'TX','tz':'Eastern'}

@app.route('/')
def home():
    return render_template('score.html', **_ctx(one=dict(EXAMPLE_LEAD)))

@app.route('/score')
def score():
    """Single lead, via GET. Bare /score is just the empty page; go=1 (the intake form) or
    any non-empty scoring param means a lead was submitted, so score it. The tuning strip
    posts here too — without a lead it only carries cutoffs, and must not conjure a verdict."""
    if not request.args.get('go') and not any(request.args.get(p) for p in SCORE_PARAMS):
        return render_template('score.html', **_ctx(cut=_cuts(request.args)))
    cuts=_cuts(request.args)
    one={p: request.args.get(p,'') for p in SCORE_PARAMS}          # repopulates the form
    lead={f['field']: request.args.get(f['param']) for f in FIELDS}
    # FORM ONLY: a rep types a state, not a time zone. An explicit ?tz= still wins, so
    # older links keep reproducing their score; the batch path never comes through here
    # and keeps using whatever time_zone the CSV supplied.
    # FORM ONLY. The batch path never reaches this function, which is exactly why
    # deriving here cannot move a batch score. An explicit ?tz= still wins so older
    # links reproduce their original score.
    derived=(); state_note=None
    typed_state=(request.args.get('state') or '').strip()
    code=normalize_state(typed_state)
    if code:
        lead['state']=code
        if code!=typed_state: state_note=f"State '{typed_state}' → {code}"
    if not (request.args.get('tz') or '').strip():
        lead['time_zone']=time_zone_for(typed_state)
        if lead['time_zone']:
            one['tz']=lead['time_zone']; derived=('tz',)   # shown, and labelled as derived
    lead['lead_id']=request.args.get('lead_id')                     # scorer's own field names
    single=scorer.score_lead(lead, cuts)
    # Same "Read as:" treatment the other fields get, so a resolved state is visible.
    if state_note and not single.get('error'): single['normalized'].append(state_note)
    return render_template('score.html', **_ctx(
        single=single, one=one, cut=cuts, derived_params=derived))

# ---------------------------------------------------------------------------
# CSV ingestion. The contract: one output row per non-blank input row, no
# exceptions. A file we cannot read at all fails loudly; a row we cannot make
# sense of comes back with a reason attached, never dropped and never fatal.
# Field VALUES are not touched here — scorer.normalize_lead owns that for both
# paths. This layer only turns bytes into per-lead dicts.
# ---------------------------------------------------------------------------
HEADER_MAP=csv_io.build_header_map(FIELDS)

# Real Salesforce / HubSpot field API names, aliased to the scorer's own field names.
# ONLY used by /api/score (see API_HEADER_MAP below) — deliberately NOT merged into
# HEADER_MAP, so the CSV upload path (read_leads) and everything that pins its behavior
# (test_schema_guard.py, test_schema_match.py) is untouched. A column named exactly one of
# these still won't be picked up by a CSV upload today; only the JSON API recognizes them.
#
#   leadsource / statecode      Salesforce Lead standard fields (LeadSource, StateCode)
#   rating                      Salesforce Lead.Rating (Hot/Warm/Cold) -> icp_category.
#                               The vocabulary does not match (icp_category expects High
#                               Value/Ideal/Low Value/Unknown), so this is NOT a semantic
#                               translation — a Rating value degrades exactly the way any
#                               unrecognized category value does: flagged, unusable, never
#                               guessed. Aliased anyway because the field is real and worth
#                               reading; scorer.normalize_category owns what happens to it.
#   hs_analytics_source         HubSpot contact property: Original Source
#   annualrevenue                Salesforce AND HubSpot both use this exact API name
#   hubspotscore                 HubSpot's own predictive lead score -> legacy_score
#   lead_source / leadscore      common custom-property spellings on either platform
#   hs_object_id                 HubSpot's own contact/record id
#   marketing_channel_c          Marketing_Channel__c, a CUSTOM Lead field this project's
#                                org carries -> utm_medium
#   prior_score_c                Prior_Score__c -> legacy_score
#
# Those last two are why writeback exists at all in the shape it does. utm_medium and
# legacy_score have NO home on a standard Lead, and LeadSource/Rating had a home with the
# wrong vocabulary, so a Salesforce-sourced lead failed all four KEY_FIELDS and was capped
# at Low confidence however clean the record was. Two custom fields plus two widened
# standard picklists is what lets one reach High. Held by
# test_a_fully_mapped_salesforce_lead_reaches_high_confidence.
#
# Both need an EXPLICIT alias line and always will: _norm_header turns
# 'Marketing_Channel__c' into 'marketing_channel_c', never 'utm_medium'. The '__c' suffix
# means no custom field on any object can ever resolve by accident, whatever it is named —
# which is why the names above are chosen to read well rather than contorted toward a
# match that was never available.
#
# Real orgs customize field names further (a custom object, a renamed property) — that
# widening is the same one-line-per-alias shape as everything below, which is the point:
# this is the seam a GTM engineer extends per org, not a finished mapping for every org.
CRM_ALIASES={'leadsource':'channel', 'hs_analytics_source':'channel', 'lead_source':'channel',
             'statecode':'state',
             'rating':'icp_category',
             'annualrevenue':'company_annual_revenue',
             'hubspotscore':'legacy_score', 'leadscore':'legacy_score',
             'marketing_channel_c':'utm_medium', 'prior_score_c':'legacy_score',
             'hs_object_id':'lead_id'}
API_HEADER_MAP=dict(HEADER_MAP, **CRM_ALIASES)

def read_leads(raw):
    """bytes -> (leads, report). The policy and the parsing live in csv_io; this binds
    them to this app's column vocabulary so callers (and the tests) pass only the bytes."""
    return csv_io.read_leads(raw, HEADER_MAP, FIELD_NAMES)

# Confidence, hardest to softest — the ranking tie-break below reads from this, so it
# stays in step with scorer.CONF_LEVELS rather than repeating the order.
_CONF_RANK={c:i for i,c in enumerate(scorer.CONF_LEVELS)}

def score_row(lead, cuts=None):
    """Score ONE ingested row. THE batch entry point: the ranked board, the CSV export and
    the tests all come through here, so these rules cannot drift apart.

    Two things happen that the single-lead form does not do, both because a batch row has
    to be actionable on its own:
      - a row with no lead_id is not scored at all. An unidentifiable lead cannot be
        called or reconciled, and a made-up id would rank it among real leads.
      - anything the model throws is caught per row, so one bad row out of 4,255 cannot
        take the file down. The row still ships, carrying its reason."""
    lid=str(lead.get('lead_id') or '').strip()
    if not lid: return _no_id_result()
    try:
        return scorer.score_lead(lead, cuts)
    except Exception as e:
        # Bare except on purpose — whatever the model throws on row 3,000 of an unseen
        # file, the other 4,254 still rank. The table renders this in place.
        return {'lead_id':lid,'error':f'could not score: {e}',
                'why':[],'normalized':[],'flags':[],'warnings':[]}

def _no_id_result():
    return {'lead_id':'','error':'no lead_id, so this row could not be scored',
            'why':[],'normalized':[],'flags':[],'warnings':[]}

def score_rows(leads, cuts=None):
    """Score MANY ingested rows in one pass — the batch form of score_row, same rules.

    The id check happens here rather than inside the scorer because it is a batch rule:
    an unidentifiable row never reaches the model, so it also never occupies a slot in
    the batched frame. Everything else goes through scorer.score_leads, which falls back
    to per-row scoring if the batched call fails, so the per-row failure contract holds."""
    out=[None]*len(leads); todo=[]; where=[]
    for i,lead in enumerate(leads):
        if str(lead.get('lead_id') or '').strip():
            todo.append(lead); where.append(i)
        else:
            out[i]=_no_id_result()
    for i,r in zip(where, scorer.score_leads(todo, cuts)):
        out[i]=r
    return out

def rank_key(r):
    """Ranked-queue order, fully deterministic. Score first, then confidence, then id.

    The tie-break matters because a pile-up at one score used to order arbitrarily by
    input position. Confidence breaks it the way a rep would want — of two leads that
    score the same, call the one we actually know something about — and lead_id makes the
    remainder reproducible instead of merely stable."""
    s=r.get('score')
    return (-(s if s is not None else -1.0),
            _CONF_RANK.get(r.get('confidence'), len(_CONF_RANK)),
            str(r.get('lead_id') or ''))

def match_band(coverage):
    """coverage -> 'ok' | 'notice' | 'refuse'. The only place the two constants are read,
    so the thresholds cannot be applied one way on the board and another in a message."""
    if coverage>=MATCH_NOTICE: return 'ok'
    if coverage>=MATCH_REFUSE: return 'notice'
    return 'refuse'

# ---------------------------------------------------------------------------
# RUN HISTORY. One row per scoring run, written from _summarize — the one function every
# intake path already converges on, so a path added later is logged by existing rather
# than by remembering to log. The store is runs.py; this is the seam between a scored
# board and a row about it.
#
# ORIGIN, not source. `source` is already taken in this file: it is the Salesforce
# provenance dict threaded through _rank_leads onto the board. The intake label needed a
# name of its own, and it is what lands in the run's `source` column and shows on /health.
#
# origin=None records nothing, which is the right default rather than an oversight.
# _summarize is also called by the test suite and by anything scoring off-request, and a
# number produced outside a real intake has no business in a history of real intakes —
# the same reasoning that keeps APPLIED out of off-request scoring.
ORIGINS=('upload','sample','api','salesforce')

# What /health calls each origin. The stored value stays the short machine word — it is a
# key, and a key that reads like prose gets rewritten by the first person who dislikes the
# prose. This is the display layer doing its one job, in the file that owns how things
# look. An origin with no entry here shows under its stored name rather than vanishing.
ORIGIN_LABELS={'upload':'CSV upload','sample':'Sample file',
               'api':'JSON API','salesforce':'Salesforce'}

def _record_run(summary, report, origin):
    """A finished summary -> one row of run history. Best effort, always.

    runs.record already swallows everything it can raise; this catches on top of it
    because the contract is stronger than "the store handles its own errors". Nothing
    between a summary and a stored row — a header that is not a list, a summary key that
    moved — may cost a rep the board they uploaded a file to get. The bookkeeping is
    allowed to fail. The run is not."""
    if not origin: return
    try:
        runs.record(source=origin,
                    fingerprint=runs.schema_fingerprint(report.get('header') or ()),
                    coverage=summary['coverage'], band=summary['band'],
                    rows_in=summary['rows_in'], scored=summary['scored'],
                    failed=summary['failed'])
    except Exception:
        log.warning('run history entry skipped; the scoring run is unaffected', exc_info=True)

def _summarize(res, report, n_in, origin=None):
    """N in / N scored / N flagged by type / confidence split — printed to the console and
    shown above the ranked list, so a messy run is legible without reading rows.

    The page shows one plain sentence built from this; the full breakdown sits behind the
    Details toggle. Keep both: the counts are the proof that no lead was dropped."""
    conf=collections.Counter(r['confidence'] for r in res if not r.get('error'))
    kinds=collections.Counter()
    for r in res:
        for f in _row_flags(r):
            kinds[_flag_kind(f)]+=1
    # Counts of LEADS, not of flags, and a lead with both a garbage value and an absent
    # field is counted in each. The two will not sum to `flagged` and are not meant to:
    # "N had an unrecognized value" and "M had a missing field" are both true of that lead,
    # and forcing them to add up would make one of the two sentences a lie.
    bad=sum(1 for r in res if any(_flag_severity(f)==BAD for f in _row_flags(r)))
    missing=sum(1 for r in res if any(_flag_severity(f)==MISSING for f in _row_flags(r)))
    # How much of this FILE the model could read, from what the scorer already decided per
    # row. See MATCH_REFUSE above for the measure and why it is cells rather than values.
    # Only scored rows count: a row that failed carries no 'unusable' list to add up.
    scored=[r for r in res if not r.get('error')]
    total_cells=len(scored)*len(scorer.KEY_FIELDS)
    by_field=collections.Counter(f for r in scored for f in r.get('unusable',())
                                 if f in scorer.KEY_FIELDS)
    usable_cells=total_cells-sum(by_field.values())
    # No cells at all — every row failed, or every key field was blank on every row — is
    # not a divide-by-zero and not a pass. There is nothing here to rank, so it reads as
    # the worst possible match rather than as an undefined one.
    coverage=(usable_cells/total_cells) if total_cells else 0.0
    s={'rows_in':n_in,'scored':sum(1 for r in res if not r.get('error')),
       'failed':sum(1 for r in res if r.get('error')),'blank_skipped':report['blank'],
       'flagged':sum(1 for r in res if _row_flags(r)),
       'bad_value':bad,'missing_field':missing,
       'by_kind':dict(kinds.most_common()),
       'confidence':{c:conf.get(c,0) for c in scorer.CONF_LEVELS},
       'missing_cols':report['missing_cols'],'ignored_cols':report['ignored_cols'],
       'coverage':round(coverage,4),'band':match_band(coverage),
       'usable_cells':usable_cells,'total_cells':total_cells,
       'unusable_by_field':dict(by_field.most_common())}
    print('[rank] '+json.dumps(s, ensure_ascii=False))
    _record_run(s, report, origin)
    return s

def _match_detail(summary):
    """The facts behind a poor match, in one shape, used by BOTH the refusal and the
    notice — so the two can never describe the same file differently.

    The distinction that makes this worth showing: a field the model could not read on
    essentially every row is a COLUMN problem, and a rep can fix it by renaming a header.
    A field it could not read on some rows is ordinary messy data, which the board already
    marks per row. Only the first kind is reported here."""
    labels={f['field']:f['label'] for f in FIELDS}
    scored=summary['scored']
    unreadable=[f for f,n in summary['unusable_by_field'].items()
                if scored and n>=scored*0.9]
    d={'pct':int(round(summary['coverage']*100)),
       # Coverage is measured over the rows that SCORED, so any sentence quoting it has to
       # name that population rather than the file. A file of 100 rows where 99 carry no
       # id has one scored row, and "25% across your 1 leads" describes a file nobody
       # uploaded. rows_in and failed are carried alongside so the copy can say both.
       'rows':scored,
       'rows_text':f'{scored:,}',        # prose, so it gets the separator the counts don't
       'rows_in':summary['rows_in'],
       'failed':summary['failed'],
       'fields':len(scorer.KEY_FIELDS),
       'unreadable':[(labels.get(f,f), f) for f in unreadable],
       # Which of the scored fields we FOUND as columns, separately from which we could
       # read. The difference is the whole diagnosis on a file like an export from another
       # CRM: every column recognized, not one value in them familiar. Saying only "we
       # could not read it" would leave a rep renaming headers that were already right.
       'read':[(labels.get(f,f), f) for f in scorer.KEY_FIELDS
               if f not in summary['missing_cols']],
       'ignored':summary['ignored_cols'],
       # Whether any column we DID read carried values we could not use. Without this the
       # copy blames the values whenever coverage is short, which is wrong in the common
       # case where the columns that were there were fine and the ones that mattered were
       # simply absent.
       'values_unreadable':any(n for f,n in summary['unusable_by_field'].items()
                               if f not in summary['missing_cols']),
       'expected':[(labels.get(f,f), f) for f in scorer.KEY_FIELDS],
       'high':summary['confidence'].get('High',0),
       # Whether NOTHING resolved, which is a different sentence from "not enough did".
       # A file in another CRM's vocabulary has every column read and not one value
       # recognized; a half-mapped file has some of both, and telling its owner none of
       # their values were understood would be false.
       'none_usable':summary['usable_cells']==0,
       'suggest':None}
    # One column we could not place and one field we could not fill is a rename, almost
    # every time. Offered as a question, not a fact: this is a guess about intent, and
    # this tool does not launder guesses into answers. With more than one of either, the
    # pairing is ambiguous and nothing is suggested at all.
    if len(unreadable)==1 and len(d['ignored'])==1:
        d['suggest']=(d['ignored'][0], labels.get(unreadable[0],unreadable[0]), unreadable[0])
    return d

def _decline(message, **extra):
    """A file we will not rank, in the shape results() renders.

    EVERY refusal is built here so the way out cannot be left off one of them. It was:
    the sample-file button hung off the schema detail, which only the vocabulary-mismatch
    refusal carries, so an unreadable CSV, an empty one, a header with no rows and an
    oversized upload all ended at a dead end with nothing to click. A refusal that leaves
    somebody stuck is half a refusal."""
    return dict(single={'error':message}, offer_sample=True, **extra)

def _rank_bytes(raw, cuts, origin):
    """CSV bytes -> the payload results() renders. THE one path a ranked board is built
    on: the upload and the sample button both come through here, so neither can produce a
    board the other would not.

    Three outcomes, and which one is chosen is decided here rather than in the template:
    a file we could not read at all, a file we read but will not rank, and a board.

    origin ('upload' or 'sample') is only a label for run history — see _record_run. It
    reaches nothing that decides anything, which is why the two callers can differ on it
    and still be unable to produce different boards. A file we could not read at all
    returns above without a row: nothing was scored, so there is no run to describe."""
    try:
        leads,report=read_leads(raw)
    except ValueError as e:                       # file-level: the only loud failure
        return _decline(f'Could not read that CSV: {e}')
    if len(leads)>MAX_ROWS:
        return _decline(
            f'That file has {len(leads):,} rows and this ranks up to {MAX_ROWS:,} at a '
            'time. Split it and rank the parts, or run the tool locally.')
    return _rank_leads(leads, report, cuts, origin=origin)

def _rank_leads(leads, report, cuts, source=None, origin=None):
    """(lead_dict, why_notes) pairs + a report dict -> the payload results() renders. THE
    shared tail of every intake path that produces a ranked board -- extracted out of
    _rank_bytes so a non-CSV source (the Salesforce pull below) scores, ranks and declines
    through the EXACT same rules a CSV upload does, rather than a second copy of them that
    could quietly drift. report shape: {'blank','missing_cols','ignored_cols','header'} --
    see read_leads for what CSV puts there; a non-CSV source fills in the same keys.

    source, when given, is opaque here -- just a dict (today: {'org','raw_url'} from the
    Salesforce pull) carried onto whichever payload shape gets returned below, ranked or
    declined, so results.html/score.html can show where a non-CSV board came from. None
    for the CSV/sample paths, which have nothing to attribute and render no such note.

    origin is the unrelated one-word run-history label ('upload' / 'sample' / 'salesforce')
    and is passed straight to _summarize. Two names because they answer two questions:
    source is shown to a rep looking at one board, origin is what /health groups by."""
    # THE contract: every input row produces an output row, carrying its reason if it
    # could not be scored. score_rows owns both rules; see it and scorer.score_leads.
    res=score_rows([lead for lead,_ in leads], cuts)
    for r,(_lead,why) in zip(res, leads): r['row_notes']=why
    res.sort(key=rank_key)                                       # ranked queue
    summary=_summarize(res, report, len(leads), origin)
    band=summary['band']

    # The file was read and scored, and the scores are not worth showing. Every row has a
    # number — the model always returns one — but a number computed from almost nothing is
    # the thing this refusal exists to not put in front of a rep as a ranking. Declined
    # through the same channel as an unreadable CSV and an oversized upload, because to
    # the person uploading it these are all the same event: the tool said no, and why.
    if band=='refuse':
        d=_match_detail(summary)
        # Two phrasings, because one of them would be a lie in the other's case. With no
        # failures the file and the scored rows are the same thing and the sentence can
        # say "your 5 leads". With failures they are not, and the coverage figure only
        # ever described the rows that scored.
        leads='lead' if d['rows']==1 else 'leads'
        across=(f"across your {d['rows_text']} {leads}" if not d['failed']
                else f"across the {d['rows_text']} {leads} it could score at all")
        return _decline(
            f"That file was read, but not ranked. Of the {d['fields']} fields this model "
            f"scores on, it could use {d['pct']}% {across} — too little to put them in an "
            'order worth trusting, so it has not.',
            schema=d,
            # The ranking is withheld; the ROWS are not. score_rows' contract is that a
            # row which could not be scored still ships carrying its reason, and a refusal
            # that swallowed those rows would break it one level up — the page would be
            # the only place a lead ever disappeared.
            summary=summary,
            declined=[r for r in res if r.get('error')][:DECLINED_SHOWN],
            **({'source':source} if source else {}))

    payload={'queue':res,'summary':summary}
    if source: payload['source']=source
    if band=='notice':
        payload['match']=_match_detail(summary)
    return payload

@app.route('/rank', methods=['POST'])
def rank():
    """Post/Redirect/Get: rank the upload, stash it, then send the browser to a plain GET.
    Nothing is ever rendered as the response to this POST."""
    f=request.files.get('csv')
    if not (f and f.filename):
        return redirect(url_for('home'), code=303)
    return _stash_and_redirect(_rank_bytes(f.read(), _cuts(request.form), 'upload'))

@app.route('/rank/sample', methods=['POST'])
def rank_sample():
    """Rank the file that ships with the tool, with nothing to choose and nothing to
    upload. A visitor who has never seen this should be able to reach a real board in one
    click; asking them to find a CSV first is a wall in front of the only thing worth
    looking at.

    A POST, not a link, because it creates a board — the same reason /rank is a POST — and
    it goes through _rank_bytes, so this and uploading the same file cannot diverge."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           SAMPLE_FILE), 'rb') as fh:
        return _stash_and_redirect(_rank_bytes(fh.read(), _cuts(request.form), 'sample'))

def _stash_and_redirect(payload):
    """Hold the payload under a one-shot token and send the browser to the plain GET.
    Every board-creating route ends here, so Post/Redirect/Get is one line, not a habit
    each route has to remember."""
    token=secrets.token_urlsafe(9)
    with _RESULTS_LOCK:
        _RESULTS[token]=payload
        while len(_RESULTS)>_RESULTS_MAX:
            _RESULTS.pop(next(iter(_RESULTS)))                  # drop oldest
    return redirect(url_for('results', token=token), code=303)

def _retier(r, cuts):
    """A stored result re-tiered against new cutoffs — scorer.tier_for over a score that
    is already computed. The model does NOT run again: applying a new cutoff to a
    4,255-lead board is this function 4,255 times, and it is instant.

    Name and action come back from scorer.TIER, so a moved boundary can never leave the
    badge and the instruction out of step."""
    if r.get('error') or r.get('score') is None: return r
    key=scorer.tier_for(r['score'], cuts)
    if key==r.get('tier'): return r
    return dict(r, tier=key, tier_name=scorer.TIER[key]['name'], action=scorer.TIER[key]['action'])

def _read_filters():
    """tier / conf / q off the query string, validated. Unknown values fall back to 'all'
    so a hand-edited URL can never render a broken view."""
    tier=request.args.get('tier','all'); conf=request.args.get('conf','all')
    q=(request.args.get('q') or '').strip()
    if tier not in scorer.TIER and tier!='all': tier='all'
    if conf not in CONF_KEYS and conf!='all': conf='all'
    return tier,conf,q

def _read_per():
    """Rows per page. Only the offered sizes are honoured, so a hand-edited URL can't
    ask the server to paint the whole board."""
    try: per=int(request.args.get('per',PAGE_SIZE))
    except (TypeError,ValueError): return PAGE_SIZE
    return per if per in PAGE_SIZES else PAGE_SIZE

def _apply_filters(rows, tier, conf, q):
    """Filter the stored result set. Rows that failed to score carry no tier or
    confidence, so they only survive when the matching filter is 'all'."""
    out=rows
    if tier!='all':
        out=[r for r in out if not r.get('error') and r.get('tier')==tier]
    if conf!='all':
        out=[r for r in out if not r.get('error') and r.get('confidence','').lower()==conf]
    if q:
        ql=q.lower()
        out=[r for r in out if ql in str(r.get('lead_id') or '').lower()]
    return out

def _chips(rows, tier, conf):
    """Counts are taken over the WHOLE stored set, not the filtered slice, so they stay
    put as filters change — a chip reading 'Hot 424' still says 424 after you click it.
    Looks wrong for a second, is right: it is how many exist, not how many are showing."""
    tc=collections.Counter(r['tier'] for r in rows if not r.get('error'))
    cc=collections.Counter(r['confidence'].lower() for r in rows if not r.get('error'))
    tiers=[('all','All',len(rows))]+[(t['key'],t['name'],tc.get(t['key'],0)) for t in TIERS]
    confs=[('all','All',len(rows))]+[(c.lower(),c,cc.get(c.lower(),0)) for c in scorer.CONF_LEVELS]
    return tiers,confs

@app.route('/results/<token>')
def results(token):
    payload=_RESULTS.get(token)
    if payload is None:  # server restarted, or pushed out by newer rankings
        return render_template('score.html', **_ctx(single={'error':
            'That ranking is no longer available. Upload the CSV again to re-rank it.'}))
    if 'queue' not in payload:                       # a stashed CSV-parse error
        return render_template('score.html', **_ctx(**payload))

    # Tiers come off the team-wide cutoffs a manager applied — the rep view never sets
    # them. No re-scoring: the model never runs again, only tier_for() does.
    cuts=APPLIED['cut']
    rows=[_retier(r, cuts) for r in payload['queue']]
    tier,conf,q=_read_filters()
    per=_read_per()
    sel=_apply_filters(rows, tier, conf, q)

    npages=max(1, math.ceil(len(sel)/per))
    try: page=int(request.args.get('page',1))
    except (TypeError,ValueError): page=1
    page=max(1, min(page, npages))
    # Sliced server-side: at most `per` rows ever reach the DOM, whatever the board size.
    start=(page-1)*per
    pagerows=sel[start:start+per]

    def link(**over):
        """Filter links deliberately drop 'page', so changing a filter returns to page 1.
        'per' rides along, so picking 100 doesn't clear the filters and vice versa."""
        a=dict(tier=tier, conf=conf, q=q, per=per); a.update(over)
        if a.get('per')==PAGE_SIZE: a.pop('per')     # keep the default out of the URL
        return url_for('results', token=token,
                       **{k:v for k,v in a.items() if v and v!='all'})

    tiers,confs=_chips(rows, tier, conf)
    qv=dict(rows=pagerows, tier=tier, conf=conf, q=q, tier_chips=tiers, conf_chips=confs,
            match=payload.get('match'), source=payload.get('source'),
            total=len(rows), matching=len(sel), page=page, npages=npages, per=per,
            sizes=PAGE_SIZES, first=start+1, last=start+len(pagerows), link=link, token=token,
            filtered=(tier!='all' or conf!='all' or bool(q)), summary=payload.get('summary'),
            # The write-back panel, built from the rows as they are tiered RIGHT NOW, so
            # what it offers to send is what the board above it is showing. None on every
            # board that is not a live Salesforce pull, and on a declined one.
            writeback=_sf_plan(payload, rows), wrote=payload.get('writeback'),
            writeback_url=url_for('writeback', token=token),
            base_url=url_for('results', token=token),
            export_url=url_for('export', token=token,
                               **{k:v for k,v in dict(tier=tier,conf=conf,q=q).items()
                                  if v and v!='all'}))
    return render_template('results.html', **_ctx(qv=qv, cut=cuts))

@app.route('/calibrate')
@app.route('/calibrate/<token>')
def calibrate(token=None):
    """Manager · Calibration — the only surface where cutoffs move. Volume-based: the
    board's score histogram turns a boundary into a number of leads to work. Reps reach
    the ranked list directly and never land here."""
    if token is None:
        # Under the lock because reversed() iterates: a concurrent upload evicting from
        # the store mid-iteration would otherwise raise here.
        with _RESULTS_LOCK:
            token=next(reversed(_RESULTS), None)     # newest board ranked this session
    payload=_RESULTS.get(token) if token else None
    rows=payload['queue'] if payload and 'queue' in payload else []
    hist=_hist(rows)
    pct=_pct(APPLIED['cut'])
    # Tier the board the way the rep view will, not the way it was stored at rank time.
    live=collections.Counter(scorer.tier_for(r['score'], APPLIED['cut']) for r in rows
                             if not r.get('error') and r.get('score') is not None)
    cal=dict(token=token, hist=hist, total=sum(hist), errors=len(rows)-sum(hist),
             volumes=_volumes(hist, pct, live), applied=bool(request.args.get('applied')),
             back_url=url_for('results', token=token) if token else url_for('home'),
             apply_url=url_for('apply_cutoffs', token=token) if token else None)
    return render_template('calibrate.html', **_ctx(view='calibrate', cal=cal))

@app.route('/calibrate/<token>/apply', methods=['POST'])
def apply_cutoffs(token):
    """Commit the previewed boundaries team-wide, then Post/Redirect/Get back to the
    calibration view so a refresh can't re-apply. Re-tiering happens on render via
    _retier -> scorer.tier_for; no lead is ever re-scored."""
    APPLIED['cut']=_cuts(request.form)
    return redirect(url_for('calibrate', token=token, applied=1), code=303)

@app.route('/results/<token>/export')
def export(token):
    """CSV of the entire current filter selection — every page, not just the visible one."""
    payload=_RESULTS.get(token)
    if payload is None or 'queue' not in payload:
        return redirect(url_for('results', token=token), code=303)
    tier,conf,q=_read_filters()
    sel=_apply_filters(payload['queue'], tier, conf, q)
    buf=io.StringIO(); w=csv.writer(buf)
    # win_pct stays a plain number (45, not "45%") so the file is still machine-readable;
    # only the on-screen rendering gained the % sign.
    w.writerow(['rank','lead_id','win_pct','score','tier','action','confidence','flags'])
    for i,r in enumerate(sel,1):
        if r.get('error'):
            w.writerow([i, r.get('lead_id') or '', '', '', 'error', '', '',
                        '; '.join([r['error']]+r.get('row_notes',[]))])
        else:
            w.writerow([i, r.get('lead_id') or '', round(r['score']*100), r['score'],
                        r['tier_name'], r['action'], r['confidence'],
                        '; '.join(r.get('flags',[])+r.get('warnings',[])+r.get('row_notes',[]))])
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition':'attachment; filename="lead-scorer-results.csv"'})

@app.route('/api/score', methods=['POST'])
def api_score():
    """JSON scoring endpoint for machine callers — Clay, Zapier, a CRM workflow step, a
    reverse-ETL sync — that want a score back inline instead of a page. THE THIRD PATH
    through this file, alongside the typed lead (GET /score) and the CSV (POST /rank):
    same scorer.score_lead underneath, same team cutoffs (Manager · Calibration / APPLIED)
    the board and the single-lead verdict already read, so a caller never sees a number the
    web UI wouldn't also show for the same lead.

    Body is one lead object, or {"leads": [...]} / a bare JSON array for a batch (capped at
    API_MAX_LEADS — see there for why). Lead keys are resolved through API_HEADER_MAP
    (case/space/punctuation-insensitive via csv_io._norm_header): the scorer's own field
    names and the form's short params, same as a CSV upload — PLUS a set of real Salesforce
    and HubSpot field API names (LeadSource, StateCode, AnnualRevenue, hs_analytics_source,
    hubspotscore, and a couple of common custom-property spellings — see CRM_ALIASES above
    for the full list and what each maps to, including the one, Rating, whose vocabulary
    doesn't actually match and degrades on purpose rather than mistranslating). An
    unrecognized key is ignored rather than rejected, exactly like an unmapped CSV column —
    see read_leads above. state -> time_zone is derived when time_zone is omitted, the same
    derivation the single-lead form does in score() above. A lead with no lead_id comes back
    unscored with a reason instead of being rejected, same as a CSV row with a blank id —
    see score_row.

    Deliberately outside the E1xx-E9xx taxonomy below: those are for this process breaking,
    and a malformed request here is the caller's input being wrong, the same category CSV
    validation already answers with a sentence rather than a crash."""
    if not request.is_json:
        return _api_error('Content-Type must be application/json', 415)
    try:
        payload=request.get_json(force=False)
    except Exception:
        return _api_error('could not parse JSON body', 400)

    if isinstance(payload, list):
        leads_in, many = payload, True
    elif isinstance(payload, dict) and isinstance(payload.get('leads'), list):
        leads_in, many = payload['leads'], True
    elif isinstance(payload, dict):
        leads_in, many = [payload], False
    else:
        leads_in, many = None, False
    if leads_in is None:
        return _api_error('body must be a lead object, {"leads": [lead, ...]}, or a bare '
                          '[lead, ...] array', 400)
    if len(leads_in) > API_MAX_LEADS:
        return _api_error(f'{len(leads_in)} leads exceeds the {API_MAX_LEADS}-per-request cap',
                          413)

    results=[]
    for raw in leads_in:
        if not isinstance(raw, dict):
            results.append({'error':'each lead must be a JSON object','lead_id':None})
        else:
            results.append(score_row(_api_map_lead(raw), APPLIED['cut']))
    # Summarized for run history only — the response is built from `results` above and is
    # byte for byte what it was before this line existed. This path is the one intake that
    # does NOT act on the band: a caller asking for one lead gets its score back whatever
    # the coverage, because a workflow step wants an answer per record and has no page to
    # be declined on. That asymmetry is real, and /health now shows it rather than hiding
    # it — an api row sitting at a refusing band is the API being asked to score leads
    # nobody filled in, which is worth somebody seeing.
    _summarize(results, _api_report(leads_in), len(leads_in), 'api')
    body={'count':len(results),'results':results} if many else results[0]
    return Response(json.dumps(body), mimetype='application/json')

def _api_report(leads_in):
    """The report shape _summarize expects, built from a JSON batch's KEYS.

    The union across the batch, not one lead's keys: a caller sending 500 records is
    sending one schema, and a record that happens to omit a field is a blank cell, not a
    different export. The same reading _rank_salesforce takes of SF_LEAD_FIELDS.

    time_zone counts as present whenever state is, because _api_map_lead derives it — the
    identical adjustment SF_MAPPED_FIELDS makes, for the identical reason."""
    keys={_norm_header(k) for raw in leads_in if isinstance(raw,dict) for k in raw}
    mapped={API_HEADER_MAP[k] for k in keys if k in API_HEADER_MAP}
    if 'state' in mapped: mapped.add('time_zone')
    return {'blank':0,
            'missing_cols':sorted(FIELD_NAMES-mapped),
            'ignored_cols':sorted(k for k in keys if k not in API_HEADER_MAP),
            'header':sorted(keys)}

def _api_map_lead(raw):
    """A raw JSON object -> scorer field names, via API_HEADER_MAP (HEADER_MAP plus real
    Salesforce/HubSpot field aliases — see CRM_ALIASES) so a Clay/HubSpot/Salesforce-shaped
    payload resolves without the caller having to rename anything first."""
    lead={}
    for k,v in raw.items():
        field=API_HEADER_MAP.get(_norm_header(k))
        if field: lead[field]=v
    typed_state=str(lead.get('state') or '').strip()
    code=normalize_state(typed_state)
    if code: lead['state']=code
    if not str(lead.get('time_zone') or '').strip():
        tz=time_zone_for(code or typed_state)
        if tz: lead['time_zone']=tz
    lead['lead_id']=str(lead.get('lead_id') or '').strip()
    return lead

def _api_error(message, status):
    return Response(json.dumps({'error':message}), mimetype='application/json', status=status)

# ---------------------------------------------------------------------------
# LIVE SALESFORCE PULL. A second proof of the same seam /api/score proves: this reuses
# _api_map_lead / API_HEADER_MAP (CRM_ALIASES) against a REAL org instead of a hand-built
# JSON payload, and reuses score_row / rank_key exactly like the CSV board does.
#
# Auth is OAuth 2.0 Client Credentials Flow against an External Client App (Salesforce's
# current replacement for the legacy Connected App) — server-to-server, no interactive
# login, no password stored anywhere. Configured entirely via three env vars; nothing
# Salesforce-specific is hardcoded and no secret is ever in source control:
#   SF_LOGIN_URL       the org's My Domain, e.g. https://orgfarm-xxxx-dev-ed.develop.my.salesforce.com
#   SF_CONSUMER_KEY    from the External Client App's Settings tab
#   SF_CONSUMER_SECRET from the same tab, behind "Manage Consumer Details"
#
# v59.0 is pinned rather than chasing the newest release: Salesforce keeps old API
# versions callable for years, and every field this queries (Id, LeadSource, AnnualRevenue,
# State, Rating) has existed on Lead since long before v59. Nothing here depends on this
# being the latest version — any supported version works.
# ---------------------------------------------------------------------------
SF_API_VERSION='v59.0'
# The SELECT list, in three groups, because they are read for three different reasons.
#
#   the scoring inputs   LeadSource / AnnualRevenue / State / Rating, plus the two CUSTOM
#                        fields that gave utm_medium and legacy_score a home at last (see
#                        CRM_ALIASES). Before those two existed a Salesforce lead failed
#                        all four KEY_FIELDS and could not clear Low confidence.
#   this tool's own      read BACK so writeback can tell a record that already matches
#                        from one that needs writing. Idempotency is a comparison and a
#                        comparison needs the current value; without these the tool would
#                        rewrite six identical fields onto every Lead on every pull.
#   the audit pair       LastModifiedById / LastModifiedDate, so "has a human touched this
#                        since we last scored it" is decidable from the pull we already
#                        made rather than from a second query per record.
SF_INPUT_FIELDS=('LeadSource','AnnualRevenue','State','Rating',
                 'Marketing_Channel__c','Prior_Score__c')
# THE fields this tool owns, and the only fields it may ever write. Everything else on a
# Lead belongs to somebody else and stays that way. Scored_At is in the list but is
# deliberately absent from the value comparison — see _sf_verdict.
SF_WRITE_FIELDS=('LeadScorer_Score__c','LeadScorer_Tier__c','LeadScorer_Next_Action__c',
                 'LeadScorer_Confidence__c','LeadScorer_Scored_At__c',
                 'LeadScorer_Model_Version__c')
SF_AUDIT_FIELDS=('LastModifiedById','LastModifiedDate')
SF_LEAD_FIELDS=','.join(('Id',)+SF_INPUT_FIELDS+SF_WRITE_FIELDS+SF_AUDIT_FIELDS)
SF_LEAD_LIMIT=50
# sObject Collections' own ceiling. SF_LEAD_LIMIT above means a real pull never reaches it
# today, so the chunking below is currently theoretical — implemented and tested anyway,
# because the day the pull cap is raised is not the day to discover the writer never
# chunked. test_a_write_larger_than_one_batch_is_split patches this down to reach it.
SF_WRITE_BATCH=200

# Not auth -- both routes below stay open on purpose, same as everywhere else in this file.
# This is cheap insurance of a different kind: /salesforce/leads and /rank/salesforce are
# public and unauthenticated, but unlike every other route here they place a real network
# call against a real external org on every hit. A Developer Edition org's daily REST API
# call budget is finite, and a page that gets shared around and repeatedly clicked (or
# hit by a bot crawling links) could burn through it for reasons that have nothing to do
# with anyone actually using the tool. A sliding window, held in the same kind of
# process-global + lock as _RESULTS above (single gunicorn worker -- see render.yaml --
# so this needs no cross-process coordination), just caps how often the org gets hit at
# all. It says nothing about WHO is asking, only how often anyone is.
SF_MAX_CALLS_PER_HOUR=30
_SF_CALL_TIMES=collections.deque()
_SF_CALL_LOCK=threading.Lock()

def _sf_rate_limited():
    """True if the live Salesforce pull has already run SF_MAX_CALLS_PER_HOUR times in the
    last hour. Checked, not just recorded -- a call that would exceed the cap is refused
    before it reaches Salesforce, so the cap actually bounds the org's API usage rather
    than just describing it after the fact."""
    now=time.monotonic()
    with _SF_CALL_LOCK:
        while _SF_CALL_TIMES and now-_SF_CALL_TIMES[0]>3600:
            _SF_CALL_TIMES.popleft()
        if len(_SF_CALL_TIMES)>=SF_MAX_CALLS_PER_HOUR:
            return True
        _SF_CALL_TIMES.append(now)
        return False

def _sf_token():
    """Client Credentials Flow: exchange the External Client App's Consumer Key + Secret
    for a short-lived access token, scoped to whichever user the app's policy runs as
    (see Setup -> the app -> Policies -> Run As). Raises RuntimeError with Salesforce's own
    error body on failure -- see api's caller, which turns that into a plain-sentence
    response instead of a stack trace, matching this file's error policy everywhere else.

    Returns (access_token, instance_url, user_id). The user id is pulled out of the
    identity URL Salesforce hands back with the token (.../id/<org id>/<user id>), so
    knowing WHICH user this integration is costs no extra call and no extra configuration.
    Writeback needs it and cannot be safely done without it: 'a human changed this' means
    LastModifiedById is anybody but us, and with no us there is nothing to compare to.
    Empty if Salesforce did not return one, which _sf_plan treats as a reason to refuse
    the whole write rather than as a reason to guess."""
    login_url=os.environ.get('SF_LOGIN_URL')
    consumer_key=os.environ.get('SF_CONSUMER_KEY')
    consumer_secret=os.environ.get('SF_CONSUMER_SECRET')
    if not (login_url and consumer_key and consumer_secret):
        raise RuntimeError('SF_LOGIN_URL, SF_CONSUMER_KEY and SF_CONSUMER_SECRET must all be set')
    body=urllib.parse.urlencode({'grant_type':'client_credentials',
                                  'client_id':consumer_key,
                                  'client_secret':consumer_secret}).encode()
    req=urllib.request.Request(f'{login_url.rstrip("/")}/services/oauth2/token', data=body,
                                headers={'Content-Type':'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data=json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Salesforce auth failed ({e.code}): {e.read().decode(errors="replace")}') from e
    return (data['access_token'], data['instance_url'],
            str(data.get('id') or '').rstrip('/').rsplit('/',1)[-1])

def _sf_query_leads():
    """Token + SOQL query -> (raw Salesforce Lead records, instance_url, user_id). Records
    are a list of dicts in Salesforce's own field names, unmapped. instance_url is returned
    alongside them -- not just for the request itself -- because callers use it as PROOF
    this came from a real org: it's shown on the ranked board and in the raw JSON view so a technical
    reader (an engineer skimming this after a recruiter forwards it) can see it hit an
    actual *.my.salesforce.com domain via OAuth, not a canned fixture. Raises RuntimeError
    with Salesforce's own error text on either step failing -- both callers below turn that
    into a plain-sentence response, never a stack trace, matching this file's error policy
    everywhere else."""
    token, instance_url, user_id=_sf_token()
    soql=f'SELECT {SF_LEAD_FIELDS} FROM Lead LIMIT {SF_LEAD_LIMIT}'
    q=urllib.parse.urlencode({'q':soql})
    req=urllib.request.Request(
        f'{instance_url}/services/data/{SF_API_VERSION}/query?{q}',
        headers={'Authorization':f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()).get('records',[]), instance_url, user_id
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Salesforce query failed ({e.code}): '
                           f'{e.read().decode(errors="replace")}') from e

@app.route('/salesforce/leads')
def salesforce_leads():
    """Raw JSON view of a live Salesforce pull -- score_row per record, no ranked-board
    page. Useful from curl/Postman while wiring this up; /rank/salesforce below is the
    same pull rendered as the normal ranked board a rep would actually look at. org and
    each record's raw Salesforce Id/fields are left in the response on purpose -- this is
    the page a skeptical reader lands on from the "View raw API response" link on the
    board, and Salesforce's own record shape (00Q-prefixed Ids, LeadSource/Rating/etc as
    Salesforce names them) is the actual evidence, not anything this app could assert."""
    if _sf_rate_limited():
        return _api_error(f'This demo has hit its cap of {SF_MAX_CALLS_PER_HOUR} live '
                          'Salesforce pulls per hour -- cheap insurance against a public, '
                          "unauthenticated route burning the connected org's daily API "
                          'limit. Try again shortly.', 429)
    try:
        records,instance_url,_user_id=_sf_query_leads()
    except RuntimeError as e:
        return _api_error(str(e), 502)
    if not records:
        return Response(json.dumps({'count':0,'source':'salesforce','org':instance_url,
                        'results':[],
                        'note':'query ran but returned no Lead records -- add a few Leads '
                                'in Salesforce and try again'}), mimetype='application/json')
    results=[score_row(_api_map_lead(r), APPLIED['cut']) for r in records]
    results.sort(key=rank_key)
    return Response(json.dumps({'count':len(results),'source':'salesforce','org':instance_url,
                                'results':results}),
                    mimetype='application/json')

# Which scorer fields a Lead pull can possibly fill, given SF_LEAD_FIELDS above (Id maps to
# lead_id, which is not itself a FIELDS entry) plus the state -> time_zone derivation
# _api_map_lead always does. Tracks SF_LEAD_FIELDS by hand, the same way OFFERED tracks the
# model's fitted vocabulary by hand -- widen one, widen the other.
SF_MAPPED_FIELDS={'channel','icp_category','company_annual_revenue','state','time_zone',
                  'utm_medium','legacy_score'}

def _rank_salesforce(cuts):
    """Pull real Lead records from Salesforce and rank them through the exact same
    _rank_leads tail the CSV board uses -- this produces the CSV board with a different
    intake, not a second, possibly-divergent feature.

    Builds a `source` dict and threads it through to _rank_leads so the board itself
    carries proof of where it came from -- see _rank_leads and results.html. A page that
    just says "pull from Salesforce" with no visible trace of Salesforce afterward is not
    convincing to anyone who can't see the server logs; org + a link to the raw API
    response is."""
    if _sf_rate_limited():
        return _decline(f'This demo has hit its cap of {SF_MAX_CALLS_PER_HOUR} live '
                        'Salesforce pulls per hour -- cheap insurance against a public, '
                        "unauthenticated button burning the connected org's daily API "
                        'limit. Try again shortly.')
    try:
        records,instance_url,user_id=_sf_query_leads()
    except RuntimeError as e:
        return _decline(f'Could not pull from Salesforce: {e}')
    if not records:
        return _decline('Connected to Salesforce, but the query returned no Lead records. '
                        'Add a few Leads in your org and try again.')
    leads=[(_api_map_lead(r), []) for r in records]
    report={'blank':0,
            'missing_cols':sorted(FIELD_NAMES-SF_MAPPED_FIELDS),
            'ignored_cols':[],
            # The SOQL SELECT list IS this pull's header, so the run-history fingerprint
            # of a Salesforce board moves when SF_LEAD_FIELDS moves and at no other time.
            # That is the honest reading: widening the query is a schema change to
            # everything downstream of it, exactly as a new CSV column would be.
            'header':SF_LEAD_FIELDS.split(',')}
    source={'org':instance_url.replace('https://','').replace('http://',''),
            'raw_url':url_for('salesforce_leads'),
            # Named on the board, not just in the README: these two custom fields are the
            # entire reason a Salesforce lead can now reach High confidence, and a claim
            # about a schema decision is worth more next to the board it produced than in
            # a document nobody has open. Derived from SF_INPUT_FIELDS so the page cannot
            # name a field the query does not ask for.
            'custom_inputs':[f for f in SF_INPUT_FIELDS if f.endswith('__c')]}
    payload=_rank_leads(leads, report, cuts, source=source, origin='salesforce')
    # The raw records ride along on the payload so writeback can diff against what the org
    # currently holds without going back to Salesforce for it. Attached only to a payload
    # that actually has a queue: a DECLINED board never gets an 'sf' key, so there is
    # nothing for _sf_plan to build a write out of and nothing for the route to send. The
    # first and most important writeback rule is enforced by the shape of the data rather
    # than by a check somebody could forget to write.
    if 'queue' in payload:
        payload['sf']={'org':source['org'],'user_id':user_id,
                       'raw':{str(r.get('Id') or ''):r for r in records}}
    return payload

# ---------------------------------------------------------------------------
# WRITEBACK. The pull above puts a verdict on a page; this puts it on the record, which is
# where a rep actually works. SF_WRITE_FIELDS is the whole surface -- six fields this tool
# owns and the only ones it may write.
#
# FIVE RULES, in the order they matter. Each is a way this could put a number it cannot
# defend into a system of record, which is worse here than anywhere else in this codebase:
# a bad board is a page somebody closes, a bad write is a field somebody else's report
# reads six months from now.
#
#   1. A DECLINED BOARD NEVER WRITES. Below MATCH_REFUSE the tool would not put the leads
#      in an order at all; writing those same scores into the CRM is the identical guess
#      wearing a different hat. Enforced structurally -- see the 'sf' key above -- rather
#      than by a check. Same rule one row down: a row that could not be scored has nothing
#      to write and is reported, never written.
#   2. DRY RUN FIRST. The panel on the board IS the diff, and the button sends what the
#      panel showed because both go through _sf_plan. It costs no extra API call: the
#      current values came back in the pull that built the board.
#   3. IDEMPOTENT. A record whose values already match is not written, so pulling twice
#      writes once. Scored_At is deliberately outside that comparison -- it changes every
#      run by definition, and comparing it would make every record differ forever.
#   4. NEVER CLOBBER A HUMAN. LastModifiedById is somebody other than the integration user
#      AND LastModifiedDate is newer than our own Scored_At -> skip it and say so.
#   5. PARTIAL FAILURE IS REPORTED, NOT SWALLOWED. allOrNone=false, and every record's own
#      result comes back to the page by id.
# ---------------------------------------------------------------------------
def _sf_verdict(r):
    """One scored row -> the values this tool owns for it. THE definition of what
    writeback writes, read by the diff and by the send, so the preview cannot describe one
    thing and the write do another.

    Scored_At is NOT here. It is stamped at send time in _sf_write_back, because it is the
    one field whose value is 'now' rather than a fact about the lead -- including it would
    make every record differ from itself on every pull and defeat rule 3 entirely.

    Name and action come from scorer.TIER rather than off the row, the same way _retier
    does it, so a re-tiered board writes the tier it is showing."""
    return {'LeadScorer_Score__c':round(r['score'],4),
            'LeadScorer_Tier__c':scorer.TIER[r['tier']]['name'],
            'LeadScorer_Next_Action__c':scorer.TIER[r['tier']]['action'],
            'LeadScorer_Confidence__c':r['confidence'],
            'LeadScorer_Model_Version__c':MODEL_VERSION}

def _sf_dt(v):
    """A Salesforce datetime string -> an aware datetime, or None for anything unreadable.

    Salesforce writes '2026-08-28T03:40:00.000+0000', which fromisoformat has handled
    since 3.11. None means 'cannot decide', and the one caller treats that as a reason to
    skip rather than as a reason to write -- see _sf_human_edited."""
    try: return datetime.datetime.fromisoformat(str(v))
    except (TypeError,ValueError): return None

def _sf_human_edited(raw, user_id):
    """Has somebody other than the integration user touched this record since we last
    scored it? True means hands off.

    Three ways this answers no, and the order is the argument:
      - we have never written to it (no Scored_At), so there is nothing of ours to clobber
        and this is a first write, not an overwrite;
      - the last edit was ours, which is the normal case on a second pull;
      - the last edit predates our stamp, so whatever it was, we have written since.
    Anything else -- including a timestamp neither side can parse -- is a yes. The check
    errs toward skipping on purpose: a lead we decline to update is visible on the page and
    costs somebody one click, and a lead we overwrite is somebody's work gone with nothing
    left to notice.

    Ids are compared on the first 15 characters because Salesforce hands out both the
    15-character case-sensitive form and the 18-character one for the same record, and
    which one arrives depends on the endpoint."""
    scored_at=_sf_dt(raw.get('LeadScorer_Scored_At__c'))
    if scored_at is None: return False
    if str(raw.get('LastModifiedById') or '')[:15]==str(user_id or '')[:15]: return False
    modified=_sf_dt(raw.get('LastModifiedDate'))
    if modified is None: return True
    return modified>scored_at

def _sf_same(current, want):
    """Is the value already in Salesforce the value we would write?

    The number is compared at the precision it is stored at (Number(2,4)) rather than as
    an exact float, because a value that made the round trip through JSON and back is not
    bit-identical to the one that went out and a tool that rewrote every record forever
    over the last decimal place would not be idempotent in any sense a person means it."""
    if isinstance(want,float):
        try: return current is not None and round(float(current),4)==want
        except (TypeError,ValueError): return False
    return str(current or '')==str(want or '')

def _sf_plan(payload, rows, user_id=None):
    """A Salesforce board + its rows as currently tiered -> what a write would do to each
    record. None when there is nothing writeable, which is every case that matters:
    a CSV or sample board, and a DECLINED Salesforce board (no queue, so no 'sf' key).

    Called twice with the same inputs -- once by results() to draw the panel and once by
    the write route to send it -- which is what makes the dry run a promise rather than a
    description. Taking `rows` rather than reading payload['queue'] is the other half of
    that: results() re-tiers against whatever cutoffs a manager has since applied, and the
    write has to send the tier the board is showing, not the one it was scored with."""
    sf=payload.get('sf')
    if not sf or 'queue' not in payload: return None
    who=user_id if user_id is not None else sf.get('user_id')
    if not who:
        # No identity means rule 4 is undecidable, and an undecidable clobber check is not
        # a reason to write carefully -- it is a reason not to write. Refusing the whole
        # plan is the same posture the file-level match check takes on a board.
        return {'org':sf['org'],'refused':
                'Salesforce did not return an identity for this integration user, so '
                'there is no way to tell this tool\'s own edits from a person\'s. Nothing '
                'will be written.','rows':[],'writeable':0,'match':0,'overridden':0,
                'unscorable':0}
    out=[]
    for r in rows:
        lid=str(r.get('lead_id') or '')
        raw=sf['raw'].get(lid) or {}
        out.append(_sf_row_plan(r, raw, who))
    counts=collections.Counter(p['action'] for p in out)
    return {'org':sf['org'],'refused':None,'rows':out,
            'writeable':counts.get('write',0),'match':counts.get('match',0),
            'overridden':counts.get('overridden',0),
            'unscorable':counts.get('unscorable',0)}

def _sf_row_plan(r, raw, user_id):
    """One row -> one line of the diff. Four outcomes and they are checked in this order
    for a reason.

    unscorable first: a row with no id or no score has no verdict to write, whatever else
    is true of it.

    match BEFORE overridden, which is the one ordering here that is not obvious. All
    Salesforce can tell us is that the RECORD changed and who changed it -- not which
    field. Without field history there is no way to know whether a person edited a field
    this tool owns or renamed the company, so checking overridden first would report a
    record as manually overridden because somebody fixed a typo in an address. If our
    values already match, the answer is 'nothing to do' regardless of who touched it, and
    that is both true and quieter. See docs/known-issues.md for what this still over-reports."""
    lid=str(r.get('lead_id') or '')
    if r.get('error') or r.get('score') is None:
        return {'action':'unscorable','id':lid,
                'note':r.get('error') or 'no score, so there is nothing to write'}
    want=_sf_verdict(r)
    now={'score':want['LeadScorer_Score__c'],'tier':want['LeadScorer_Tier__c'],
         'conf':want['LeadScorer_Confidence__c']}
    if all(_sf_same(raw.get(k), v) for k,v in want.items()):
        return dict(action='match', id=lid, note='already matches, not written', **now)
    if _sf_human_edited(raw, user_id):
        return dict(action='overridden', id=lid,
                    note='edited in Salesforce since this tool last scored it, so it is '
                         'left alone', **now)
    return dict(action='write', id=lid, fields=want,
                was=(None if raw.get('LeadScorer_Score__c') is None
                     else round(float(raw['LeadScorer_Score__c']),4)),
                note='', **now)

def _sf_patch(instance_url, token, body):
    """ONE sObject Collections PATCH -> Salesforce's per-record result list, in the order
    the records went out. The only function here that touches the network on a write, and
    the one the tests replace.

    allOrNone is false in the body every caller builds: 199 good records must not be lost
    to one that violates a validation rule somebody added last week, and which one failed
    is information this page shows rather than swallows."""
    req=urllib.request.Request(
        f'{instance_url}/services/data/{SF_API_VERSION}/composite/sobjects',
        data=json.dumps(body).encode(), method='PATCH',
        headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Salesforce write failed ({e.code}): '
                           f'{e.read().decode(errors="replace")}') from e

def _sf_write_back(plan):
    """Send a plan. Returns what happened, per record, always.

    Rate limited per BATCH, on the same hourly budget the two read routes share, and
    checked before each call so the cap bounds the org's API usage rather than describing
    it afterwards -- the same reasoning as _sf_rate_limited's own docstring. Running out
    mid-write is reported as records not attempted, which is a different sentence from
    records that failed and has to stay one."""
    todo=[p for p in plan['rows'] if p['action']=='write']
    if not todo:
        return {'sent':0,'ok':0,'failed':0,'not_attempted':0,'results':[],
                'note':'nothing to write'}
    token,instance_url,_user=_sf_token()
    stamp=datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    results=[]; not_attempted=[]
    for i in range(0, len(todo), SF_WRITE_BATCH):
        chunk=todo[i:i+SF_WRITE_BATCH]
        if _sf_rate_limited():
            not_attempted=todo[i:]
            break
        body={'allOrNone':False,
              'records':[dict({'attributes':{'type':'Lead'},'Id':p['id']}, **p['fields'],
                              **{'LeadScorer_Scored_At__c':stamp}) for p in chunk]}
        try:
            answers=_sf_patch(instance_url, token, body)
        except RuntimeError as e:
            # A transport-level failure -- an expired token, a 500 -- is not a per-record
            # error and says nothing about the batches already sent. Letting it out of
            # here would throw away the results of every batch before it and report a
            # partial write as if nothing had happened, which is the one thing rule 5
            # exists to prevent. This chunk is failed, the rest is not attempted, and the
            # page says both. Stopping rather than continuing because the next call would
            # fail the same way and spend rate-limit budget finding out.
            for p in chunk:
                results.append({'id':p['id'],'ok':False,'error':str(e)})
            not_attempted=todo[i+len(chunk):]
            break
        for p,res in zip(chunk, answers):
            errs=res.get('errors') or []
            results.append({'id':p['id'],'ok':bool(res.get('success')),
                            'error':'; '.join(e.get('message','') for e in errs)})
    for p in not_attempted:
        results.append({'id':p['id'],'ok':False,
                        'error':f'not attempted: this demo has hit its cap of '
                                f'{SF_MAX_CALLS_PER_HOUR} Salesforce calls per hour'})
    return {'sent':len(todo)-len(not_attempted),
            'ok':sum(1 for r in results if r['ok']),
            'failed':sum(1 for r in results if not r['ok']),
            'not_attempted':len(not_attempted),'results':results,'note':None,
            'stamp':stamp}

@app.route('/results/<token>/writeback', methods=['POST'])
def writeback(token):
    """Send the plan the board is showing. Post/Redirect/Get like every other route that
    changes something, so a refresh cannot write twice -- which on this route is the
    difference between an idempotent tool and one that only claims to be.

    The plan is rebuilt here rather than carried over from the panel, against the cutoffs
    in force at this moment. That is not distrust of the preview; it is the only way the
    write can be true to a board a manager re-tiered while looking at it."""
    payload=_RESULTS.get(token)
    if payload is None or 'queue' not in payload:
        return redirect(url_for('results', token=token), code=303)
    cuts=APPLIED['cut']
    plan=_sf_plan(payload, [_retier(r, cuts) for r in payload['queue']])
    if not plan or plan['refused']:
        return redirect(url_for('results', token=token), code=303)
    try:
        payload['writeback']=_sf_write_back(plan)
    except RuntimeError as e:
        payload['writeback']={'sent':0,'ok':0,'failed':0,'not_attempted':0,'results':[],
                              'note':str(e)}
    return redirect(url_for('results', token=token), code=303)


@app.route('/rank/salesforce', methods=['POST'])
def rank_salesforce():
    """Pull and rank real Leads from the connected Salesforce org -- same Post/Redirect/Get
    shape as /rank and /rank/sample, so this and a CSV upload land on the identical board."""
    return _stash_and_redirect(_rank_salesforce(_cuts(request.form)))

# ---------------------------------------------------------------------------
# /health — the run history, read back. Two routes with nearly the same name and no
# relationship: /healthz below is a liveness probe for the host and touches nothing,
# /health is a page about the DATA that has come through this tool. Neither is a
# substitute for the other, and the process being up is exactly the state in which the
# failure this page exists to catch is invisible.
#
# WHAT IT IS FOR. One run cannot tell you its file was worse than last month's. The tool
# refuses a file that arrives unreadable; it has nothing to say about a file that arrives
# slightly worse every month until it is unreadable, because each of those runs looked
# fine. This page is the difference between "here is a screenshot of the notice" and
# "here is the run where the column was renamed".
HEALTH_RUNS_SHOWN=12   # rows in the table, per source. The counts above the table are over
                       # runs.RECENT_LIMIT, which is the window this page reads at all —
                       # said out loud on the page rather than implied, because a count
                       # that quietly stops at 400 is the kind of number this repo refuses
                       # to print everywhere else.

def _run_flags(cur, prev):
    """Why one run is worth a second look, judged against the previous run OF THE SAME
    SOURCE. [] for the first run of a source — there is nothing to have changed from.

    Both conditions are about CHANGE, not level, and that is the design. A source that has
    always sat at 60% coverage is a limitation somebody already knows about, and flagging
    it every single run would teach a reader to skim past the flags — which is how this
    page would come to fail in exactly the way the boards it watches can. A source that
    read 98% last week and 60% today is the thing nobody would otherwise notice.

    The coverage rule is a DOWNWARD CROSSING of MATCH_NOTICE, not a drop: 0.99 -> 0.85 is
    still a file whose order is worth trusting, and the tool says nothing about it on the
    board either. The run where the tool started calling its own output rough is the run
    worth a sentence."""
    if prev is None: return []
    out=[]
    if cur['fingerprint']!=prev['fingerprint']:
        out.append('Schema changed. The column set is not the one the previous run read — '
                   'a column was renamed, added or dropped.')
    if prev['coverage']>=MATCH_NOTICE>cur['coverage']:
        out.append(f'Coverage crossed below {int(round(MATCH_NOTICE*100))}%. This run\'s '
                   'order is rough; the run before it was not.')
    return out

def _health_run(r, prev):
    """One stored run, shaped for the table. Decides nothing except what _run_flags does."""
    return {'at':str(r['at'])[:16].replace('T',' '),   # 2026-08-27 21:14, UTC, seconds cut
            'source':r['source'],
            'fingerprint':r['fingerprint'] or '—',
            'pct':int(round(r['coverage']*100)),
            'band':r['band'],
            'rows_in':r['rows_in'],'scored':r['scored'],'failed':r['failed'],
            'flags':_run_flags(r, prev)}

def _health_source(name, group):
    """One source's runs, newest first. group is every run of this source that was read,
    which is what the counts are over; the table shows the newest HEALTH_RUNS_SHOWN.

    Each run is compared with the one genuinely before it, taken from `group` rather than
    from the truncated list — so the oldest row on screen is still judged against real
    history instead of reading as a first run every time the page is trimmed."""
    rows=[_health_run(r, group[i+1] if i+1<len(group) else None)
          for i,r in enumerate(group[:HEALTH_RUNS_SHOWN])]
    pcts=[r['pct'] for r in rows]
    return {'source':name,'label':ORIGIN_LABELS.get(name,name),
            'runs':len(group),'shown':len(rows),
            # The CURRENT schema: whatever the newest run read. This is the string somebody
            # compares by eye against the one they wrote down last month.
            'fingerprint':rows[0]['fingerprint'],
            'pct':rows[0]['pct'],'band':rows[0]['band'],
            'low':min(pcts),'high':max(pcts),
            'flagged':sum(1 for r in rows if r['flags']),
            'rows':rows}

def _health():
    """Run history grouped by source, or an honest account of why there is none.

    Three states, kept apart on purpose. 'off' — nothing configured, which is a supported
    way to run this app and not a fault. 'unavailable' — configured and the store could
    not be read, which IS a fault and must not read as a quiet week. 'ok' — data, possibly
    none of it yet.

    Every count here is over the newest runs.RECENT_LIMIT runs, which is the window this
    reads. The page says so; nothing here claims to be a total."""
    rows=runs.recent()
    if rows is None:
        return {'state':'unavailable' if runs.configured() else 'off',
                'env_var':runs.ENV_VAR,'sources':[]}
    by=collections.defaultdict(list)
    for r in rows: by[r['source']].append(r)          # recent() returns newest first
    # ORIGINS order first so the page reads the same every time, then anything else the
    # store holds — a source written by an older version of this app is still shown rather
    # than silently dropped, because a history that hides rows is not a history.
    order=list(ORIGINS)+sorted(set(by)-set(ORIGINS))
    return {'state':'ok','env_var':runs.ENV_VAR,'total':len(rows),'window':runs.RECENT_LIMIT,
            'sources':[_health_source(n, by[n]) for n in order if by.get(n)]}

@app.route('/health')
def health():
    """The run history. Read-only, and it writes nothing itself — loading this page is not
    a run and never appears in it."""
    return render_template('health.html', **_ctx(view='health', health=_health()))

@app.route('/healthz')
def healthz():
    """Liveness, for whatever is hosting this. Deliberately touches nothing: no model, no
    template, no _RESULTS. A health check that exercised the scorer would take the site
    down for a reason the site could survive, and one that rendered a page would keep the
    log noisy. If the process is up enough to route, it answers."""
    return 'ok', 200

# ---------------------------------------------------------------------------
# ERROR TAXONOMY — a stable code on screen, the whole story in the log.
#
# This is for CRASHES ONLY. Handled validation is the tool WORKING and keeps its
# plain-English text: a flagged field, an unreadable CSV, an expired token are all
# answers, not failures. Recoding those as "errors" would make a clean run look broken.
#
# The contract:
#   - the code is deterministic, so the same break always shows the same code;
#   - the log line carries the code, the route, the traceback and a safe fingerprint of
#     the input, so the break is reproducible from the log alone;
#   - the page shows the code and nothing else. Trace in the log, never on screen.
#
#   E1xx  file / ingestion
#     E101  header only, no data rows            read_leads     already a ValueError
#     E102  no recognizable columns              read_leads     already a ValueError
#     E103  field over size limit / bad quoting  read_leads     was a 500, now caught
#     E104  undecodable bytes                    _decode        cannot fail: latin-1 last
#   E2xx  field validation — these are FLAGS, not crashes. Listed so the numbering is
#         complete and so an export could carry them; nothing here ever raises.
#     E210  number not a plain decimal           validate_number
#     E211  number out of domain                 validate_number
#     E212  non-finite number                    _plain_number
#     E220  invalid date                         validate_date
#   E3xx  scoring / model
#     E300  one row failed to score              score_row      already caught per row
#     E301  batch scoring failure                score_leads    falls back to per-row
#   E4xx  request / route
#     E401  no file uploaded                     rank           already a redirect
#     E404  ranking token not found or expired   results        already friendly
#   E5xx  startup / config
#     E500  model.joblib missing or corrupt      import time
#     E501  meta.json missing a key              import time
#   E900  unexpected, uncaught                   the handler below
#
# Only E103 and E301 were ever real crash sites and both are handled now. The handler
# below exists for the unknown unknowns — which is exactly why it has a catch-all.
# ---------------------------------------------------------------------------
log=logging.getLogger('lead_scorer')

ERROR_CODES={
 'E101':'CSV had a header but no data rows',
 'E102':'no recognizable columns in the CSV header',
 'E103':'a CSV field exceeded the size limit, usually an unclosed quote',
 'E104':'the uploaded bytes could not be decoded',
 'E210':'a number was not a plain decimal',
 'E211':'a number was outside its valid range',
 'E212':'a number was not finite',
 'E220':'a date could not be parsed',
 'E300':'a single row failed to score',
 'E301':'the batched scoring call failed',
 'E401':'no file was uploaded',
 'E404':'that ranking token is unknown or expired',
 'E500':'the model file is missing or corrupt',
 'E501':'meta.json is missing a key',
 'E900':'unexpected, uncaught',
}

# Exception type -> code, most specific first. Type-based so it is deterministic: the
# same break yields the same code every time, which is what makes a screenshot of the
# page enough to find the log line.
_CODE_BY_EXC=((csv.Error,'E103'), (UnicodeDecodeError,'E104'),
              (FileNotFoundError,'E500'), (KeyError,'E501'))

def _error_code(e):
    for exc,code in _CODE_BY_EXC:
        if isinstance(e,exc): return code
    return 'E900'

def _fingerprint():
    """A safe description of the input — enough to reproduce the break, and no more.

    Deliberately never raises: a handler that dies while reporting a death tells nobody
    anything. Reads at most 200 bytes, which on a CSV is the header, not lead data."""
    try:
        bits=[f'path={request.path}', f'bytes={request.content_length or 0}']
        f=request.files.get('csv') if request.files else None
        if f is not None and getattr(f,'filename',None):
            bits.append(f'file={f.filename!r}')
            try:
                s=f.stream; pos=s.tell(); s.seek(0)
                bits.append(f'head={s.read(200)!r}'); s.seek(pos)
            except Exception: bits.append('head=<unreadable>')
        elif request.args:
            bits.append(f'args={dict(request.args)!r}'[:200])
        return ' '.join(bits)
    except Exception:
        return '<fingerprint unavailable>'

@app.errorhandler(RequestEntityTooLarge)
def _too_big(e):
    """An upload over MAX_CONTENT_LENGTH, answered the way every other bad file is: a
    sentence where the verdict goes.

    This is HANDLED VALIDATION, not a crash — the taxonomy above says so — and it must not
    reach _unhandled, which would let Werkzeug's bare 413 page through instead. It is
    registered as its own handler because Flask matches the most specific one, and
    RequestEntityTooLarge is an HTTPException that _unhandled deliberately passes along.

    Not flask.flash: that needs a session, a session needs a SECRET_KEY, and this app has
    neither and needs neither. Every other refusal in the file — an unreadable CSV, an
    expired ranking token — already renders through _ctx(single={'error': ...}), so the
    size limit reads the same as the rest rather than inventing a second channel."""
    mb=app.config['MAX_CONTENT_LENGTH']//(1024*1024)
    return render_template('score.html', **_ctx(single={'error':
        f'That file is too large to upload here (the limit is {mb}MB). '
        'Split it and rank the parts, or run the tool locally.'})), 413

@app.errorhandler(Exception)
def _unhandled(e):
    """Last line of defence. Anything that reaches here is a bug, so it is logged in full
    and shown as a code — never as a stack trace, which tells a rep nothing and leaks
    paths to anyone else."""
    if isinstance(e, HTTPException):
        return e                      # 404/405 keep their ordinary meaning
    code=_error_code(e)
    log.error('%s %s | %s\n%s', code, ERROR_CODES.get(code,''), _fingerprint(),
              traceback.format_exc())
    return render_template('score.html', **_ctx(single={'error':
        f'Something went wrong ({code}). The details were logged.'})), 500

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    # debug=False deliberately, and a test holds it there. debug=True serves the Werkzeug
    # interactive debugger, which is a remote code execution console for anyone who can
    # reach the port, and this file is copied verbatim into the shipped bundle. The
    # reloader it also brings is worth nothing here: the model loads at import, so a
    # restart is the slow part of the loop, not the part being saved.
    # PORT is read from the environment because 5000 is not reliably free: macOS gives it
    # to the AirPlay receiver by default, which answers with a 403 and looks like the app
    # failing rather than the port being taken. PORT=8000 python app.py sidesteps it, and
    # every host that injects $PORT works without an edit.
    #
    # HOST defaults to localhost, and that default is the safe one: this branch is the
    # DEVELOPMENT server, and a dev server bound to 0.0.0.0 is reachable by everything on
    # the coffee-shop wifi. Deployment does not come through here at all — the Procfile,
    # render.yaml and Dockerfile all start gunicorn with an explicit --bind 0.0.0.0, which
    # is the process that should be answering the internet. HOST is here for the case in
    # between, a container or a VM where the port has to be published from inside.
    app.run(debug=False, host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", 5000)))
