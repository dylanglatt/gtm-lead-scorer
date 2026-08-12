"""Lead Scorer — the scoring engine. The single source of truth for how a lead is judged.

THE PATH A LEAD TAKES, in order. Both the typed form and the CSV upload go through this
exact sequence; there is no second path anywhere.

    score_lead(lead, cutoffs)                   <- the only entry point
      1. normalize_lead      canonicalize every field
                               -> normalize_category   categories, one shared rule
                               -> normalize_revenue    amounts -> bands
      2. _unusable           what the model could NOT use, one reason per field
      3. _prep_row           lay the row out the way the model was fit
      4. MODEL.predict_proba the score
      5. tier_for            score -> Hot / Warm / Cool / Cold, using the cutoffs given
      6. confidence          count of unusable fields -> High / Medium / Low
      7. _why                segment win-rates, for the explanation
    -> a dict the UI renders and the CSV export writes

THE FIVE CHOICES YOU WILL BE ASKED ABOUT:

  A blank field lowers CONFIDENCE, not the SCORE — with ONE named exception. A rep
  leaving a box empty means "I didn't type it", not "this lead is junk", so a blank
  must not drag the score down. The exception is `state`: the model was fit with a
  missing state as a feature in its own right, and those leads won 1.7% against a 16.5%
  base, so a blank state is real information rather than a hole. It is the only field
  where leaving the box empty changes the number, and the UI says so on the spot.

  BOTH INTAKES DERIVE FEATURES THE SAME WAY. The typed form and the CSV upload differ
  only in how they COLLECT raw fields; from there it is one normalize_lead, one
  _prep_dict, one model call. There is no from_form flag and there must not be one
  again: it existed for exactly one feature (state_missing) and it made the same lead
  score 38% Warm typed and 3.4% Cold uploaded.

  Unknown categories are IGNORED, not errors. The encoder is fit with
  handle_unknown='ignore', so a value the model never saw contributes zero instead of
  raising. That is what lets an unseen lead still score. See normalize_category.

  'Unknown' ICP is a REAL level, not missing data. The CRM writes it as an answer — 17
  leads in the history carry it — and it closes at 17.6%, between Low Value and Ideal.
  What it means upstream is not something this data records: all 17 are 'Not Enriched',
  so it is NOT an enrichment result. See REAL_LEVELS / _missing.

  The "why" uses SEGMENT WIN-RATES, not model coefficients. A correlated logistic gives
  High Value a negative coefficient even though High Value closes at 30% — true to the
  math, indefensible to a rep. Segment rates are both true and explainable. See _why.

Cutoffs are NOT decided here. tier_for takes them as an argument; app.py owns which
ones are in force, and only Manager · Calibration changes them.

Grep '# DEMO:' for the spots most likely to be hit live."""
import json, joblib, os, functools, math, re, datetime
import pandas as pd
_DIR=os.path.dirname(os.path.abspath(__file__))
MODEL=joblib.load(os.path.join(_DIR,'model.joblib'))
META=json.load(open(os.path.join(_DIR,'meta.json')))
BASE=META['base_rate']; FACTS=META['segment_win_rates']
LEG_MED=META['legacy_score_median']
INTAKE=['channel','campaign_ref','utm_medium','company_annual_revenue','icp_category',
        'state','zip3','time_zone','enrichment_status','legacy_score']
# The fields a rep is asked for and the model can actually use. Every one of them costs
# confidence when it is blank or unrecognized — nothing a rep types is silently ignored.
KEY_FIELDS=['channel','icp_category','company_annual_revenue','utm_medium']
# Numeric fields the model never reads. They cannot move a score, so a BLANK one is free —
# but a corrupt one still costs a confidence level, because it says the row is unreliable.
NON_FEATURE_CHECKS=['zip3']

def _blank(v): return v in (None,'','nan','Unknown') or (isinstance(v,float) and pd.isna(v))

# ---------------------------------------------------------------------------
# NUMERIC VALIDATION — domain, not just syntax.
#
# A number that PARSES is not the same as a number that is REAL. 'inf' parses. 99999
# parses. '1e9' parses. Every one of them is corrupt input for a 0-100 score, and
# feeding them to the model saturated it: legacy_score=99999 came back as a 1.0000
# win chance at HIGH confidence with no flag, so the most corrupted rows in a messy
# file became the entire top of the call queue. Parseability was the only gate.
#
# So every numeric field declares its DOMAIN here, and anything outside it is invalid
# INPUT, not a real reading. Invalid is handled exactly like missing: kept out of the
# score, flagged, and costing a confidence level. Output clamping is not a substitute —
# by the time the score is clamped to 100 the row has already won the ranking.
# ---------------------------------------------------------------------------
NUMERIC_DOMAINS={'legacy_score':(0.0,100.0),   # the score's own published scale
                 'zip3':(0.0,999.0)}           # a 3-digit ZIP prefix, 000-999

_SCI=re.compile(r'[eE]')
# A plain decimal number, in ASCII, and nothing else. float() is far more generous than
# the contract: it reads Unicode digits, so '５５' came back as 55 and '１００' as a
# perfectly real score of 100. Those are corrupted input, not readings — the same class
# of "technically parseable, obviously wrong" as '1e9'. Anchored, so one stray character
# anywhere disqualifies the whole string ('5٨' is not 58).
# [0-9] literally, NOT \d — Python's \d is Unicode-aware and matches '５' and '٨' too,
# which is the very thing this is here to reject.
_PLAIN=re.compile(r'[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)$')

def _plain_number(v):
    """v -> float, or None if it is not a plain, finite, ASCII decimal number.

    Rejected on purpose: '', None, 'abc', NaN, ±inf, scientific notation, and non-ASCII
    digits (fullwidth '５５', Arabic-Indic '٨٧١'). '1e9' and '8.71E2' are parseable and
    are never what anyone meant by a 0-100 score or a ZIP prefix, so they are input errors
    rather than readings. float() alone is far too permissive: it accepts 'inf', '-inf',
    'infinity' and 'nan', and an infinity sails past a NaN check and then dies inside the
    model with a raw sklearn error."""
    if v is None: return None
    if isinstance(v,bool): return None            # True is not the number 1 here
    if isinstance(v,(int,float)):                 # already a number: only finiteness left
        return float(v) if math.isfinite(v) else None
    s=str(v).strip()
    if not s or _SCI.search(s) or not _PLAIN.match(s): return None
    try: x=float(s)
    except (TypeError,ValueError): return None
    return x if math.isfinite(x) else None

def validate_number(field, v):
    """(value, problem) for one numeric field. value is None whenever it cannot be used.

    Three outcomes, and callers treat the last two identically:
      blank        -> (None, None)      nothing was given
      unparseable  -> (None, reason)    'abc', 'inf', '1e9'
      out of range -> (None, reason)    99999, -88, 101   <- these used to be ACCEPTED"""
    if _blank(v): return None,None
    label=LABELS.get(field,field); shown=str(v).strip()
    x=_plain_number(v)
    if x is None:
        return None,f"{label} '{shown}' is not a plain number, so it is not used in the score"
    lo,hi=NUMERIC_DOMAINS[field]
    if not lo<=x<=hi:
        return None,f"{label} '{shown}' is outside {lo:g}–{hi:g}, so it is not used in the score"
    return x,None

# Formats a CRM actually exports. strptime rejects an impossible month or day-of-month
# for us (2026-13-45, month 13), so this needs no calendar of its own.
#
# The 2-digit-year forms are here for robustness, NOT for the current file: leads_to_score
# .csv stamps '2026-05-18 20:42:20' and every one of its 4255 rows already validates on the
# first pattern. '5/18/26 20:42' is what a SPREADSHEET renders that value as, not what the
# bytes say — worth accepting in case an export ever really is written that way.
#
# %m/%d/%y is ambiguous with %d/%m/%y and we do not try to resolve it. Nothing reads the
# parsed datetime: this decides valid-or-not, never a value, so the ambiguity is harmless.
DATE_FORMATS=('%Y-%m-%d %H:%M:%S','%Y-%m-%dT%H:%M:%S','%Y-%m-%d',
              '%Y/%m/%d','%m/%d/%Y','%d/%m/%Y',
              '%m/%d/%y %H:%M','%m/%d/%y %H:%M:%S','%m/%d/%y')

def validate_date(v):
    """None if blank or a real timestamp; a problem string otherwise.

    NOTE, and it matters: the model has NO recency term — created_at is not a feature —
    so an invalid date cannot move a score by even one point. It costs a confidence level
    because a row whose own timestamp is corrupt is a row to trust less, not because the
    arithmetic changes. 'yesterday' and '2026-13-45' are not dates."""
    if _blank(v): return None
    s=str(v).strip()
    for fmt in DATE_FORMATS:
        try:
            datetime.datetime.strptime(s,fmt); return None
        except ValueError: continue
    return f"{LABELS.get('created_at','Created')} '{s}' is not a valid date"

# Values that LOOK like a placeholder but are a real recorded level for one specific
# field. icp_category='Unknown' is the CRM's own answer, not an empty box. It closes at
# 17.6%, between Low Value (11%) and Ideal (24%), and the encoder was fit with it, so the
# model can use it and should. Why the CRM writes it is not recorded anywhere we can see:
# enrichment_status is 'Not Enriched' on all 17 of them (and on 99.9% of the history), so
# whatever it means, it is not the outcome of an enrichment run.
#
# Scoped per field on purpose. 'Unknown' is not a level anywhere else, and _blank is
# consulted for every column: company_annual_revenue and utm_medium both have a real
# 'nan' level, so treating 'Unknown' as present everywhere would reroute those fields
# and move scores on features this change has no business touching.
# DEMO: type Fit = "Unknown" -> scored as a real level, High confidence, no flag.
REAL_LEVELS={'icp_category':{'unknown'}}

def _missing(field, v):
    """Is this field genuinely absent? _blank, except that a value the field records as
    a real level counts as present — a recorded 'Unknown' ICP is data, not a hole.

    # DEMO: leave a box blank -> this returns True -> _unusable flags it -> confidence
    drops one level. The score is untouched."""
    if field in REAL_LEVELS and str(v).strip().lower() in REAL_LEVELS[field]: return False
    return _blank(v)

# ---------------------------------------------------------------------------
# The vocabulary the model actually knows, and the plain-English name of each
# field. Both the normalizer and the confidence rule read from here, so "what the
# model can use" is answered in exactly one place.
# ---------------------------------------------------------------------------
# What a rep calls each field. These strings are user-facing — they appear in every flag
# and in the "why" — so they carry no jargon: no ICP, no CRM, no UTM.
LABELS={'channel':'Source','icp_category':'Fit',
        'company_annual_revenue':'Company revenue','legacy_score':'Prior score',
        'utm_medium':'Marketing channel','time_zone':'Time zone',
        'zip3':'ZIP prefix','created_at':'Created','state':'State'}

def _fit_categories():
    """The exact category values the encoder saw at fit time, per column. Anything
    outside these lists contributes nothing to the score — the encoder is fit with
    handle_unknown='ignore', so it drops the value silently."""
    try:
        for name,trans,cols in MODEL.named_steps['pre'].transformers_:
            if name=='cat':
                return {c:{str(v) for v in vals} for c,vals in zip(cols,trans.categories_)}
    except (AttributeError,KeyError,ValueError):
        pass
    return {c:set(FACTS[c]) for c in FACTS}  # fall back to meta's segment win-rates
KNOWN=_fit_categories()
# lowercase+trimmed spelling -> the exact string the model was fit on
CANON={c:{v.strip().lower():v for v in vals} for c,vals in KNOWN.items()}

# ---------------------------------------------------------------------------
# Input normalization. Reps and CRM exports spell revenue and channel a dozen
# ways; the model only knows the exact strings it was trained on. Canonicalize
# here, before _prep_row. To teach it a new spelling, edit the two tables below —
# nothing else changes.
# ---------------------------------------------------------------------------
# Half-open [lo, hi) -> the exact band string the model expects.
REVENUE_BANDS=[(0,250_000,'Less than $250,000'),
               (250_000,500_000,'$250,000 to $499,999'),
               (500_000,1_000_000,'$500,000 to $999,999'),
               (1_000_000,5_000_000,'$1,000,000 to $4,999,999'),
               (5_000_000,10_000_000,'$5,000,000 to $9,999,999'),
               (10_000_000,25_000_000,'$10,000,000 to $24,999,999'),
               (25_000_000,float('inf'),'$25,000,000 and greater')]
REVENUE_PASSTHROUGH={'Self-Serve Signup'}  # a real category, not an amount — never bucket it

# The training file tops out at '$10,000,000 to $24,999,999' — no lead in it was larger —
# so the encoder never saw '$25,000,000 and greater' and dropped it, costing the lead its
# revenue signal and a confidence level. Fold the top of the scale into the top band the
# model actually knows: a $25m+ company is not meaningfully different from a $20m one
# for this model, and using the highest fitted band is strictly better than using nothing.
# Keyed lowercase because normalize_category lowercases before the lookup. This is an
# ALIAS, not a retrain: the model, meta.json and the fitted vocabulary are untouched.
REVENUE_ALIASES={'$25,000,000 and greater':'$10,000,000 to $24,999,999'}

CHANNEL_ALIASES={'google':'google','google ads':'google','adwords':'google','google adwords':'google',
                 'meta':'meta','meta ads':'meta','facebook':'meta','fb':'meta','instagram':'meta','ig':'meta',
                 'bing':'bing','bing ads':'bing','microsoft':'bing','microsoft ads':'bing',
                 'tiktok':'tiktok','tiktok ads':'tiktok'}

def _to_amount(s):
    """'$2.3M' / '2,300,000' / '1.2m' -> float. None if it isn't a number."""
    t=str(s).strip().lower().replace('$','').replace(',','').replace(' ','')
    mult=1.0
    if t.endswith('k'): mult,t=1_000.0,t[:-1]
    elif t.endswith('m'): mult,t=1_000_000.0,t[:-1]
    try: return float(t)*mult
    except ValueError: return None

def normalize_revenue(v):
    """Raw revenue -> the exact band string the model expects. Returns
    (value, change_note, problem).

    Revenue is the one categorical that isn't a plain vocabulary lookup, because an
    amount has to be bucketed first. So: try the shared vocabulary match (which gets
    the exact bands, 'less than $250,000' in the wrong case, and Self-Serve
    Signup), then try reading it as an amount, then give up and flag it."""
    if _blank(v): return v,None,None
    s=str(v).strip()
    if s in REVENUE_PASSTHROUGH: return s,None,None      # a product, never an amount
    canon,note,problem=normalize_category('company_annual_revenue', s)
    if problem is None: return canon,note,None           # 1. an exact band, any case
    n=_to_amount(s)
    if n is not None and n>=0:                           # 2. an amount -> its band
        for lo,hi,band in REVENUE_BANDS:
            if lo<=n<hi:
                # '30M' buckets to the top band, which the model was never fit on — fold it
                # the same way a typed '$25,000,000 and greater' folds, so both routes in
                # land on a level the encoder knows.
                band=REVENUE_ALIASES.get(band.lower(),band)
                return band,f"{LABELS['company_annual_revenue']} '{s}' → {band}",None
    # DEMO: type Company revenue = "banana" -> lands here: flagged, confidence drops,
    # contributes nothing. Note the value is returned UNCHANGED — we never invent one.
    return v,None,problem

def normalize_category(field, v):
    """THE categorical normalizer. One rule for every category the model knows:
    trim the whitespace, apply the field's alias table if it has one, then match
    case-insensitively against the exact vocabulary the encoder was fit on.

    Returns (value, change_note, problem). A match returns the canonical spelling, so
    'low value' and 'eastern ' reach the model as 'Low Value' and 'Eastern' and cost
    nothing in confidence. A genuine unknown ('youtube') comes back untouched with a
    problem, so it still flags and still lowers confidence — the point is to stop
    punishing good values for their letter case, not to launder bad ones."""
    if _missing(field, v): return v,None,None
    raw=str(v).strip()                                   # 1. trim
    s=ALIASES.get(field,{}).get(raw.lower(), raw)        # 2. synonyms ('fb' -> 'meta')
    canon=CANON.get(field,{}).get(s.lower())             # 3. case-insensitive vocabulary
    label=LABELS.get(field,field)
    if canon is None:
        # DEMO: type Source = "youtube" -> here. Flagged, confidence drops, and the
        # encoder's handle_unknown='ignore' means it contributes zero rather than raising.
        # The value comes back untouched: we flag what we don't know, we never guess.
        return raw,None,f"{label} '{raw}' not recognized, so it is not used in the score"
    return canon,(f"{label} '{raw}' → {canon}" if canon!=raw else None),None

# ---------------------------------------------------------------------------
# State -> time zone. Lives HERE, not in app.py, because both intake paths need it:
# a typed lead and an uploaded row must reach the same time zone from the same state or
# the same lead scores two different ways depending on how it arrived. normalize_lead
# applies it, so every caller of score_lead gets it with no call-site of its own.
#
# The rule: a time_zone that is PRESENT wins, always. Only a blank/absent one is derived
# from the state. An upload that carries its own time_zone column is therefore untouched,
# which is why no lead with a populated time zone can move.
# ---------------------------------------------------------------------------
_TZ={'Eastern':'CT DE DC FL GA IN KY ME MD MA MI NH NJ NY NC OH PA RI SC VT VA WV',
     'Central':'AL AR IL IA KS LA MN MS MO NE ND OK SD TN TX WI',
     'Mountain':'CO ID MT NM UT WY','Arizona':'AZ','Pacific':'CA NV OR WA',
     'Alaska':'AK','Hawaii':'HI'}
STATE_TZ={s:z for z,ss in _TZ.items() for s in ss.split()}

# Full state names -> the two-letter code the rest of the tool speaks. A rep types
# "Florida"; a CRM exports "FL"; both must reach the same time zone.
STATE_NAMES={
 'alabama':'AL','alaska':'AK','arizona':'AZ','arkansas':'AR','california':'CA',
 'colorado':'CO','connecticut':'CT','delaware':'DE','district of columbia':'DC',
 'florida':'FL','georgia':'GA','hawaii':'HI','idaho':'ID','illinois':'IL','indiana':'IN',
 'iowa':'IA','kansas':'KS','kentucky':'KY','louisiana':'LA','maine':'ME','maryland':'MD',
 'massachusetts':'MA','michigan':'MI','minnesota':'MN','mississippi':'MS','missouri':'MO',
 'montana':'MT','nebraska':'NE','nevada':'NV','new hampshire':'NH','new jersey':'NJ',
 'new mexico':'NM','new york':'NY','north carolina':'NC','north dakota':'ND','ohio':'OH',
 'oklahoma':'OK','oregon':'OR','pennsylvania':'PA','rhode island':'RI',
 'south carolina':'SC','south dakota':'SD','tennessee':'TN','texas':'TX','utah':'UT',
 'vermont':'VT','virginia':'VA','washington':'WA','west virginia':'WV','wisconsin':'WI',
 'wyoming':'WY'}
STATE_CODES=set(STATE_NAMES.values())

def normalize_state(v):
    """'  florida ' / 'Florida' / 'fl' -> 'FL'. None if it isn't a state we know.

    Trimmed and case-insensitive, like every other normalizer in the tool. Anything
    unrecognized comes back None and the state is simply absent — we never guess."""
    s=str(v or '').strip()
    if not s: return None
    if s.upper() in STATE_CODES: return s.upper()
    return STATE_NAMES.get(s.lower())

def time_zone_for(state):
    """Time zone from a state, or None if we can't tell — in which case the field is
    simply missing, exactly as a blank time zone has always been.

    # DEMO: State = TX -> Central appears in "what you gave it", marked (from state).
    State = ZZ -> None, and the time zone is simply absent."""
    return STATE_TZ.get(normalize_state(state) or '')

# Fields normalized by the shared rule above, and the alias tables that feed it.
# Aliases map a synonym to a canonical spelling; the vocabulary match then handles case.
CATEGORICALS=['channel','icp_category','utm_medium','time_zone']
ALIASES={'channel':CHANNEL_ALIASES,'company_annual_revenue':REVENUE_ALIASES}

# Revenue is the one categorical that isn't a vocabulary lookup — an amount has to be
# bucketed into a band first — so it keeps its own normalizer. Everything else shares one.
NORMALIZERS=([('company_annual_revenue',normalize_revenue)]+
             [(f, functools.partial(normalize_category, f)) for f in CATEGORICALS])

def normalize_lead(lead):
    """Canonicalize a lead before scoring. Returns (lead, notes, problems), where
    problems maps field -> why it couldn't be resolved. score_lead calls this, so
    the typed single-lead path and the CSV batch path get the identical treatment —
    there is no second normalization anywhere."""
    out=dict(lead); notes=[]; problems={}
    # A present time_zone always wins; only a blank/absent one is derived from the state.
    # This is the ONE place it happens, so a typed lead and an uploaded row with the same
    # state reach the same zone. A row that carries its own time_zone cannot move.
    if _blank(out.get('time_zone')):
        tz=time_zone_for(out.get('state'))
        if tz:
            out['time_zone']=tz
            notes.append(f"{LABELS['time_zone']} {tz} (from state)")
    for field,fn in NORMALIZERS:
        out[field],note,prob=fn(out.get(field))   # out, not lead: time_zone may just have been set
        if note: notes.append(note)
        if prob: problems[field]=prob
    return out,notes,problems

# ---------------------------------------------------------------------------
# One rule for confidence and flags: a field is UNUSABLE if the model couldn't
# use it. Three ways that happens — (a) it's blank, (b) normalization couldn't
# recognize it, or (c) it's a valid value the model never saw at fit time, which
# the encoder silently ignores (handle_unknown='ignore'). All three cost the same
# confidence and all three get a flag, so nothing is dropped quietly.
#
# Case (b) now fires only on values normalize_category genuinely could not resolve:
# a known value in the wrong case or with stray whitespace is canonicalized upstream
# and costs nothing. LABELS, KNOWN and CANON are defined at the top of this file.
# ---------------------------------------------------------------------------
def _unusable(lead, problems):
    """-> [(field, flag)] for every key field the model couldn't use. One reason per
    field, first match wins, so an unknown channel isn't counted twice."""
    out=[]
    for f in KEY_FIELDS:
        v=lead.get(f)
        if _missing(f,v): out.append((f,f"missing {LABELS[f]}"))
        elif f in problems: out.append((f,problems[f]))
        elif f in KNOWN and str(v) not in KNOWN[f]:
            out.append((f,f"{LABELS[f]} '{v}' not recognized, so it is not used in the score"))
    # Prior score: blank, unparseable and OUT OF RANGE all cost the same level. The third
    # is the fix — 99999 used to sail through here with no flag at all, then saturate the
    # model and take the top of the queue at High confidence.
    val,prob=validate_number('legacy_score', lead.get('legacy_score'))
    if val is None:                                   # imputed to the median when unusable
        out.append(('legacy_score', prob or f"missing {LABELS['legacy_score']}"))
    # Fields the MODEL never reads. A blank one costs nothing, because it cost the score
    # nothing — but a value that is PRESENT and corrupt is still a reason to trust the row
    # less, so it lowers confidence exactly like any other field that failed validation.
    for f in NON_FEATURE_CHECKS:
        if _blank(lead.get(f)): continue
        _v,prob=validate_number(f, lead.get(f))
        if prob: out.append((f,prob))
    prob=validate_date(lead.get('created_at'))
    if prob: out.append(('created_at',prob))
    # State, and this one is DISPLAY ONLY. The model never reads the state itself, only
    # whether it is absent, so 'Banana' cannot move a score by a hair - which is exactly
    # why it slipped through: a garbage state used to render identically to Texas, at High
    # confidence, with nothing on the row to look at. Until now state was only ever checked
    # when it drove time-zone derivation, so a lead carrying both a junk state AND an
    # explicit time zone was never validated at all.
    #
    # A row whose state is not a state is a row to trust less, the same argument the zip3
    # check above makes. state_missing is untouched: a present-but-wrong state is still
    # PRESENT, so the feature stays 0 and the score does not move. Only the flag is new.
    st=lead.get('state')
    if not _blank(st) and normalize_state(st) is None:
        out.append(('state', f"{LABELS['state']} '{str(st).strip()}' is not a US state, "
                             "so it is not used in the score"))
    return out

def _prep_dict(lead):
    """One lead -> the plain dict the model's columns are read from.

    Split out of _prep_row so a batch can build ONE DataFrame from many of these rather
    than one DataFrame per lead. Same keys, same values, same order either way, which is
    what makes the batched score identical to the per-row score."""
    r={c: lead.get(c) for c in INTAKE}
    r['utm_medium']=str(r.get('utm_medium')).lower().strip()
    # ONE rule for both intakes, and it is the rule the model was fit on
    # (train_and_save.py: df['state'].isna()). This used to be forced to 0 for the typed
    # form, on the theory that a rep leaving the box empty means "I didn't type it" rather
    # than "no state on file". That theory cost more than it bought: the same lead scored
    # 38% Warm typed and 3.4% Cold uploaded, so the tool contradicted itself on the same
    # record. A blank state is a trained signal (those leads won 1.7%), so it is read the
    # same way whoever supplies it, and the UI says so rather than hiding it.
    r['state_missing']=1 if _blank(r.get('state')) else 0
    # A real level reaches the encoder as itself; only a genuine hole becomes 'nan'.
    for c in ['channel','icp_category','company_annual_revenue','time_zone']:
        r[c]=str(r.get(c)) if not _missing(c, r.get(c)) else 'nan'
    # Validate BEFORE the model sees it: anything blank, unparseable or outside 0-100 is
    # replaced by the neutral median rather than passed through as if it were real. An
    # out-of-domain 99999 must never reach predict_proba — clamping the OUTPUT to 100
    # afterwards is no use, because by then the row has already won the ranking.
    x,_prob=validate_number('legacy_score', r.get('legacy_score'))
    if x is None: x=LEG_MED
    lo,hi=NUMERIC_DOMAINS['legacy_score']
    r['legacy_score']=min(max(x,lo),hi)   # belt and braces: the rail is now unreachable
    return r

def _prep_row(lead):
    """The single-lead shape: exactly _prep_dict, wrapped in a one-row frame."""
    return pd.DataFrame([_prep_dict(lead)])

# ---------------------------------------------------------------------------
# THE tier vocabulary — one list, one source of truth. A tier's name, its action
# line and its colour all hang off `key`, and every part of the tool reads from
# here: the verdict card, the tuning strip, the top-bar legend, the ranked table
# and the CSV export. `key` doubles as the CSS class, so the palette in the
# stylesheet's :root block is keyed by the same four words. Order is hottest ->
# coldest and is the ranking order everywhere.
# ---------------------------------------------------------------------------
TIERS=[{'key':'hot', 'name':'Hot',  'action':'Call until you reach them'},
       {'key':'warm','name':'Warm', 'action':'Call once, then sequence'},
       {'key':'cool','name':'Cool', 'action':'Sequence only'},
       {'key':'cold','name':'Cold', 'action':'No outbound, inbound only'}]
TIER={t['key']:t for t in TIERS}
TIER_ORDER=[t['key'] for t in TIERS]
CUT_KEYS=TIER_ORDER[:-1]      # the three movable boundaries; the last tier is the floor

# meta.json keys its cutoffs by these same tier keys, written by train_and_save.py.
# These are the DEFAULT boundaries; the UI's tuning strip overrides them per request.
DEFAULT_CUTOFFS={k:float(v) for k,v in META['tier_cutoffs_abs'].items()}

def tier_for(score, cutoffs=None):
    """score -> tier key. The first tier whose cutoff the score clears; the coldest tier
    is the floor, so every score lands somewhere and no lead can fall out.

    Cutoffs are an ARGUMENT, never read from global state here. app.APPLIED holds the
    ones in force and only Manager · Calibration writes it, so re-tiering a whole board
    is this function over stored scores — the model never runs twice."""
    c=cutoffs or DEFAULT_CUTOFFS
    for k in CUT_KEYS:
        if score>=c[k]: return k
    return TIER_ORDER[-1]

# One confidence rule, unchanged: count the fields the model couldn't use.
CONF_LEVELS=['High','Medium','Low']
def confidence(n_unusable):
    """(level, plain-English footnote) for n fields the model couldn't use.

    The whole rule, in three lines: count the fields, don't weigh them. A field is
    unusable when it is blank, unrecognized, or a value the model never saw — all three
    cost the same, because to the model they are the same: no signal."""
    if n_unusable==0: return ('High','Scored on everything you gave it')
    if n_unusable==1: return ('Medium',"1 field the model couldn't use")
    return ('Low',f"{n_unusable} fields the model couldn't use")

def _why(lead):
    """The explanation: each field's real historical close rate against the 16.5% base.

    Deliberately NOT the model's coefficients. The logistic is correlated, so it hands
    High Value a negative coefficient even though High Value closes at 30% — true to the
    math and impossible to defend to a rep. Segment rates are both."""
    out=[]
    def add(label, value, info):
        if not info: return
        d=info['win_rate']-BASE
        out.append({'factor':f"{label}: {value}",'win_rate':info['win_rate'],
                    'delta':round(d,3),'arrow':'▲' if d>0 else '▼'})
    for col in ['channel','icp_category','company_annual_revenue']:
        add(LABELS[col], lead.get(col), FACTS.get(col,{}).get(str(lead.get(col))))
    # State earns a row for its PRESENCE, not its value: Texas and Ohio are not levels the
    # model learned, but "no state on file" is, and it is usually the biggest mover on the
    # panel. Shown either way — a rep who sees the score fall off a cliff for leaving one
    # box empty has to be able to read why, and showing it only when it hurts would be a
    # panel that flatters the lead. See _prep_dict for why a blank state is a real signal.
    present='on file' if not _blank(lead.get('state')) else 'missing'
    add(LABELS['state'], present, FACTS.get('state',{}).get(present))
    out.sort(key=lambda x: abs(x['delta']), reverse=True); return out

def _assemble(lead, normalized, problems, score, cutoffs):
    """A computed score -> the result dict every caller renders. Steps 5-7 of the header.

    Shared by score_lead and score_leads so the batched path cannot drift from the
    per-row one: only WHERE the number comes from differs, never what is made of it."""
    unusable=_unusable(lead, problems)             # what the model can't use, and why
    flags=[flag for _,flag in unusable]
    # warnings = normalization problems on fields the confidence rule does NOT count, so a
    # value like an unrecognized time_zone is still reported without costing a level.
    #
    # It is NOT "everything outside KEY_FIELDS". Confidence counts whatever _unusable
    # returned (see the call below), and that includes legacy_score, the NUMERIC_DOMAINS
    # checks on zip3, and a bad created_at — none of which are KEY_FIELDS. A corrupt zip3
    # lowering confidence is deliberate: _unusable says so where it appends it.
    warnings=[p for f,p in problems.items() if f not in KEY_FIELDS]
    key=tier_for(score, cutoffs)
    conf,conf_note=confidence(len(unusable))
    return {'lead_id':lead.get('lead_id'),'score':round(score,4),'tier':key,
            'tier_name':TIER[key]['name'],'action':TIER[key]['action'],
            'why':_why(lead),'normalized':normalized,
            'confidence':conf,'confidence_note':conf_note,
            'unusable':[f for f,_ in unusable],'flags':flags,'warnings':warnings}

def _failed(lead, normalized, problems, reason):
    """A row the model could not take, as a result with the reason attached. Nothing is
    ever dropped for failing — it comes back and the caller renders it in place."""
    unusable=_unusable(lead, problems)
    return {'error':reason,'lead_id':lead.get('lead_id'),'why':[],
            'normalized':normalized,'flags':[f for _,f in unusable],
            'warnings':[p for f,p in problems.items() if f not in KEY_FIELDS]}

def score_lead(lead, cutoffs=None):
    # The seven steps in the file header, in order.
    cutoffs=cutoffs or DEFAULT_CUTOFFS
    lead,normalized,problems=normalize_lead(lead)  # canonicalize before anything reads the fields
    try:
        # The score itself: P(this lead becomes a won deal), intake features only.
        score=float(MODEL.predict_proba(_prep_row(lead))[:,1][0])
    except Exception as e:
        return _failed(lead, normalized, problems, f"could not score: {e}")
    return _assemble(lead, normalized, problems, score, cutoffs)

def score_leads(leads, cutoffs=None):
    """Score MANY leads with ONE predict_proba call. Same numbers as score_lead.

    Identical by construction: the same normalize_lead, the same _prep_dict, the same
    _assemble. Only the model call is batched, and that is the whole cost — 4,255 leads
    were ~6s of one-row-at-a-time predict_proba, which is also what made a 50k upload look
    hung. Batched, the same 4,255 land in well under a second.

    Resilience is preserved, and it is the part that must never regress: if the batched
    call raises for ANY reason, every lead is re-scored one at a time through score_lead,
    which isolates a bad row behind its own try. One unscorable row can slow the file
    down; it can never take the other 4,254 with it."""
    cutoffs=cutoffs or DEFAULT_CUTOFFS
    prepped=[]                       # [norm, notes, problems, row|None, error|None]
    for lead in leads:
        try:
            norm,notes,problems=normalize_lead(lead)
            prepped.append([norm,notes,problems,_prep_dict(norm),None])
        except Exception as e:       # normalization itself failed: still ships, with why
            prepped.append([lead,[],{},None,f"could not score: {e}"])

    live=[p for p in prepped if p[4] is None]
    scores={}
    if live:
        try:
            probs=MODEL.predict_proba(pd.DataFrame([p[3] for p in live]))[:,1]
            scores={id(p):float(s) for p,s in zip(live,probs)}
        except Exception:
            # The batch is all-or-nothing, so one poisonous row would cost every other
            # row its score. Drop back to the per-row path — and wrap each call, because
            # the whole point of falling back is that ONE row is expected to fail. An
            # unguarded comprehension here would let it take the file down after all.
            out=[]
            for l in leads:
                try: out.append(score_lead(l, cutoffs))
                except Exception as e:
                    out.append({'error':f"could not score: {e}",'lead_id':l.get('lead_id'),
                                'why':[],'normalized':[],'flags':[],'warnings':[]})
            return out

    out=[]
    for p in prepped:
        norm,notes,problems,_row,err=p
        out.append(_failed(norm,notes,problems,err) if err is not None
                   else _assemble(norm,notes,problems,scores[id(p)],cutoffs))
    return out

if __name__=='__main__':
    b={'channel':'google','icp_category':'High Value','company_annual_revenue':'$1,000,000 to $4,999,999','legacy_score':'60'}
    r=score_lead(dict(b,state='',utm_medium=''))
    print("blank state                 :", r['score'], r['tier_name'], '->', r['action'])
    print("same lead with a state      :", score_lead(dict(b,state='TX',utm_medium='brand'))['score'])
    j=score_lead({'lead_id':'JUNK'})
    print("totally blank               :", j['tier_name'], "| conf:", j['confidence'])
