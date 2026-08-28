"""Bytes off an upload -> one dict per lead, plus a note on anything odd.

THE ERROR POLICY, which is the whole point of this module. It raises ValueError for
exactly three FILE-level problems - empty, header-only, no recognizable column - and for
nothing else. Every other mess is a note attached to the row, and the row still ships:
encoding, delimiter, header case, unknown or missing columns, ragged rows, blank rows,
duplicate or absent ids. One input row, one output row, always.

Field VALUES are not touched here. scorer.normalize_lead owns those, for both the typed
form and the upload, so the two intakes cannot disagree about what a value means.

Split out of app.py because none of this needs a request or an app: it is bytes in,
structures out. Behavior is unchanged; the functions moved verbatim, with the header map
passed in rather than read off a module global.
"""
import collections
import csv
import io
import re


def _norm_header(h):
    """'  ICP Category ' / 'icp-category' -> 'icp_category'.

    # DEMO: mis-case or space out a header and the column is still found. The header map
    accepts both scorer's field names and the short URL params, in any case."""
    return re.sub(r'[^a-z0-9]+','_',str(h).strip().lower()).strip('_')

def build_header_map(fields):
    """Every spelling of a column we accept -> the field name scorer expects. Both the
    scorer's own names and the short form-param names work, in any case."""
    header_map={}
    for _f in fields:
        header_map[_norm_header(_f['field'])]=_f['field']
        header_map[_norm_header(_f['param'])]=_f['field']
    header_map.update({'lead_id':'lead_id','id':'lead_id','leadid':'lead_id'})
    # created_at and zip3 are INGESTED but are deliberately NOT FIELDS: neither is a model
    # feature (there is no recency term, and zip3 was never fit on), so neither is shown as
    # an intake row or asked for on the form. They are read for one reason - so a corrupt
    # value can be validated and cost a confidence level instead of passing silently.
    # Without this they would be dropped at the header and the validation could never fire
    # on a CSV. See scorer.validate_date / scorer.NON_FEATURE_CHECKS.
    header_map.update({'created_at':'created_at','createdat':'created_at','created':'created_at',
                       'zip3':'zip3','zip_3':'zip3','zip':'zip3'})
    return header_map

def _decode(raw):
    """Bytes -> text. Tried in order; utf-8-sig first so a BOM is eaten rather than glued
    to the first header, latin-1 last because it CANNOT fail — a mojibake lead beats a
    dead run. This is why an Excel export never takes the whole upload down."""
    for enc in ('utf-8-sig','utf-8','cp1252','latin-1'):
        try: return raw.decode(enc)
        except UnicodeDecodeError: continue
    return raw.decode('latin-1','replace')

def _dialect(sample):
    """Comma, semicolon, tab or pipe — whatever the export used. Sniffed, not assumed:
    a European CRM exports semicolons and would otherwise land as one giant column."""
    try: return csv.Sniffer().sniff(sample, delimiters=',;\t|')
    except csv.Error: return csv.excel

def _rows(rdr):
    """Yield the reader's rows, turning a csv.Error into the file-level ValueError that
    read_leads promises its callers.

    csv raises when one field exceeds the size cap, and an UNTERMINATED QUOTE reaches that
    cap by swallowing the rest of the file into a single field. Uncaught it left read_leads
    entirely — past every `except ValueError` on the way out — and surfaced as a 500. It is
    a bad file, which this tool answers with a sentence, not a stack trace."""
    try:
        for row in rdr: yield row
    except csv.Error as e:
        raise ValueError(f'that file could not be parsed ({e}). An unclosed quote mark '
                         'is the usual cause — one " with no partner swallows '
                         'everything after it') from e

def read_leads(raw, header_map, field_names):
    """bytes -> (leads, report). Each lead is (lead_dict, [reasons]).

    The whole error policy lives here: this raises ValueError for exactly three FILE-level
    problems — empty, header-only, no recognizable column — and for nothing else. Every
    other mess (encoding, delimiter, header case, unknown or missing columns, ragged rows,
    blank rows, duplicate or absent ids) is a note on the row and the row still ships.
    Field VALUES are not touched here; scorer.normalize_lead owns those for both paths."""
    text=_decode(raw)
    if not text.strip(): raise ValueError('that file is empty')
    rdr=_rows(csv.reader(io.StringIO(text), _dialect(text[:4096])))
    header=next(rdr, None)
    if header is None: raise ValueError('that file is empty')
    cols=[header_map.get(_norm_header(h)) for h in header]
    if not any(cols):
        raise ValueError('no recognizable columns in the header. Expected some of: '
                         +', '.join(sorted(field_names))+', lead_id')
    missing=sorted(field_names-set(filter(None,cols)))
    ignored=[h for h,c in zip(header,cols) if c is None]

    leads=[]; blank=0; seen=collections.Counter()
    for lineno,row in enumerate(rdr, start=2):
        if not any(str(c).strip() for c in row):        # a wholly empty line is not a lead
            blank+=1; continue
        why=[]
        # DEMO: ragged rows. Extra cells past the header are dropped, missing ones read as
        # blank — either way the row survives with a note. Nothing is skipped for shape.
        if len(row)>len(cols):
            why.append(f'row had {len(row)-len(cols)} extra cell(s) beyond the header, ignored')
        elif len(row)<len(cols):
            why.append(f'row had {len(cols)-len(row)} fewer cell(s) than the header, treated as blank')
        d={c:v for c,v in zip(cols,row) if c}
        # DEMO: delete a lead_id -> the row still ships, but as UNSCORED, never as a
        # callable lead. A row with no id cannot be dialled, chased or reconciled back to
        # the CRM, and a fabricated 'row-9' would rank it among real leads as though it
        # were one. It keeps its blank id and score_row() routes it to the couldn't-score
        # bucket with the reason attached. Nothing is dropped; it just isn't callable.
        lid=str(d.get('lead_id') or '').strip()
        if not lid:
            why.append(f'no lead_id on line {lineno}, so this row could not be scored')
        # DEMO: duplicate a lead_id -> BOTH rows are kept and both are flagged. Silently
        # collapsing them would lose a lead, which is the one thing this must never do.
        seen[lid]+=1
        if seen[lid]>1: why.append(f'duplicate lead_id, kept as a separate lead')
        d['lead_id']=lid
        leads.append((d,why))
    if not leads: raise ValueError('that file has a header but no rows')
    # 'header' is the NORMALIZED header, in file order: what this module made of row 1
    # before any of it was mapped to a field name. It is here for run history, which
    # fingerprints the header SET so a reordered or recased export reads as the same
    # schema and a renamed or dropped column does not -- see runs.schema_fingerprint.
    # Normalized rather than raw so the one definition of "same column name" lives in
    # _norm_header, and in file order rather than sorted because ordering the set is the
    # fingerprint's job, not this module's.
    return leads, {'blank':blank,'missing_cols':missing,'ignored_cols':ignored,
                   'header':[_norm_header(h) for h in header]}
