# Cloudflare Worker setup

This project is deployed as one Cloudflare Worker with static assets. The backend lives in `worker.js`; there is no `functions/api` Pages Functions backend anymore.

## Files to keep in the Worker deploy

- `worker.js`
- `wrangler.jsonc`
- `_headers`
- `.assetsignore`
- `index.html`
- `watchlist.html`
- `gamelist.html`
- `tasks.html`
- `styles/tailwind-lite.css`
- image/static assets such as `favicon.png`, `1illustration.png`, `CNAME`, `sitemap.xml`

The old `stream.html`, `test.html`, `wall.html`, and Pages Functions files were removed.

## Required Cloudflare settings

Create or keep one Workers KV namespace and bind it as:

- `WATCHLIST`

Add these secrets:

- `ADMIN_PASSWORD`
- `TMDB_ACCESS_TOKEN`
- `TWITCH_CLIENT_ID`
- `TWITCH_CLIENT_SECRET`

The Worker also uses Cloudflare Workers Rate Limiting bindings configured in `wrangler.jsonc`:

- `ADMIN_LOGIN_RATE_LIMIT` - admin login attempts
- `WRITE_RATE_LIMIT` - watchlist/gamelist saves
- `GAME_SEARCH_RATE_LIMIT` - IGDB search
- `PUBLIC_API_RATE_LIMIT` - public search and Twitch status

Use Wrangler 4.36 or newer when deploying the `ratelimits` config:

```bash
npx wrangler deploy
```

## Why KV usage is lower now

The removed stream/test pages used polling and could create thousands of KV reads per open tab. They are gone.

Rate limiting no longer stores counters in KV. It uses Cloudflare Rate Limiting bindings instead, so login/search/write throttling does not spend daily Workers KV operations.

KV is still used for:

- watchlist data
- gamelist data
- cached temporary IGDB/Twitch access tokens

## Data saves

`/api/watchlist` and `/api/gamelist` return:

```json
{
  "items": [],
  "revision": 0
}
```

Admin saves must send the current `revision`. If another tab has already saved a newer version, the Worker returns `409 revision_conflict` with the current server copy. The browser then reloads the current list instead of silently overwriting it.

Existing KV data stored as a plain array is still supported. After the next admin save it is migrated to:

```json
{
  "revision": 1,
  "items": []
}
```

## Security notes

The site no longer loads Tailwind from `cdn.tailwindcss.com`. Utility styles are served locally from `styles/tailwind-lite.css`.

HTML responses get a per-request CSP nonce from `worker.js`, so inline page scripts and style blocks can run without script `unsafe-inline` or `unsafe-eval` in the Worker CSP. Style attributes remain allowed because the UI updates dynamic widths, colors and backgrounds from JavaScript. `_headers` keeps a static fallback for non-Worker static serving, but the Worker response headers are the production path.
