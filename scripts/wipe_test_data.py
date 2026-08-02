#!/usr/bin/env python3
"""Wipe test data from data/artists.csv and the configured Google Sheet.

WARNING: Destructive. This will remove rows from the CSV (keeping header) and
clear rows A2:Z1000 from the target Sheet (preserves row 1 header).

Configuration:
- GOOGLE_APPLICATION_CREDENTIALS env var -> service account JSON path
- SHEET_ID env var -> target spreadsheet ID

Usage: python3 scripts/wipe_test_data.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / 'data' / 'artists.csv'


def wipe_csv(path=CSV_PATH):
    if not path.exists():
        print(f'CSV not found at {path}, nothing to do')
        return
    # preserve header if present
    lines = path.read_text().splitlines()
    header = lines[0] if lines else ''
    path.write_text(header + '\n')
    print(f'Wiped CSV and kept header at {path}')


def wipe_sheet():
    sheet_id = os.environ.get('SHEET_ID')
    creds_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if not sheet_id or not creds_path or not Path(creds_path).exists():
        print('SHEET_ID or GOOGLE_APPLICATION_CREDENTIALS not set / missing; skipping sheet wipe')
        return
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except Exception as e:
        print('Google libs not installed; cannot wipe sheet:', e)
        return

    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    service = build('sheets', 'v4', credentials=creds)
    body = {}
    try:
        # Clear rows A2:Z1000 (preserve header row 1)
        service.spreadsheets().values().clear(spreadsheetId=sheet_id, range='A2:Z1000', body=body).execute()
        print(f'Cleared rows A2:Z1000 in sheet {sheet_id}')
    except Exception as e:
        print('Failed to clear sheet:', e)


def main():
    confirm = input('This will DELETE test data from CSV and Sheet. Type YES to proceed: ')
    if confirm != 'YES':
        print('Aborting.')
        return
    wipe_csv()
    wipe_sheet()


if __name__ == '__main__':
    main()

