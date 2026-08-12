"""Turning scored results into the shapes a template can print.

Nothing here decides anything. Every function takes a result dict (or a cutoff dict) and
returns strings and numbers laid out the way the page wants them: percentages rounded the
way the UI rounds, deltas signed, rows paired with their headings.

Split out of app.py because none of it needs a request, an app, or any module state -
which makes it the part of the display layer that can be read and checked on its own.
Behavior is unchanged; the functions moved verbatim.
"""
import scorer

import flags

# What a rep calls each field. scorer owns the vocabulary; this is the same dict under the
# name the display code uses, so there is no second copy to drift.
WHY_KEY=scorer.LABELS
# The fields the model actually weighs for confidence: the three key fields + legacy score.
SCORE_FIELDS_TOTAL=len(scorer.KEY_FIELDS)+1

_SMALL={'of'}   # 'district of columbia' -> 'District of Columbia', not 'District Of Columbia'
def _title(s): return ' '.join(w if w in _SMALL else w.capitalize() for w in s.split())

def _rate_pct(x):
    """A close rate as a percent. Whole numbers, except below 10%, where a decimal is
    the difference between a real number and a shrug: a missing state closes at 1.7% and
    rounding that to 2% is a 15% error on the biggest mover the panel ever shows."""
    v=x*100
    return f'{v:.1f}' if v<10 else round(v)

def _why_rows(r):
    """(heading, value, close-rate %, signed delta in points, negative?) per why entry.
    scorer gives 'Channel: google' and a probability delta; this only reformats them."""
    out=[]
    for w in r.get('why',[]):
        k,_,v=w['factor'].partition(': ')
        p=round(w['delta']*100,1)
        out.append((k,v,_rate_pct(w['win_rate']),('−' if p<0 else '+')+f"{abs(p)} pts",p<0))
    return out

def _unused_rows(r):
    """(heading, reason, severity) for every field the model couldn't use. score_lead builds
    'unusable' and 'flags' from one list, so the two stay index-aligned.

    Severity rides along so the single-lead panel can mark a corrupt value differently from
    an absent one, in the same two chips the ranked board uses. A rep who learns the marker
    on the board should not have to learn a second vocabulary here."""
    return [(WHY_KEY.get(f,f), why, flags._flag_severity(why))
            for f,why in zip(r.get('unusable',[]),r.get('flags',[]))]

def _usable(r): return SCORE_FIELDS_TOTAL-len(r.get('unusable',[]))

def _pct(cut):
    """0-1 cutoffs -> whole win-out-of-100 numbers. Everything the manager and the rep
    read is a whole number: no decimals anywhere in the UI."""
    return {k:int(round(v*100)) for k,v in cut.items()}
