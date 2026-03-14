---
description: Infrastructure and deployment conventions
globs: Dockerfile,docker-compose.yml,docker-compose.yaml,.github/**,docker/**
---

# DevOps Conventions

## Docker
- Base image: Python 3.11 slim
- **GDAL install order matters:**
  1. `apt-get install libgdal-dev gdal-bin libgeos-dev libproj-dev`
  2. `pip install gdal==$(gdal-config --version)` (must match system version)
  3. `pip install -r requirements.txt`
- Volumes: mount `./output` and `./data` into container
- Profiles: `docker-compose --profile dev up` adds web dev server

## CI (GitHub Actions)
Two-job structure in `.github/workflows/ci.yml`:
1. **test-pipeline:** Ubuntu + system GDAL + pytest
2. **build-web:** Node 20 + npm ci + npm run build + upload artifact

Both run on push/PR to `main`.

## Cloudflare Pages
- Web dashboard auto-deploys from `main` branch
- Config in `web/wrangler.toml`
- Build output: `web/dist/`

## Cloudflare R2 (Results Storage)
Required env vars:
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET_NAME`, `R2_PUBLIC_URL`, `R2_PREFIX`

## Environment Variables
- `JOBS_DB_PATH` — SQLite job database path (default: `data/jobs.db`)
- `VITE_API_URL` — Frontend API base URL
- R2 vars above for cloud storage
