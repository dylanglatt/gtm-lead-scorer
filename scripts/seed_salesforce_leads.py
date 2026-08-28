"""One-time seed script: creates a handful of realistic demo Leads in a connected
Salesforce org, so 'Pull from Salesforce' on the live site has something worth ranking.

Not part of the app -- this is a dev-time utility, run once by hand, never imported by
app.py. Uses the SAME three env vars and the same Client Credentials Flow as the app
itself, so if /rank/salesforce already works for you, this will too.

Run it from the project root, in the same shell where you've already exported
SF_LOGIN_URL / SF_CONSUMER_KEY / SF_CONSUMER_SECRET:

    .venv-dev/bin/python scripts/seed_salesforce_leads.py

Deliberately mixed data, not a clean sweep:
  - Revenue and state land inside real band boundaries (see scorer.REVENUE_BANDS) so
    Company revenue actually resolves instead of landing on an edge case.
  - LeadSource/Rating mostly use the model's own trained vocabulary (google/meta/bing/
    tiktok, High Value/Ideal/Low Value/Unknown) so most leads score with real confidence.
  - Marketing_Channel__c and Prior_Score__c are populated, which is what lets a seeded
    lead reach HIGH confidence. Before those two custom fields existed there was no home
    on a Lead for utm_medium or legacy_score at all, and every seeded lead came out at Low
    however clean the rest of it was. Seeding without them produces exactly the ceiling
    this project spent a release removing, so they are not optional here.
  - One lead (Ines Moreau) deliberately uses Salesforce's OWN native LeadSource/Rating
    values ('Web', 'Hot') and leaves both custom fields blank -- keeps the 'flags what it
    doesn't recognize' story visible in the demo instead of erasing it entirely. A board
    where every lead is perfect proves less than one with a known bad row in it.

If your org restricts the LeadSource or Rating picklist to its own values (a fresh
Developer Edition org usually does NOT restrict these by default, but some do), Salesforce
rejects the record with INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST. This script catches that
per-record, drops exactly the field(s) named in the error, and retries once -- so one
restricted field can't take the other nine leads down with it. Whatever gets dropped is
printed, so you know exactly what didn't make it in.

That recovery does NOT cover an org missing the two custom fields. Salesforce answers an
unknown column with INVALID_FIELD and no 'fields' list to drop, so the record simply fails
and prints. Run scripts/check_salesforce_schema.py first -- it answers the whole question
in one describe call, before anything is created.
"""
import json, os, sys, urllib.request, urllib.parse, urllib.error

LEADS = [
    dict(FirstName='Sarah', LastName='Chen', Company='Acme Robotics',
         LeadSource='google', Rating='High Value', AnnualRevenue=2_500_000,
         Marketing_Channel__c='brand', Prior_Score__c=78,
         StateCode='TX', CountryCode='US'),
    dict(FirstName='Marcus', LastName='Webb', Company='BrightPath Logistics',
         LeadSource='meta', Rating='Ideal', AnnualRevenue=15_000_000,
         Marketing_Channel__c='paid search', Prior_Score__c=64,
         StateCode='CA', CountryCode='US'),
    dict(FirstName='Priya', LastName='Patel', Company='Nimbus Cloud Storage',
         LeadSource='bing', Rating='Low Value', AnnualRevenue=400_000,
         Marketing_Channel__c='retargeting', Prior_Score__c=31,
         StateCode='NY', CountryCode='US'),
    dict(FirstName='Jordan', LastName='Lee', Company='Vertex Fitness',
         LeadSource='tiktok', Rating='Unknown', AnnualRevenue=3_000_000,
         Marketing_Channel__c='organic social', Prior_Score__c=45,
         StateCode='WA', CountryCode='US'),
    dict(FirstName='Diego', LastName='Alvarez', Company='Ferro Metals',
         LeadSource='google', Rating='High Value', AnnualRevenue=12_000_000,
         Marketing_Channel__c='nonbrand', Prior_Score__c=88),  # no State on purpose
    dict(FirstName='Hannah', LastName='Kim', Company='Solstice Media',
         LeadSource='meta', Rating='Ideal', AnnualRevenue=800_000,
         Marketing_Channel__c='prospecting', Prior_Score__c=57,
         StateCode='FL', CountryCode='US'),
    dict(FirstName='Owen', LastName='Brooks', Company='Craftwell Goods',
         LeadSource='bing', Rating='Low Value', AnnualRevenue=250_000,
         Marketing_Channel__c='organic search', Prior_Score__c=22,
         StateCode='OH', CountryCode='US'),
    # Native SF vocabulary AND both custom fields left blank: the one lead in the set the
    # model cannot read properly, kept on purpose. It is the row that shows the flagging
    # still works now that everything around it resolves.
    dict(FirstName='Ines', LastName='Moreau', Company='Lumen Analytics',
         LeadSource='Web', Rating='Hot', AnnualRevenue=6_000_000,
         StateCode='MA', CountryCode='US'),
]

SF_API_VERSION = 'v59.0'


def token():
    login_url = os.environ.get('SF_LOGIN_URL')
    key = os.environ.get('SF_CONSUMER_KEY')
    secret = os.environ.get('SF_CONSUMER_SECRET')
    if not (login_url and key and secret):
        sys.exit('SF_LOGIN_URL, SF_CONSUMER_KEY and SF_CONSUMER_SECRET must all be set '
                  '(same three vars the app itself needs -- export them first).')
    body = urllib.parse.urlencode({'grant_type': 'client_credentials',
                                    'client_id': key, 'client_secret': secret}).encode()
    req = urllib.request.Request(f'{login_url.rstrip("/")}/services/oauth2/token', data=body,
                                  headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f'Auth failed ({e.code}): {e.read().decode(errors="replace")}')
    return data['access_token'], data['instance_url']


def create_lead(access_token, instance_url, fields):
    url = f'{instance_url}/services/data/{SF_API_VERSION}/sobjects/Lead/'
    body = json.dumps(fields).encode()
    req = urllib.request.Request(url, data=body, method='POST',
                                  headers={'Authorization': f'Bearer {access_token}',
                                           'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()), fields
    except urllib.error.HTTPError as e:
        errors = json.loads(e.read().decode(errors='replace'))
        return errors, fields


def main():
    access_token, instance_url = token()
    print(f'Connected to {instance_url}\n')
    created, dropped_fields = 0, []
    for fields in LEADS:
        result, sent = create_lead(access_token, instance_url, dict(fields))
        # A restricted picklist comes back as a LIST of error dicts, not the usual
        # {"id":..., "success":...} shape -- that's how we tell retry-worthy apart from ok.
        if isinstance(result, list):
            bad_fields = {f for err in result for f in err.get('fields', [])
                          if err.get('errorCode') == 'INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST'}
            if bad_fields:
                retry_fields = {k: v for k, v in fields.items() if k not in bad_fields}
                result, sent = create_lead(access_token, instance_url, retry_fields)
                dropped_fields.append((fields['Company'], sorted(bad_fields)))
        if isinstance(result, dict) and result.get('success'):
            created += 1
            print(f'  ok   {fields["Company"]:<24} -> {result["id"]}')
        else:
            print(f'  FAIL {fields["Company"]:<24} -> {result}')
    print(f'\n{created}/{len(LEADS)} Leads created.')
    if dropped_fields:
        print('\nYour org restricts these picklists, so the values below were dropped '
              'and the record created without them (still counts as ok above):')
        for company, fields in dropped_fields:
            print(f'  {company}: {", ".join(fields)}')


if __name__ == '__main__':
    main()
