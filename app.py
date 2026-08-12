"""GTM Lead Scorer — the web tool. Run: python app.py -> http://127.0.0.1:5000

PRESENTATION ONLY. Every number and message on the page comes from scorer.score_lead();
this file decides how it looks, never what it says. Tier names, actions and colour keys
come from scorer.TIERS and are never restated here.

THE TWO PATHS THROUGH THIS FILE

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

  Manager · Calibration                   GET /calibrate      -> calibrate()  ~line 1158
    the only place cutoffs change. Apply writes APPLIED; nothing is re-scored.

WHERE THINGS LIVE
  FIELDS        ~63    every field: label, scorer name, URL param, combo-box options.
                      Also drives the CSV header map — a field hidden from the FORM must
                      stay in this list or the batch path stops reading its column.
  OFFERED       ~101   what the combo boxes SUGGEST. Curated by hand; not what's accepted.
  APPLIED        ~26   the team-wide cutoffs in force. Only Manager · Calibration writes it.
  PAGE          ~152   the entire UI: one Jinja template, styles included.
  _RESULTS      ~149   ranked boards held between the POST and the GET, newest 8.

Two names you will see everywhere in the template:
  one   the values the rep typed, keyed by URL param — repopulates the form and the
        "what you gave it" column. Also carries the derived time zone.
  qv    the ranked-list view model: rows for THIS page, chips, counts, pager, links.

Grep '# DEMO:' for the spots most likely to be hit live."""
from flask import Flask, request, render_template, redirect, url_for, Response
from werkzeug.exceptions import HTTPException
import io, os, csv, math, re, json, secrets, collections, logging, traceback
import scorer

# The parts that need no request and no app. Imported rather than defined here so this file
# is the routing layer and little else; the names are re-exported below, because the tests
# and the templates reach several of them through `app.`.
import csv_io, flags, format as fmt
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

TIERS=scorer.TIERS                  # [{key,name,action}] hottest -> coldest. THE vocabulary.
CUT_KEYS=scorer.CUT_KEYS            # the three movable boundaries: hot, warm, cool
CONF_KEYS=[c.lower() for c in scorer.CONF_LEVELS]
PAGE_SIZES=[25,50,100]
PAGE_SIZE=50                        # default rows per page; the rep can pick from PAGE_SIZES

# The team-wide tier cutoffs. Defaults come from meta.json via scorer; a manager moves
# them in Manager · Calibration and every view reads from here, so one setting drives the
# board and the single-lead verdict alike. Module-level state like _RESULTS below — this
# is a single-user local tool, not a multi-tenant server.
APPLIED={'cut':dict(scorer.DEFAULT_CUTOFFS)}
# Read by: results() (re-tiers the board), _ctx (the verdict), calibrate(). Written by
# exactly one function, apply_cutoffs(). make_submission.py deliberately ignores it and
# reads scorer.DEFAULT_CUTOFFS, so the graded output cannot depend on a demo session.

# Short column heading per score-driving field, for the "why" table.
BASE_PCT=round(scorer.BASE*100,1)  # read from meta, never hardcoded

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
# answer, 17.6% close rate), so posting it would quietly record an answer the rep did
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
# token; only the last few are kept, since this is a single-user local tool.
_RESULTS={}
_RESULTS_MAX=8

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
    base=dict(single=None,qv=None,cal=None,view='rep',one={},cut=APPLIED['cut'],fields=FIELDS,
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

@app.route('/')
def home():
    return render_template('score.html', **_ctx())

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

def read_leads(raw):
    """bytes -> (leads, report). The policy and the parsing live in csv_io; this binds
    them to this app's column vocabulary so callers (and the tests) pass only the bytes."""
    return csv_io.read_leads(raw, HEADER_MAP, FIELD_NAMES)

# Confidence, hardest to softest — the ranking tie-break below reads from this, so it
# stays in step with scorer.CONF_LEVELS rather than repeating the order.
_CONF_RANK={c:i for i,c in enumerate(scorer.CONF_LEVELS)}

def score_row(lead, cuts=None):
    """Score ONE ingested row. THE batch entry point: the ranked board and
    make_submission both come through here, so these rules cannot drift apart.

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

def _summarize(res, report, n_in):
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
    s={'rows_in':n_in,'scored':sum(1 for r in res if not r.get('error')),
       'failed':sum(1 for r in res if r.get('error')),'blank_skipped':report['blank'],
       'flagged':sum(1 for r in res if _row_flags(r)),
       'bad_value':bad,'missing_field':missing,
       'by_kind':dict(kinds.most_common()),
       'confidence':{c:conf.get(c,0) for c in scorer.CONF_LEVELS},
       'missing_cols':report['missing_cols'],'ignored_cols':report['ignored_cols']}
    print('[rank] '+json.dumps(s, ensure_ascii=False))
    return s

@app.route('/rank', methods=['POST'])
def rank():
    """Post/Redirect/Get: rank the upload, stash it, then send the browser to a plain GET.
    Nothing is ever rendered as the response to this POST."""
    cuts=_cuts(request.form)
    f=request.files.get('csv')
    if not (f and f.filename):
        return redirect(url_for('home'), code=303)
    try:
        leads,report=read_leads(f.read())
    except ValueError as e:                       # file-level: the only loud failure
        payload={'single':{'error':f'Could not read that CSV: {e}'}}
        token=secrets.token_urlsafe(9); _RESULTS[token]=payload
        return redirect(url_for('results', token=token), code=303)

    # THE contract: every input row produces an output row, carrying its reason if it
    # could not be scored. score_rows owns both rules; see it and scorer.score_leads.
    res=score_rows([lead for lead,_ in leads], cuts)
    for r,(_lead,why) in zip(res, leads): r['row_notes']=why
    res.sort(key=rank_key)                                       # ranked queue
    payload={'queue':res,'summary':_summarize(res, report, len(leads))}

    token=secrets.token_urlsafe(9)
    _RESULTS[token]=payload
    while len(_RESULTS)>_RESULTS_MAX:
        _RESULTS.pop(next(iter(_RESULTS)))                      # drop oldest
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
            total=len(rows), matching=len(sel), page=page, npages=npages, per=per,
            sizes=PAGE_SIZES, first=start+1, last=start+len(pagerows), link=link, token=token,
            filtered=(tier!='all' or conf!='all' or bool(q)), summary=payload.get('summary'),
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
        token=next(reversed(_RESULTS), None)         # newest board ranked this session
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
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)))
