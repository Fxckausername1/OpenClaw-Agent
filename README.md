# OpenClaw Agent — Pipeline

Lightweight repo for daily Atlanta artist lead discovery and outreach.

Quickstart
- Copy required secrets into GitHub repo Secrets (see .env.example)
- Workflows are configured under .github/workflows:
  - daily-pipeline.yml: scheduled run (cron) that executes artist_pipeline.py and uploads artifacts
  - ci.yml: syntax smoke tests on push/PR
- To run locally:
  - python -m venv venv && source venv/bin/activate
  - pip install -r requirements.txt  # if present
  - IG_ALLOW_DM_ONLY=1 IG_SEEDS_PER_DAY=6 IG_FOLLOWERS_PER_SEED=25 ./venv/bin/python artist_pipeline.py

Where things live
- scripts/: utility scripts and CI helpers
- data/: runtime outputs (leads_YYYY-MM-DD.csv, leads_master.csv, ig_digest_latest.txt)
- docs/runbook.md: operational runbook and recovery steps

Security
- Do NOT commit credentials. Use GitHub Secrets for APIFY_TOKEN, GCP_SA_JSON, SHEETS credentials.

