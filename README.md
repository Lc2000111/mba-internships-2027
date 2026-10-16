# Summer 2027 MBA Internship Feed

Live site: https://lc2000111.github.io/mba-internships-2027/

Consulting, LDP and strategy MBA internships in the US and China, with visa-sponsorship flags.

## How it updates

`scripts/agent.py` is a small agent that runs every 6 hours on GitHub Actions (`.github/workflows/agent.yml`):

1. Pulls current postings from Amazon Jobs, several company Workday sites, and a community MBA internship list.
2. Checks them against its memory in `data.json`. Only postings it hasn't seen before get fetched in full
   and classified (by `gpt-6-luna` through Portkey when the `PORTKEY_API_KEY` secret is set, otherwise by keyword rules).
3. Marks postings that disappeared as closed, and merges in the hand-picked roles in `curated.json`.
4. Commits `data.json`. The site (`index.html`) reads it on every page load.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/agent.py
python3 -m http.server 8765
```

Put `PORTKEY_API_KEY=...` in a `.env` file (never commit it) to turn on AI classification.
