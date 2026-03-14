# CLAUDE.md — web/

React dashboard for RAS Agent. Deployed to Cloudflare Pages at `ras-agent.pages.dev`.

## Commands

```bash
npm ci              # install deps (uses lockfile)
npm run dev         # Vite dev server on :5173
npm run build       # production build → dist/
npm run lint        # ESLint
npm run preview     # serve production build locally
```

## Stack

- Vite 8 + React 19 + Tailwind CSS 3
- MapLibre GL JS (loaded from CDN in `MapViewer.jsx`, not npm)
- No routing library — single-page app with component state

## Key Files

- `src/App.jsx` — Main app: job list, submit form, stats dashboard, MapViewer
- `src/MapViewer.jsx` — Flood extent map with per-return-period toggle layers (10/50/100yr), MapLibre GL JS, OSM tiles
- `src/api.js` — Fetch wrapper for FastAPI backend (`VITE_API_URL`, defaults to `http://localhost:8000`)
- `wrangler.toml` — Cloudflare Pages config, build output `dist/`

## Patterns

- **Polling:** Jobs + stats refresh every 10s, health check every 30s
- **Mock data:** API returns sample IL polygons for mock jobs — dashboard is demo-ready without HEC-RAS
- **API URL:** Set `VITE_API_URL` env var or `.env` file. See `.env.example`.
- **Custom colors:** `tailwind.config.js` defines `navy`, `teal`, `teal-light` brand colors
