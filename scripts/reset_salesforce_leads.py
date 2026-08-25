"""One-time cleanup: deletes ALL Lead records in the connected Salesforce org.

Dev-time utility only, never imported by app.py. Uses the SAME three env vars and
Client Credentials Flow as the app and seed_salesforce_leads.py. Meant to be run
BEFORE seed_salesforce_leads.py, so Salesforce's own default demo Leads (every
Developer Edition org ships with ~20 of them -- "Edge Communications", "Grand
Hotels & Resorts", etc.) and any leftovers from earlier seed runs don't dilute
the mock data below the ranking model's coverage threshold.

Run it from the project root, in the same shell where you've already exported
SF_LOGIN_URL / SF_CONSUMER_KEY / SF_CONSUMER_SECRET:

    .venv-dev/bin/python scripts/reset_salesforce_leads.py

This is destructive and does not distinguish real data from demo data -- it
deletes every Lead in the org, no filtering. That's fine for a dev org with no
real data in it (which is what this was built for), but it will ask for a
typed "yes" first regardless, since there's no undo.
"""
import json, os, sys, urllib.request, urllib.parse, urllib.error

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


def query_all_lead_ids(access_token, instance_url):
    query = urllib.parse.urlencode({'q': 'SELECT Id FROM Lead'})
    url = f'{instance_url}/services/data/{SF_API_VERSION}/query?{query}'
    ids = []
    while url:
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {access_token}'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        ids.extend(r['Id'] for r in data['records'])
        nxt = data.get('nextRecordsUrl')
        url = f'{instance_url}{nxt}' if nxt else None
    return ids


def delete_lead(access_token, instance_url, lead_id):
    url = f'{instance_url}/services/data/{SF_API_VERSION}/sobjects/Lead/{lead_id}'
    req = urllib.request.Request(url, method='DELETE',
                                  headers={'Authorization': f'Bearer {access_token}'})
    try:
        urllib.request.urlopen(req, timeout=15)
        return True, None
    except urllib.error.HTTPError as e:
        return False, e.read().decode(errors='replace')


def main():
    access_token, instance_url = token()
    ids = query_all_lead_ids(access_token, instance_url)
    if not ids:
        print('No Lead records found -- nothing to delete.')
        return
    print(f'Found {len(ids)} Lead record(s) in {instance_url}.')
    reply = input(f'Type "yes" to permanently delete all {len(ids)} of them: ').strip().lower()
    if reply != 'yes':
        print('Aborted -- nothing deleted.')
        return
    deleted, failed = 0, []
    for lid in ids:
        ok, err = delete_lead(access_token, instance_url, lid)
        if ok:
            deleted += 1
        else:
            failed.append((lid, err))
    print(f'\n{deleted}/{len(ids)} Lead(s) deleted.')
    if failed:
        print('Failed:')
        for lid, err in failed:
            print(f'  {lid}: {err}')


if __name__ == '__main__':
    main()
