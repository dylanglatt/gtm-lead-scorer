"""Fits the model and writes the two artifacts the app loads at import.

    data/leads_train.csv  ->  model.joblib   the fitted pipeline
                          ->  meta.json      base rate, segment win rates, tier cutoffs

Run make_synthetic_data.py first; this file never invents data, it only fits what is
there. Both artifacts are committed, so a fresh clone runs offline and nobody needs to
retrain to try the tool.

THREE THINGS HERE ARE LOAD-BEARING AND MUST NOT DRIFT.

  The pipeline SHAPE. scorer._fit_categories reaches into named_steps['pre'] and the
  transformer named 'cat' to recover the exact vocabulary the encoder was fit on, which is
  how the tool knows an unfamiliar value when it sees one. Rename either and the tool
  silently falls back to meta.json's segment keys.

  handle_unknown='ignore'. A value the model never saw contributes zero instead of
  raising. That is the whole reason an unseen lead still scores, and scorer.py's contract
  is written around it.

  prep(). It is the same row layout as scorer._prep_dict, written out again here because
  fitting a model must not require a fitted model to import. The two are pinned together
  by test_train_prep_matches_scorer_prep — if you change one, that test fails.

Tier cutoffs are DERIVED, not chosen: tier_thresholds are percentiles, and the absolute
score at each percentile of the training distribution is what gets written to
tier_cutoffs_abs. Hot is the top 10% of leads, by construction.
"""
import json
import math
import os
import re

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

# The one thing imported from the generator: how a segment's win rate is counted. The
# numbers written into meta.json are then the same numbers the generator checks for
# separation from the superseded dataset — one computation, so they cannot disagree.
from make_synthetic_data import TRAIN_PATH, segment_win_rates

_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(_DIR, 'model.joblib')
META_PATH = os.path.join(_DIR, 'meta.json')

CATEGORICAL = ['channel', 'icp_category', 'company_annual_revenue', 'utm_medium',
               'time_zone']
NUMERIC = ['legacy_score', 'state_missing']
FEATURES = CATEGORICAL + NUMERIC

# Percentiles, not scores. A manager moves the boundaries in Manager · Calibration; these
# are only where they start, and they start at "the top 10% are Hot".
TIER_THRESHOLDS = {'hot': 0.90, 'warm': 0.70, 'cool': 0.35}

LEGACY_DOMAIN = (0.0, 100.0)          # scorer.NUMERIC_DOMAINS['legacy_score']
_PLAIN = re.compile(r'[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)$')


def _blank(v):
    return v is None or str(v).strip() in ('', 'nan')


def _legacy_value(v):
    """A usable prior score, or None. The same three outcomes scorer.validate_number has:
    blank, unparseable, and out of domain all come back as None and get imputed."""
    if _blank(v):
        return None
    s = str(v).strip()
    if not _PLAIN.match(s):
        return None
    x = float(s)
    lo, hi = LEGACY_DOMAIN
    return x if math.isfinite(x) and lo <= x <= hi else None


def prep(df, legacy_median):
    """The rows as the model reads them. Mirrors scorer._prep_dict, field for field:
    the marketing channel lowercased, a blank state as its own feature, a genuinely
    absent category as the string 'nan', and a prior score that is blank or out of range
    imputed to the median rather than passed through as though it were real."""
    out = pd.DataFrame(index=df.index)
    for col in ['channel', 'icp_category', 'company_annual_revenue', 'time_zone']:
        out[col] = [('nan' if _blank(v) else str(v)) for v in df[col]]
    out['utm_medium'] = [str(v).lower().strip() for v in df['utm_medium']]
    out['state_missing'] = [1 if _blank(v) else 0 for v in df['state']]
    lo, hi = LEGACY_DOMAIN
    vals = [_legacy_value(v) for v in df['legacy_score']]
    out['legacy_score'] = [min(max(legacy_median if x is None else x, lo), hi) for x in vals]
    return out[FEATURES]


def build_pipeline():
    """The shape scorer.py introspects. See the header before renaming anything.

    min_frequency=20 folds a level with fewer than 20 leads behind it into an infrequent
    bucket: with a handful of rows, a level's rate is noise, and a lead should not be
    scored on it. C=0.5 keeps the coefficients from chasing the same noise."""
    pre = ColumnTransformer([
        ('cat', OneHotEncoder(handle_unknown='ignore', min_frequency=20), CATEGORICAL),
        ('num', StandardScaler(), NUMERIC)])
    return Pipeline([('pre', pre), ('lr', LogisticRegression(C=0.5, max_iter=2000))])


def main():
    # keep_default_na=False so an empty cell arrives as '', exactly as csv_io hands it to
    # the scorer. Read any other way, a blank would become NaN here and '' at scoring
    # time, and the two paths would prepare the same lead differently.
    df = pd.read_csv(TRAIN_PATH, dtype=str, keep_default_na=False)
    df['won'] = df['won'].astype(int)

    legacy_vals = [x for x in (_legacy_value(v) for v in df['legacy_score']) if x is not None]
    legacy_median = float(np.median(legacy_vals))

    X = prep(df, legacy_median)
    y = df['won'].to_numpy()
    pipe = build_pipeline().fit(X, y)

    scores = pipe.predict_proba(X)[:, 1]
    cutoffs = {tier: round(float(np.quantile(scores, q)), 4)
               for tier, q in TIER_THRESHOLDS.items()}

    meta = {'base_rate': round(float(y.mean()), 4),
            'segment_win_rates': segment_win_rates(df),
            'tier_thresholds': TIER_THRESHOLDS,
            'features': FEATURES,
            'tier_cutoffs_abs': cutoffs,
            'legacy_score_median': legacy_median}

    joblib.dump(pipe, MODEL_PATH)
    with open(META_PATH, 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, indent=2)
        fh.write('\n')

    # Tier counts, so the split the cutoffs produce is visible rather than assumed.
    tiers = np.select([scores >= cutoffs['hot'], scores >= cutoffs['warm'],
                       scores >= cutoffs['cool']], ['hot', 'warm', 'cool'], 'cold')
    print(f'fit on {len(df)} leads from {os.path.relpath(TRAIN_PATH, _DIR)}')
    print(f'base rate {meta["base_rate"]:.4f}   prior-score median {legacy_median:g}')
    for tier in ['hot', 'warm', 'cool', 'cold']:
        n = int((tiers == tier).sum())
        print(f'  {tier:<5} {n:>5}  {n / len(df) * 100:4.1f}%   score >= {cutoffs.get(tier, 0):.4f}')
    # The top of the distribution, printed every run. A tool that tells a rep an inbound
    # lead has a 90% chance of closing is not believable, whatever the maths says, so this
    # number is one a human has to keep an eye on.
    print(f'predicted score: max {scores.max():.3f}   p99 {np.quantile(scores, 0.99):.3f}   '
          f'median {np.median(scores):.3f}')
    print(f'wrote {os.path.relpath(MODEL_PATH, _DIR)} and {os.path.relpath(META_PATH, _DIR)}')


if __name__ == '__main__':
    main()
