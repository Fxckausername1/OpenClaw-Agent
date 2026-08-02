---
name: spotify-youtube-enricher
description: Fetch Spotify/YouTube metadata (monthly listeners, top tracks, channel links) and normalize results for lead enrichment. Use with audio and social scrapers.
user-invocable: true
---

Scaffold for enrichment tasks. Includes example scripts that demonstrate how to call Spotify/YouTube APIs (stubs). Replace API keys via env vars at runtime.

Included:
- scripts/enrich_stub.mjs — demonstrates lookup flow and output schema.

Security: Store API keys in env vars; do not hardcode.

