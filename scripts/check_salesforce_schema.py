"""Preflight: does the connected org have the schema writeback needs?

    .venv-dev/bin/python scripts/check_salesforce_schema.py

Run with SF_LOGIN_URL / SF_CONSUMER_KEY / SF_CONSUMER_SECRET set -- the same three the app
uses, so if 'Pull from Salesforce' already works for you, this will too. Exits non-zero if
anything is wrong, so it works as a setup step rather than only as something to read.

WHY THIS EXISTS AS A SCRIPT. Almost every way writeback fails against a real org is a
schema or a permission problem, and Salesforce reports those as error bodies attached to
individual records, one field at a time, halfway through a batch. Describe answers all of
it in one call, before anything is sent.

DESCRIBE IS EVALUATED AS THE INTEGRATION USER, which is the point. 'updateable' here is the
real field-level-security answer for the identity the app actually writes as -- not what
the Setup UI shows an admin, which is the version that looks fine right up until the write
comes back with INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY and no useful field name.

Not part of the test suite: it needs credentials and a network, and `pytest` runs clean on
a fresh clone with neither. Same class of thing as seed_salesforce_leads.py next to it --
a dev-time utility, never imported by app.py.
"""
import json, os, sys, urllib.parse, urllib.request

API = 'v59.0'

# name -> (describe type, required picklist values or None, (scale, precision) or None).
#
# The scale check earns its place: Salesforce reports a Number(2,4) as type 'double' with
# scale 4, and a field created as Number(18,0) by an admin in a hurry also reports as
# 'double'. Writeback would then round every score to 0 or 1 silently, on the org's side,
# with nothing in any response to say so. Type alone cannot see that.
EXPECTED = {
    'LeadScorer_Score__c':         ('double',   None, (4, 6)),
    'LeadScorer_Tier__c':          ('picklist', {'Hot', 'Warm', 'Cool', 'Cold'}, None),
    'LeadScorer_Next_Action__c':   ('string',   None, None),
    'LeadScorer_Confidence__c':    ('picklist', {'High', 'Medium', 'Low'}, None),
    'LeadScorer_Scored_At__c':     ('datetime', None, None),
    'LeadScorer_Model_Version__c': ('string',   None, None),
    'Marketing_Channel__c':        ('picklist', {'brand', 'nonbrand', 'paid search',
                                                 'organic search', 'organic social',
                                                 'prospecting', 'retargeting',
                                                 'cpc', 'ppc', 'pmax'}, None),
    'Prior_Score__c':              ('double',   None, (2, 5)),
}
# The two fields the never-clobber-a-human check reads. Not writeable and not ours -- only
# readable, which is all the check needs and all this should ever ask for.
READ_ONLY = ('LastModifiedById', 'LastModifiedDate')
STANDARD = {
    'LeadSource': {'google', 'meta', 'bing', 'tiktok'},
    'Rating':     {'High Value', 'Ideal', 'Low Value', 'Unknown'},
}


def token():
    url, key, sec = (os.environ.get('SF_LOGIN_URL'), os.environ.get('SF_CONSUMER_KEY'),
                     os.environ.get('SF_CONSUMER_SECRET'))
    if not (url and key and sec):
        sys.exit('set SF_LOGIN_URL, SF_CONSUMER_KEY and SF_CONSUMER_SECRET first')
    body = urllib.parse.urlencode({'grant_type': 'client_credentials',
                                   'client_id': key, 'client_secret': sec}).encode()
    req = urllib.request.Request(url.rstrip('/') + '/services/oauth2/token', data=body,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
    return d['access_token'], d['instance_url']


def main():
    tok, inst = token()
    req = urllib.request.Request(f'{inst}/services/data/{API}/sobjects/Lead/describe',
                                 headers={'Authorization': f'Bearer {tok}'})
    with urllib.request.urlopen(req, timeout=30) as r:
        fields = {f['name']: f for f in json.loads(r.read())['fields']}

    print(f'org: {inst}\n')
    bad = 0

    for name, (want_type, want_vals, want_scale) in EXPECTED.items():
        f = fields.get(name)
        if not f:
            print(f'MISSING  {name}'); bad += 1; continue
        notes = []
        if f['type'] != want_type:
            notes.append(f"type is {f['type']}, expected {want_type}")
        if not f.get('updateable'):
            notes.append('NOT updateable by this user (field-level security)')
        if want_scale is not None:
            got = (f.get('scale'), f.get('precision'))
            if got != want_scale:
                notes.append(f'scale/precision is {got}, expected {want_scale} -- a score '
                             'written into the wrong scale is rounded by Salesforce, '
                             'silently')
        if want_vals is not None:
            got = {v['value'] for v in f.get('picklistValues', [])}
            if got != want_vals:
                for extra in sorted(got - want_vals):   notes.append(f'extra value {extra!r}')
                for miss in sorted(want_vals - got):    notes.append(f'missing value {miss!r}')
        if notes:
            print(f'PROBLEM  {name}'); bad += 1
            for n in notes: print(f'           - {n}')
        else:
            print(f'ok       {name}')

    print()
    for name in READ_ONLY:
        if name not in fields:
            print(f'MISSING  {name} -- the clobber check cannot run without it'); bad += 1
        else:
            print(f'ok       {name} readable')

    print()
    for name, want in STANDARD.items():
        got = {v['value'] for v in fields[name].get('picklistValues', [])}
        miss = want - got
        if miss:
            print(f'PROBLEM  {name} is missing {sorted(miss)}'); bad += 1
        else:
            print(f'ok       {name} carries all four fitted values')

    print('\nall good -- this org is ready for writeback'
          if not bad else f'\n{bad} problem(s) above')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
