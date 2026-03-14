---
description: Conventions for the React web dashboard
globs: web/**
---

# Web Dashboard Conventions

## Stack
- React 19 + Vite 8 + Tailwind CSS 3
- MapLibre GL JS loaded from **CDN** in `MapViewer.jsx` — do NOT install via npm
- No routing library — single-page app with component state
- Deployed to Cloudflare Pages (`wrangler.toml`)

## API Integration
- All backend calls go through `src/api.js` — never call `fetch()` directly from components
- Base URL from `VITE_API_URL` env var (defaults to `http://localhost:8000`)
- Polling: jobs/stats refresh every 10s, health check every 30s

## Brand Colors
Defined in `tailwind.config.js`:
- `navy` — primary dark
- `teal` — primary accent
- `teal-light` — secondary accent

## Map Layers
- `MapViewer.jsx` renders flood extents with per-return-period toggle layers (10/50/100yr)
- Layers use GeoJSON sources from the API
- OSM base tiles

## Dependencies
- **No new npm dependencies** without explicit user approval
- Keep the bundle small — this is a lightweight dashboard

## Build & Lint
```bash
npm ci          # install from lockfile
npm run dev     # dev server :5173
npm run build   # production build → dist/
npm run lint    # ESLint — must pass before commit
```
