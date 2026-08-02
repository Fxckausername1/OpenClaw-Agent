---
name: social-media-scraper
description: Unified scraper scaffolding for Instagram, TikTok, and YouTube. Handles session cookies, per-handle targets, rate-limit backoff, and exports normalized profiles for enrichment.
user-invocable: true
---

Use when you need to collect public social profile metadata (followers, latest posts, profile URL, bio) across platforms. This skill is a scaffold: it provides safe helper scripts and a clear flow for authenticated scraping under human control.

Included:
- scripts/scrape_stub.mjs — example scraper CLI demonstrating cookie-based auth and rate-limit handling (stub).
- scripts/README.md — integration notes and recommended headers/rate limits.

Security: Keep session cookies/private tokens out of repository; pass them at runtime via env vars. The skill will never hardcode credentials.

