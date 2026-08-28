"""One row per scoring run, and nothing else.

WHY THIS EXISTS. The failure this repo is built around is a confident board assembled out
of almost no signal, and the tool already refuses that file at the moment it arrives. What
it could not do until now is see the SLOW version of the same failure: an export whose
columns drift a little each month, coverage sliding 0.98 -> 0.86 -> 0.71 across runs that
each looked acceptable on their own. No single run can notice that. A row per run can.

THE POSTURE IS sf_configured's, deliberately. RUN_HISTORY_DB unset is a fully supported
state: no store, no writes, and /health says it has no data. Nothing else about the app
changes, because a logging failure is not a scoring failure -- record() swallows every
exception it can produce, and recent() answers "unavailable" rather than raising into a
route. A rep uploading a file gets their board whether or not the disk is mounted.

DECIDES NOTHING. What counts as a schema change, or as a coverage regression worth
flagging, is a question about this app's thresholds and lives in app.py next to
MATCH_NOTICE. This module hashes a header set, appends a row, and reads rows back.
"""
import datetime
import hashlib
import logging
import os
import sqlite3

from csv_io import _norm_header

log=logging.getLogger('lead_scorer')

# The one switch. Read fresh on every call rather than captured at import, the same way
# _ctx checks the Salesforce env vars per request: setting the variable and restarting is
# the whole configuration step, and the test suite can point it at a temp file without
# reimporting anything.
ENV_VAR='RUN_HISTORY_DB'

# How many rows a read pulls back before the caller groups them. Runs are one-per-upload,
# so a few hundred is months of a demo's history and still one small query.
RECENT_LIMIT=400

# Enough hex to make a collision between two column sets a non-event, short enough to read
# off the page and compare by eye, which is the actual job: "is this the same schema as
# last week" is a question somebody answers by looking.
FINGERPRINT_CHARS=12

def schema_fingerprint(columns):
    """A header SET -> a short stable hash. '' for no columns at all.

    Normalized, de-duplicated, sorted -- in that order, and all three matter. A reordered
    export and a recased one are the same schema and MUST produce the same string, or
    every run would read as a schema change and the signal would be worthless. A renamed
    or dropped column is a different set and a different string, which is the entire
    point: the renamed-column failure in the README moves no other number this tool
    records, because every row still scores and the board still looks fine.

    Not a hash of the FILE. Two exports a month apart share a fingerprint if their columns
    match, however different their rows are.

    Joined on a newline because _norm_header maps every non-alphanumeric character to '_',
    so a newline cannot occur inside a name -- two different sets cannot collide by
    running together at the seam."""
    names=sorted({_norm_header(c) for c in columns if str(c).strip()})
    if not names:
        return ''
    return hashlib.sha256('\n'.join(names).encode('utf-8')).hexdigest()[:FINGERPRINT_CHARS]

def db_path():
    """The configured store path, or '' for none."""
    return (os.environ.get(ENV_VAR) or '').strip()

def configured():
    """Whether a store is asked for at all. Distinct from whether it works -- recent()
    answers that -- so /health can tell "nobody turned this on" apart from "the disk is
    gone", which want different responses from whoever is reading the page."""
    return bool(db_path())

_DDL="""
CREATE TABLE IF NOT EXISTS runs(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  at          TEXT    NOT NULL,
  source      TEXT    NOT NULL,
  fingerprint TEXT    NOT NULL,
  coverage    REAL    NOT NULL,
  band        TEXT    NOT NULL,
  rows_in     INTEGER NOT NULL,
  scored      INTEGER NOT NULL,
  failed      INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS runs_by_source ON runs(source, id);
"""

_COLUMNS=('id', 'at', 'source', 'fingerprint', 'coverage', 'band',
            'rows_in', 'scored', 'failed')

def _connect():
    """A connection with the table in place, or None when no store is configured.

    Opened per call rather than held open: a run is written once per ranked board, this is
    served by gunicorn with --workers 1 --threads 8 (see render.yaml), and a per-call
    connection has no thread affinity to get wrong. CREATE TABLE IF NOT EXISTS on every
    open is the whole migration story -- two statements against a table that already
    exists, on a path hit once per upload.

    The directory is NOT created. A path whose parent is missing is an operator getting the
    configuration wrong or a disk that failed to mount, and both want to surface on /health
    as "configured and unreadable" rather than be papered over by this quietly making a
    directory somewhere nobody meant.

    timeout=5 because --threads 8 means two uploads can finish at once and SQLite takes one
    writer at a time; five seconds is far past what a sub-millisecond insert needs and the
    write is expendable anyway if it is somehow not enough.

    Raises whatever sqlite3 raises. Both callers below catch; this one stays honest so the
    failure is visible in the log rather than turning into a silent no-op here."""
    path=db_path()
    if not path:
        return None
    con=sqlite3.connect(path, timeout=5)
    con.row_factory=sqlite3.Row
    con.executescript(_DDL)
    return con

def record(source, fingerprint, coverage, band, rows_in, scored, failed):
    """Append one run. True if it was written, False if it was not.

    NEVER RAISES, and it is the reason this module exists in the shape it does. A disk
    that is full, unmounted, read-only or locked costs the run its history entry and
    nothing else. Every other function in this repo that swallows an exception is doing it
    to keep one bad row from killing a file; this one is doing it to keep bookkeeping from
    killing the work it was only ever meant to describe."""
    con=None
    try:
        con=_connect()
        if con is None:
            return False
        with con:
            con.execute(
                'INSERT INTO runs(at,source,fingerprint,coverage,band,rows_in,scored,failed)'
                ' VALUES(?,?,?,?,?,?,?,?)',
                (datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
                 str(source), str(fingerprint), float(coverage), str(band),
                 int(rows_in), int(scored), int(failed)))
        return True
    except Exception:
        log.warning('run history write failed; the scoring run is unaffected', exc_info=True)
        return False
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass

def recent(limit=RECENT_LIMIT):
    """The newest runs, newest first, as plain dicts -- or None if there is no store.

    None and [] are different answers and /health says so out loud. None is "nothing is
    configured, or the store could not be opened"; [] is "the store is there and nothing
    has run yet". Collapsing the two would let an unmounted disk read as a quiet week,
    which is the exact class of silent-but-fine that this feature exists to catch."""
    con=None
    try:
        con=_connect()
        if con is None:
            return None
        rows=con.execute(
            f'SELECT {",".join(_COLUMNS)} FROM runs ORDER BY id DESC LIMIT ?',
            (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        log.warning('run history read failed', exc_info=True)
        return None
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
