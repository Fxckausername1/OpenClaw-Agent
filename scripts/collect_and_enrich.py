#!/usr/bin/env python3
"""Collect ATL indie R&B/Trap artists via search and enrich pages for contact info using Playwright.
Writes /home/heff/.openclaw/workspace/output/atl_indie_leads_enriched.csv
"""
from playwright.sync_api import sync_playwright
import re
from urllib.parse import urlparse
from pathlib import Path
import csv

ROOT = Path('/home/heff/.openclaw/workspace')
OUT = ROOT / 'output' / 'atl_indie_leads_enriched.csv'
OUT.parent.mkdir(parents=True, exist_ok=True)

queries = [
    'Atlanta independent R&B artist 2026',
    'Atlanta R&B singer independent 2026',
    'Atlanta trap artist independent 2026',
    'Atlanta emerging R&B artists 2026 blog',
    'Atlanta independent artist SoundCloud 2026',
]

email_re = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
phone_re = re.compile(r'\+?\d[\d\s().-]{7,}\d')
seen = set()
leads = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    for q in queries:
        page.goto('https://duckduckgo.com/')
        page.fill('input[name=q]', q)
        page.keyboard.press('Enter')
        page.wait_for_timeout(1000)
        # gather result links
        links = page.query_selector_all('a.result__a')
        if not links:
            # try Bing
            page.goto('https://www.bing.com/search?q=' + q.replace(' ', '+'))
            page.wait_for_timeout(1000)
            links = page.query_selector_all('li.b_algo h2 a')
        for a in links:
            try:
                href = a.get_attribute('href')
            except:
                href = None
            if not href:
                continue
            parsed = urlparse(href)
            if parsed.scheme not in ('http','https'):
                continue
            host = parsed.netloc
            if host in ('accounts.google.com','www.facebook.com'):
                continue
            if href in seen:
                continue
            seen.add(href)
            # visit page
            try:
                page.goto(href, timeout=15000)
                page.wait_for_timeout(800)
                content = page.content()
            except Exception as e:
                content = ''
            emails = email_re.findall(content)
            phones = phone_re.findall(content)
            title = ''
            try:
                title_el = page.query_selector('title')
                if title_el:
                    title = title_el.inner_text()
            except:
                title = ''
            leads.append({'name': title or host, 'platform': host, 'profile_url': href, 'emails': ';'.join(sorted(set(emails)))[:200], 'phones': ';'.join(sorted(set(phones)))[:200], 'notes': ''})
            if len(leads) >= 30:
                break
        if len(leads) >= 30:
            break
    browser.close()

# prioritize those with emails
leads_sorted = sorted(leads, key=lambda x: (0 if x['emails'] else 1))
selected = leads_sorted[:15]

with OUT.open('w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=['name','platform','profile_url','emails','phones','notes'])
    w.writeheader()
    for r in selected:
        w.writerow(r)

print('Wrote', OUT)
