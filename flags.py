"""What a flag MEANS, separated from how the page draws it.

Every problem the tool finds on a lead arrives as a sentence: "missing Source", "Fit
'Banana' not recognized", "Created 'not a date' is not a valid date". Those sentences are
written in scorer.py (and, for row-level problems, in csv_io.read_leads). This module is
the one place that reads them back and decides what kind of problem each one is.

Two questions get asked of a flag, and they are different:
  _flag_kind      which bucket does it belong to in the run summary's Details breakdown
  _flag_severity  is it a MISSING field or a BAD value, which is what the board marks

Split out of app.py so the classification can be read and tested without a Flask app
around it. Behavior is unchanged; the functions moved verbatim.
"""
import scorer

# Flag text -> the bucket name shown in the run summary's Details. Keyed off the
# labels, so renaming a field renames its bucket too.
_KINDS=([('duplicate lead_id','duplicate lead ID'),('no lead_id','missing lead ID'),
         ('extra cell','ragged row'),('fewer cell','ragged row'),
         ('not recognized','unfamiliar value')]
        +[(f'missing {lbl}', f'missing {lbl}') for lbl in scorer.LABELS.values()])

def _flag_kind(flag):
    for needle,kind in _KINDS:
        if needle in flag: return kind
    return 'other'

# The two-way split the board shows: was a field ABSENT, or was its value GARBAGE?
# The tool has always known the difference - it is in the flag text - but the ranked list
# collapsed both into one confidence pill, so a rep could not tell "nobody filled this in"
# from "the export wrote junk into that box". Those want different responses: one is a gap
# to fill, the other is a row to distrust.
#
# The neutral set is enumerated; everything else is bad. scorer writes exactly one shape of
# absence flag, f"missing {LABELS[field]}", and read_leads writes one more for a row with no
# id at all. Anything else is a value that failed validation.
_MISSING_FLAGS=tuple([f'missing {lbl}' for lbl in scorer.LABELS.values()]+['no lead_id'])
BAD, MISSING = 'bad', 'missing'

def _flag_severity(flag):
    """One flag -> BAD or MISSING.

    Defaults to BAD deliberately. This couples the UI to how scorer words its flags, and if
    that wording drifts the safe failure is to over-mark: an unrecognized string is far more
    likely to be a value problem than an absence, and shouting about a clean row costs a
    rep a second, while staying quiet about a corrupt one costs them a call.
    test_every_flag_the_scorer_writes_classifies_correctly is what holds the coupling."""
    return MISSING if any(n in flag for n in _MISSING_FLAGS) else BAD

def _worst_severity(flags):
    """The one marker a row gets. BAD outranks MISSING, so a lead with both reads as the
    more serious of the two; the hover text still lists every problem."""
    if not flags: return None
    return BAD if any(_flag_severity(f)==BAD for f in flags) else MISSING

def _row_flags(r):
    """Every problem on one result, in the order the row shows them. One definition, used
    by the table, the summary counts and the export, so they cannot disagree."""
    return r.get('flags',[])+r.get('warnings',[])+r.get('row_notes',[])
