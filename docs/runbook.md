# Runbook — artist pipeline

1) Quick run locally
- python -m venv venv && source venv/bin/activate
- pip install -r requirements.txt
- IG_ALLOW_DM_ONLY=1 ./venv/bin/python artist_pipeline.py

2) Common failures
- APIFY auth error: ensure APIFY_TOKEN is present in environment or GitHub Secrets
- No candidates: increase IG_SEEDS_PER_DAY or IG_FOLLOWERS_PER_SEED or loosen filters

3) Recover from GitHub Actions failures
- Check workflow logs, download leads artifact, run locally to reproduce

