"""Builds the synthetic lead data this repo is trained and demoed on.

WHY THIS FILE EXISTS. The tool was built against a real B2B inbound dataset that is
confidential and cannot be published. Everything below is INVENTED: the numbers in the
parameter table are chosen by hand to tell a coherent go-to-market story, not fitted to
anything. No row, rate or coefficient from the original data survives here, and the
separation check at the bottom of this file is what enforces that — see PRIOR_WIN_RATES.

WHAT IT WRITES
  data/leads_train.csv   9,000 labelled rows. train_and_save.py fits model.joblib on it.
  demo_leads.csv         250 unlabelled rows, a different seed, no deliberate breakage.
                         The file to upload first. (leads_messy_fixture.csv is the
                         robustness test and is hand-written, not generated here.)

Both are seeded and deterministic: same seed, same bytes, every run.

THE STORY THE DATA TELLS, which is the part a founder reads off the why panel:
  paid search (google, bing) beats paid social (meta, tiktok)
  company revenue is monotone — every band up the ladder closes better than the one below
  High Value > Ideal > Unknown > Low Value fit
  no state on file is a genuinely bad signal, not just a missing field
  legacy_score is positively but noisily correlated with winning

HOW IT IS BUILT, and why it is not a stack of independent effects. Each lead draws one
latent QUALITY score first, and channel, fit, revenue and legacy_score are all drawn
CONDITIONAL on it. Two reasons:

  1. Real leads covary. A $10m High Value company arriving on brand search is one kind of
     lead, not four independent coin flips, and a model fit on independent draws learns
     relationships no salesperson would recognize.
  2. It bounds the top of the distribution. With independent effects the best possible
     combination stacks every bonus at once and predicts a 90%+ win chance on an inbound
     lead, which is not a credible number. Because the features covary, each one needs
     only a small direct effect to reach its marginal close rate, and the model tops out
     in the 70s. train_and_save.py prints the max and p99 so this stays visible.

Part of the win probability rides on the latent quality DIRECTLY (QUALITY_EFFECT), which
is the honest version of "the CRM does not record everything that matters". It is what
keeps the fitted model from being able to explain a lead perfectly, and it is why the
scores spread rather than clumping at the ends.
"""
import csv
import json
import math
import os
import statistics

import numpy as np
import pandas as pd

_DIR = os.path.dirname(os.path.abspath(__file__))

N_TRAIN = 9000
N_DEMO = 250
TRAIN_SEED = 7211
DEMO_SEED = 9317          # a different seed, so the demo file is a fresh draw, not a slice

TRAIN_PATH = os.path.join(_DIR, 'data', 'leads_train.csv')
DEMO_PATH = os.path.join(_DIR, 'demo_leads.csv')

COLUMNS = ['lead_id', 'channel', 'icp_category', 'company_annual_revenue', 'utm_medium',
           'time_zone', 'legacy_score', 'state', 'won']

# ---------------------------------------------------------------------------
# THE PARAMETER TABLE. Every number below is invented. Read it as "the world this
# synthetic CRM lives in", and edit it to change the story the model learns.
# ---------------------------------------------------------------------------

# Categorical levels, listed WORST TO BEST. Each field is drawn by cutting a latent
# variable at the quantiles of its mix, so the order here is also the quality order:
# Low Value fit sits at the bottom of its latent, High Value at the top.
#
# `mix` is the share of leads at that level and must sum to 1.
#
# The source is drawn in two steps rather than one, because the quality signal is in
# SEARCH VS SOCIAL — somebody who typed a query wants something — and not in which search
# engine they typed it into. So the latent picks the family, and the family's own split is
# a coin weighted by spend. google and bing therefore sit at the same latent quality, and
# the small gap between their close rates is carried by their effects below rather than by
# pretending a bing lead is a worse lead.
CHANNEL_FAMILIES = [('social', 0.48, [('meta', 0.71), ('tiktok', 0.29)]),
                    ('search', 0.52, [('google', 0.77), ('bing', 0.23)])]
CHANNEL_MIX = [(level, family_share * share)
               for _family, family_share, within in CHANNEL_FAMILIES
               for level, share in within]
ICP_MIX = [('Low Value', 0.49), ('Unknown', 0.08), ('Ideal', 0.25), ('High Value', 0.18)]
REVENUE_MIX = [('Self-Serve Signup', 0.04),      # a product, not an amount: smallest of all
               ('Less than $250,000', 0.40),
               ('$250,000 to $499,999', 0.20),
               ('$500,000 to $999,999', 0.14),
               ('$1,000,000 to $4,999,999', 0.12),
               ('$5,000,000 to $9,999,999', 0.06),
               ('$10,000,000 to $24,999,999', 0.04)]
# Nothing above $25m on purpose. The tool offers a '$25,000,000 and greater' band and
# folds it down to the top fitted band; scorer.REVENUE_ALIASES documents that, and it is
# only true while no training lead is that large.

# How tightly each field tracks the latent quality score. `load` is its weight on quality,
# `noise` the weight on its own independent draw. Bigger noise = a blurrier signal, which
# is what makes the segments overlap instead of separating cleanly.
LOADINGS = {'channel': (0.60, 0.95), 'icp_category': (0.65, 0.80),
            'company_annual_revenue': (0.65, 0.85)}

# legacy_score: the previous system's 0-100 score. Correlated with quality at about 0.6 —
# it knows something, it is not a giveaway, and a rep who trusts it blindly is wrong often
# enough to matter.
LEGACY_CENTER, LEGACY_QUALITY, LEGACY_NOISE = 50.0, 18.0, 24.0
LEGACY_BLANK_RATE = 0.04

# Marketing channel, conditional on the source. Search sources carry search mediums;
# social sources carry prospecting and retargeting. Every value the form offers appears
# here, because a value the model was never fit on would flag the moment a rep picked it.
UTM_LEVELS = ['brand', 'nonbrand', 'paid search', 'cpc', 'ppc', 'pmax',
              'organic search', 'organic social', 'prospecting', 'retargeting']
UTM_BY_CHANNEL = {
    'google': [0.26, 0.22, 0.14, 0.10, 0.07, 0.09, 0.07, 0.00, 0.03, 0.02],
    'bing':   [0.22, 0.24, 0.18, 0.14, 0.10, 0.02, 0.06, 0.00, 0.02, 0.02],
    'meta':   [0.06, 0.05, 0.01, 0.04, 0.02, 0.01, 0.01, 0.25, 0.35, 0.20],
    'tiktok': [0.05, 0.04, 0.01, 0.03, 0.02, 0.01, 0.01, 0.28, 0.38, 0.17],
}
UTM_BLANK_RATE = 0.03
REVENUE_BLANK_RATE = 0.03
# Blanks in these two, and only these two, so 'nan' is a real fitted level for them and
# not for channel or fit — which is what scorer._missing's comment says is true.

# Values a CRM writes into a column that are not answers to the question the column asks:
# a licence error where a marketing channel should be, a machine-written duplicate of a
# real medium, a literal '--'. Every export has them, and they are the reason app.OFFERED
# is a hand-curated list rather than the fitted vocabulary — the tool ACCEPTS these (they
# were fit on, so they score) and simply does not SUGGEST them to a rep.
#
# Applied after the win probability is drawn, like the blanks: the CRM mangling a field
# does not change whether the deal was won, it only changes what the export says.
UTM_ARTEFACTS = ['expired or invalid attributer license', 'rd', 'other campaigns',
                 'prospecting_lead_generation', 'retargeting_lead_generation']
ZONE_ARTEFACTS = ['--', 'Notify Admin']
UTM_ARTEFACT_RATE = 0.0075
ZONE_ARTEFACT_RATE = 0.0035
# Rare on purpose, and rare enough that each artefact lands on fewer than the encoder's
# min_frequency=20 leads, so they pool into one infrequent bucket instead of each earning
# a coefficient of its own. At a higher rate they get fitted individually, and a level
# with thirty leads behind it is a coin flip: on the first pass at this, one artefact drew
# a lucky run of wins and a lead whose marketing channel was a licence error scored 82%,
# above every clean lead on the board. That is the exact failure min_frequency is set to
# prevent, so the rate is set to let it work rather than to overwhelm it.
# demo_leads.csv is generated with these off — it is the file that shows what the tool
# does, and leads_messy_fixture.csv is the file that shows what it survives.

# State pool and its time zone. Weights are relative and get normalized. Every zone the
# form offers is represented, including Alaska and Hawaii, which need enough rows to clear
# the encoder's min_frequency=20.
STATE_POOL = [('NY', 'Eastern', 7.0), ('FL', 'Eastern', 7.0), ('GA', 'Eastern', 4.0),
              ('PA', 'Eastern', 4.0), ('NC', 'Eastern', 3.5), ('OH', 'Eastern', 3.0),
              ('MA', 'Eastern', 3.0), ('VA', 'Eastern', 2.5), ('NJ', 'Eastern', 2.5),
              ('MI', 'Eastern', 2.0),
              ('TX', 'Central', 9.0), ('IL', 'Central', 5.0), ('TN', 'Central', 2.5),
              ('MO', 'Central', 2.0), ('MN', 'Central', 2.0), ('WI', 'Central', 1.5),
              ('LA', 'Central', 1.2),
              ('CO', 'Mountain', 3.0), ('UT', 'Mountain', 1.5), ('MT', 'Mountain', 0.8),
              ('AZ', 'Arizona', 3.0),
              ('CA', 'Pacific', 12.0), ('WA', 'Pacific', 4.0), ('OR', 'Pacific', 2.5),
              ('NV', 'Pacific', 1.8),
              ('AK', 'Alaska', 0.7), ('HI', 'Hawaii', 0.7)]
# The state->zone pairs must agree with scorer.STATE_TZ, which is what the app derives a
# zone from when a rep types a state. test_scorer.py pins that.

STATE_MISSING_RATE = 0.06
# A lead with no state can still carry a zone — a phone number gives it away — so only
# some of those rows lose the zone too. Without this the 'no zone' dummy would be a
# perfect stand-in for state_missing and the two would split one signal between them.
ZONE_KEPT_WHEN_STATE_MISSING = 0.40

# ---------------------------------------------------------------------------
# Win probability. log-odds = intercept + quality + per-level effects, through a logistic.
# The TARGET close rate for each level — the story this file is written to tell — is the
# ladder in the docstring, restated per level in the comments below.
#
# READ THE NUMBERS IN THIS BLOCK AS RESIDUALS, NOT AS THE STORY. The latent quality has
# already done most of the work by the time they are applied: High Value leads are drawn
# from the top of the quality distribution, so quality ALONE would close them well above
# the 37% this file is aiming for, and the -0.316 pulls them back down to it. A negative
# number here does not mean the level is bad. It means the shared factor over-explained it.
#
# This is the same effect scorer._why exists to work around: fit a correlated logistic and
# High Value comes back with a negative coefficient while closing at 37%, which is true to
# the arithmetic and impossible to say to a rep. The realized close rates — what meta.json
# stores and the why panel shows — are the ladder, monotone and in the intended order.
#
# The values were solved by fixed-point: adjust each level by the gap between its realized
# close rate and its target, repeat until they land, then round. Changing a mix, a loading
# or QUALITY_EFFECT invalidates them, and check_separation is what catches the drift.
# ---------------------------------------------------------------------------
INTERCEPT = -1.413
QUALITY_EFFECT = 0.90                    # what the CRM does not record, in log-odds per sd
LEGACY_EFFECT = 0.80                     # per (legacy_score - 50) / 50
STATE_MISSING_EFFECT = -1.323            # the one field where a blank is real information

EFFECTS = {
    # targets: google 32%, bing 30%, meta 18%, tiktok 12%. Search and social are already
    # separated by the family draw above, so these only carry the gap WITHIN a family.
    'channel': {'meta': 0.161, 'google': 0.020, 'bing': -0.095, 'tiktok': -0.370},
    # targets: High Value 37%, Ideal 30%, Unknown 24%, Low Value 17%. 'Unknown' is a level
    # the CRM records as an answer, not an empty box, and it sits between Low and Ideal.
    'icp_category': {'Low Value': 0.113, 'Ideal': 0.014, 'Unknown': -0.023,
                     'High Value': -0.316},
    # targets, up the ladder: 8%, 15.5%, 23%, 31%, 36%, 42%, 47%.
    'company_annual_revenue': {'$500,000 to $999,999': 0.114,
                               '$1,000,000 to $4,999,999': 0.095,
                               '$5,000,000 to $9,999,999': 0.086,
                               'Self-Serve Signup': 0.034,
                               'Less than $250,000': -0.044,
                               '$250,000 to $499,999': -0.060,
                               '$10,000,000 to $24,999,999': -0.103},
    # Not on the why panel and not in segment_win_rates — the marketing channel is a
    # smaller, secondary signal, so these are set by hand and left alone rather than
    # solved to a target.
    'utm_medium': {'brand': 0.18, 'nonbrand': 0.05, 'paid search': 0.10, 'cpc': 0.02,
                   'ppc': 0.02, 'pmax': -0.05, 'organic search': 0.12,
                   'organic social': -0.15, 'prospecting': -0.12, 'retargeting': 0.06},
}

# ---------------------------------------------------------------------------
# THE SEPARATION CHECK.
#
# These are the SUPERSEDED win rates — the ones in the meta.json this generator replaced,
# which were derived from the confidential dataset. They are frozen here as literals for
# one reason: to prove the new numbers are not the old numbers.
#
# They are deliberately NOT read from meta.json. train_and_save.py overwrites that file,
# so a check that read it would compare the regenerated values against themselves on the
# second run and either hard-fail or pass vacuously. A literal cannot drift.
# ---------------------------------------------------------------------------
PRIOR_BASE_RATE = 0.1598
PRIOR_WIN_RATES = {
    'channel': {'bing': 0.236, 'google': 0.240, 'meta': 0.113, 'tiktok': 0.051},
    'icp_category': {'High Value': 0.286, 'Ideal': 0.228, 'Low Value': 0.107,
                     'Unknown': 0.167},
    'company_annual_revenue': {'$1,000,000 to $4,999,999': 0.290,
                               '$10,000,000 to $24,999,999': 0.240,
                               '$250,000 to $499,999': 0.164,
                               '$5,000,000 to $9,999,999': 0.325,
                               '$500,000 to $999,999': 0.241,
                               'Less than $250,000': 0.090,
                               'Self-Serve Signup': 0.015},
    'state': {'missing': 0.029, 'on file': 0.168},
}
MIN_SEPARATION = 0.03          # no new rate may land within 3 points of its old one
BASE_RATE_FORBIDDEN = (0.14, 0.18)
# The table above is designed with a ~6-point gap on every key, so the 3-point floor is
# never within reach of sampling noise. check_separation prints the tightest realized gap.

SEGMENT_FIELDS = ['channel', 'icp_category', 'company_annual_revenue']


def _cut_points(mix, sd):
    """Quantile cuts that turn a latent normal into a categorical with the given mix."""
    dist = statistics.NormalDist(0.0, sd)
    out, acc = [], 0.0
    for _level, share in mix[:-1]:
        acc += share
        out.append(dist.inv_cdf(acc))
    return out


def _draw_ordinal(rng, n, mix, quality, field):
    """One categorical column, drawn worst-to-best along the latent quality score."""
    load, noise = LOADINGS[field]
    latent = load * quality + noise * rng.standard_normal(n)
    idx = np.searchsorted(_cut_points(mix, math.hypot(load, noise)), latent)
    levels = np.array([lvl for lvl, _ in mix], dtype=object)
    return levels[idx]


def generate(n, seed, prefix='L', artefacts=True):
    """n synthetic leads as a DataFrame, with the label and the true probability.

    Deterministic in (n, seed): every draw comes off one seeded Generator, in a fixed
    order, so re-running writes byte-identical files. `prefix` keeps the two files' lead
    ids apart, so a demo lead can never be mistaken for a training row. `artefacts` is the
    CRM junk described above, on for training and off for the demo file."""
    rng = np.random.default_rng(seed)
    quality = rng.standard_normal(n)

    family = _draw_ordinal(rng, n, [(f, s) for f, s, _w in CHANNEL_FAMILIES],
                           quality, 'channel')
    channel = np.empty(n, dtype=object)
    for name, _share, within in CHANNEL_FAMILIES:
        hit = family == name
        channel[hit] = rng.choice([lvl for lvl, _ in within], size=int(hit.sum()),
                                  p=[s for _, s in within])

    icp = _draw_ordinal(rng, n, ICP_MIX, quality, 'icp_category')
    revenue = _draw_ordinal(rng, n, REVENUE_MIX, quality, 'company_annual_revenue')

    # Marketing channel follows the source: search mediums on search, social on social.
    utm = np.empty(n, dtype=object)
    for src, weights in UTM_BY_CHANNEL.items():
        hit = channel == src
        utm[hit] = rng.choice(UTM_LEVELS, size=int(hit.sum()), p=weights)

    legacy = LEGACY_CENTER + LEGACY_QUALITY * quality + LEGACY_NOISE * rng.standard_normal(n)
    legacy = np.clip(np.rint(legacy), 0, 100)

    codes = [s for s, _z, _w in STATE_POOL]
    zones = {s: z for s, z, _w in STATE_POOL}
    weights = np.array([w for _s, _z, w in STATE_POOL], dtype=float)
    state = rng.choice(codes, size=n, p=weights / weights.sum())
    zone = np.array([zones[s] for s in state], dtype=object)
    state_missing = rng.random(n) < STATE_MISSING_RATE
    keep_zone = rng.random(n) < ZONE_KEPT_WHEN_STATE_MISSING
    state = np.where(state_missing, '', state)
    zone = np.where(state_missing & ~keep_zone, '', zone)

    # The score, then the coin flip. Blanks are applied AFTER this: a field the CRM failed
    # to record does not change whether the deal was won, it only hides why.
    logit = (INTERCEPT + QUALITY_EFFECT * quality
             + LEGACY_EFFECT * (legacy - LEGACY_CENTER) / 50.0
             + STATE_MISSING_EFFECT * state_missing)
    for field, values in (('channel', channel), ('icp_category', icp),
                          ('company_annual_revenue', revenue), ('utm_medium', utm)):
        table = EFFECTS[field]
        logit = logit + np.array([table[v] for v in values])
    p = 1.0 / (1.0 + np.exp(-logit))
    won = (rng.random(n) < p).astype(int)

    revenue = np.where(rng.random(n) < REVENUE_BLANK_RATE, '', revenue)
    utm = np.where(rng.random(n) < UTM_BLANK_RATE, '', utm)
    legacy_out = np.where(rng.random(n) < LEGACY_BLANK_RATE, '',
                          legacy.astype(int).astype(str))

    # Last, so that turning artefacts off leaves every other column of a given seed
    # untouched: the demo file is then the same draw as the training file, minus the mess.
    if artefacts:
        utm = np.where(rng.random(n) < UTM_ARTEFACT_RATE,
                       rng.choice(UTM_ARTEFACTS, size=n), utm)
        zone = np.where(rng.random(n) < ZONE_ARTEFACT_RATE,
                        rng.choice(ZONE_ARTEFACTS, size=n), zone)

    return pd.DataFrame({'lead_id': [f'{prefix}-{i:05d}' for i in range(1, n + 1)],
                         'channel': channel, 'icp_category': icp,
                         'company_annual_revenue': revenue, 'utm_medium': utm,
                         'time_zone': zone, 'legacy_score': legacy_out,
                         'state': state, 'won': won, 'p_true': p})


def segment_win_rates(df):
    """Close rate and n for every level the why panel reports on.

    Lives here rather than in train_and_save.py so the numbers this file CHECKS are the
    same numbers that file WRITES into meta.json — one computation, no drift. Blank
    values are left out: 'no revenue recorded' is not a segment a rep can act on.

    State is reported by PRESENCE, not by value: the model was never fit on Texas, it was
    fit on whether a state was there at all."""
    out = {}
    for col in SEGMENT_FIELDS:
        vals = df[col].astype(str).str.strip()
        keep = df[vals != '']
        out[col] = {str(level): {'win_rate': round(float(g['won'].mean()), 4),
                                 'n': int(len(g))}
                    for level, g in keep.groupby(vals[vals != ''], sort=True)}
    present = df['state'].astype(str).str.strip() != ''
    out['state'] = {}
    for label, g in (('on file', df[present]), ('missing', df[~present])):
        out['state'][label] = {'win_rate': round(float(g['won'].mean()), 4), 'n': int(len(g))}
    return out


def check_separation(base_rate, rates):
    """Fail loudly if the regenerated data resembles the dataset it replaced.

    Two rules, both hard: the base rate may not sit in the band the old one sat in, and no
    segment rate may land within MIN_SEPARATION of the old rate for that same segment.
    Returns the tightest gap it found, so a run that passes still shows its margin."""
    problems = []
    lo, hi = BASE_RATE_FORBIDDEN
    if lo <= base_rate <= hi:
        problems.append(f'base rate {base_rate:.4f} is inside the forbidden band '
                        f'{lo}-{hi} (the superseded value was {PRIOR_BASE_RATE})')
    tightest = (abs(base_rate - PRIOR_BASE_RATE), 'base_rate')
    for field, priors in PRIOR_WIN_RATES.items():
        for level, prior in priors.items():
            got = rates.get(field, {}).get(level)
            if got is None:
                problems.append(f'{field} / {level} is missing from the regenerated data, '
                                'so its separation cannot be checked')
                continue
            gap = abs(got['win_rate'] - prior)
            tightest = min(tightest, (gap, f'{field} / {level}'))
            if gap < MIN_SEPARATION:
                problems.append(f'{field} / {level}: {got["win_rate"]:.3f} is only '
                                f'{gap * 100:.1f}pp from the superseded {prior:.3f}')
    if problems:
        raise SystemExit('SEPARATION CHECK FAILED — the regenerated data is too close to '
                         'the dataset it replaces:\n  ' + '\n  '.join(problems))
    return tightest


def write_csv(df, path, columns):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        for row in df[columns].itertuples(index=False):
            w.writerow(row)


def main():
    train = generate(N_TRAIN, TRAIN_SEED)
    write_csv(train, TRAIN_PATH, COLUMNS)

    # The demo file is scored, not trained on, so it carries no label — and no p_true,
    # which would be an answer key for a score the tool is supposed to be predicting.
    demo = generate(N_DEMO, DEMO_SEED, prefix='D', artefacts=False)
    write_csv(demo, DEMO_PATH, [c for c in COLUMNS if c != 'won'])

    base = float(train['won'].mean())
    rates = segment_win_rates(train)
    gap, where = check_separation(base, rates)

    print(f'wrote {TRAIN_PATH}  {len(train)} rows')
    print(f'wrote {DEMO_PATH}  {len(demo)} rows')
    print(f'base rate {base:.4f}  (superseded: {PRIOR_BASE_RATE})')
    for field in SEGMENT_FIELDS + ['state']:
        print(f'  {field}')
        for level, info in sorted(rates[field].items(), key=lambda kv: -kv[1]['win_rate']):
            prior = PRIOR_WIN_RATES[field].get(level)
            delta = '' if prior is None else f'   was {prior:.3f}, moved {abs(info["win_rate"] - prior) * 100:4.1f}pp'
            print(f'    {level:<28} {info["win_rate"]:.3f}  n={info["n"]:>5}{delta}')
    print(f'separation check passed — tightest gap {gap * 100:.1f}pp on {where} '
          f'(floor is {MIN_SEPARATION * 100:.0f}pp)')
    print(json.dumps({'true_p_max': round(float(train['p_true'].max()), 4),
                      'true_p_p99': round(float(train['p_true'].quantile(0.99)), 4)}))


if __name__ == '__main__':
    main()
