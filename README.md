# art_tracking

Tracks up-and-coming artists from Denver galleries, residencies and MFA programs. Each run scrapes
the sources in `sources.yaml`, matches names against `artists.json`, lets a curator decide who fits
your preferences (`artifacts/user_preference/preferences.md`), researches approved artists with
Claude (web search), and writes `artifacts/artists/<slug>.md`.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo "CLAUDE_KEY=sk-ant-..." > .env
```

## Run

```bash
.venv/bin/python main.py                     # normal run, capped by max_artists_per_run
.venv/bin/python main.py --dry-run           # scrape, match, curate; show the queue (no research)
.venv/bin/python main.py --artist "Name"     # research one artist, skipping the curator
.venv/bin/python main.py --limit 25          # override the per-run cap (0 = no limit)
.venv/bin/python main.py --force             # re-research regardless of staleness
.venv/bin/python main.py --rebuild-registry  # regenerate artists.json from the profiles
```

## How it works

1. **Collect** (`tracker/scrape.py`): sitemaps (Robischon, K Contemporary), link lists
   (David B. Smith, Rule), or Claude name extraction from page text (RedLine). Pages whose content
   hash hasn't changed reuse cached results from `state.json`.
2. **Match** (`tracker/match.py`): names exactly matching `artists.json` (name or alias) are
   updates with no API call. Unmatched names go to Claude with the compact registry only
   (slug, name, aliases, one-liner, affiliations), never the markdown files, and come back as
   create / update / skip with a cleaned name and priority. Decisions are cached.
3. **Curate** (`tracker/curate.py`): walks the queue in priority order (new artists and new
   info first, then stale profiles). For each, it checks medium and style (a couple of web searches
   at most) against your preferences file and returns add or reject, until `max_artists_per_run`
   are approved. Verdicts are cached until the preferences file changes. Tracked artists that get
   rejected are marked `dropped` in the registry and their profiles move to `artifacts/dropped/`.
4. **Research + write** (`tracker/research.py`): one web-search call gathers sourced notes; one
   structured call merges them into that artist's existing profile.
5. **Save** (`tracker/profiles.py`): writes the markdown (front matter + body + changelog) and
   updates the artist's `artists.json` entry in the same step.

## Files

- `sources.yaml`: settings, sources (tier `roster` / `pipeline`), and career signals checked during research
- `artists.json`: the registry, committed
- `state.json`: scrape cache and cached match decisions
- `artifacts/user_preference/preferences.md`: what you do and don't want; edit freely
- `artifacts/artists/`: one profile per tracked artist
- `artifacts/dropped/`: profiles of artists the curator removed

## Models and cost

Each step's model is set under `settings.models`. Defaults: Haiku 5.5 for extract, decide and
curate; Sonnet 5.5 for research and write. Each run prints an estimated cost. Measured: about
$0.015 per curator check and about $0.10 per researched artist.

## Web app

A minimal gallery-style site: every tracked artist in the sidebar, their profile on the right, and a
Refresh button that re-runs research and rewrites that profile.

- `server.py`: FastAPI. `GET /api/artists`, `GET /api/artists/{slug}`, and
  `POST /api/artists/{slug}/refresh`, which streams progress as NDJSON and needs the `X-Admin-Token` header.
- `web/`: Vite + React frontend.
- `tracker/storage.py`: local files in development, a private Vercel Blob store when deployed
  (the Vercel filesystem is read-only).

### Run locally

```bash
.venv/bin/uvicorn server:app --reload --port 8000     # terminal 1
cd web && npm install && npm run dev                  # terminal 2 → http://localhost:5173
```

Locally, refresh needs no token unless `ADMIN_TOKEN` is set.

### Deploy to Vercel

1. Import the GitHub repo in Vercel. `vercel.json` defines two services, `web` (Vite) and `api`
   (FastAPI). Vercel Services is in beta.
2. Storage → Create → Blob, choose **private**, and connect it to the project.
3. Project → Settings → Environment Variables: add `CLAUDE_KEY` and `ADMIN_TOKEN` (any long
   random string; you type it once in the browser when you first click Refresh).
4. Copy your local data into Blob: add the store's `BLOB_READ_WRITE_TOKEN` to `.env`, then run
   `.venv/bin/python main.py --push`.
5. Redeploy.

Once deployed, the site's data lives in Blob. To make new pipeline runs show up on the site, run the
pipeline directly against Blob with `STORAGE=blob .venv/bin/python main.py`. Alternatively, run
`--pull` before a local run and `--push` after; `--push` overwrites Blob, so pull first or you'll lose
refreshes made on the site.

Hobby plans cap functions at 300 seconds. A refresh usually takes 25 seconds to 2.5 minutes.
