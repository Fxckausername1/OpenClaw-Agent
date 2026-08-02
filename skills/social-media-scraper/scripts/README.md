# social-media-scraper scripts

Guidance:

- Do not commit session cookies or tokens. Provide them at runtime via env vars or stdin.
- Respect platform TOS. Prefer official APIs when possible. Use cookie-based scraping only for accounts you control or have consent to access.
- Backoff on 429s and rotate proxies when rate-limited.

Example (stub):
node scrape_stub.mjs --platform instagram --handle artist_handle --cookie "sessionid=..."

