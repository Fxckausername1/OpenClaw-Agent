#!/usr/bin/env python3
"""Read and print the first 200 rows from the configured Google Sheet.

Requires GOOGLE_APPLICATION_CREDENTIALS env var set to service account JSON and
SHEET_ID env var set to the spreadsheet ID.
"""
import os
import sys
from pprint import pprint

sheet_id = os.environ.get('SHEET_ID')
creds_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')

if not sheet_id:
    print('SHEET_ID not set', file=sys.stderr); sys.exit(2)
if not creds_path or not os.path.exists(creds_path):
    print('GOOGLE_APPLICATION_CREDENTIALS not set or file missing', file=sys.stderr); sys.exit(2)

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except Exception as e:
    print('Google libs not installed:', e, file=sys.stderr); sys.exit(2)

creds = service_account.Credentials.from_service_account_file(creds_path, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
service = build('sheets', 'v4', credentials=creds)
sheet = service.spreadsheets()
res = sheet.values().get(spreadsheetId=sheet_id, range='A1:G200').execute()
values = res.get('values', [])
print(f'Retrieved {len(values)} rows from sheet {sheet_id}')
for i, row in enumerate(values[:200], start=1):
    print(i, row)

