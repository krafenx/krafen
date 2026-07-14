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
- image/static assets such as `favicon.png`, `1illustration.png`, `CNAME`, `sitemap.xml`

The old `stream.html`, `test.html`, `wall.html`, and Pages Functions files were removed.

## Required Cloudflare settings

Create or keep one Workers KV namespace and bind it as:

- `WATCHLIST`

Add these secrets:

- `ADMIN_PASSWORD`
- `TMDB_ACCESS_TOKEN`
- `LASTFM_API_KEY`
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
- cached temporary IGDB/Twitch access tokens (read first; written only when a token is issued or renewed)

The homepage checks Twitch at most once every five minutes per open tab. The Worker additionally keeps a five-minute Twitch status cache in the Workers Cache API, so cache hits do not make the two Helix API requests and do not write to KV. If Twitch rejects a cached access token with `401`, the Worker requests and stores one replacement token, then retries the status request once.

The Last.fm API key is kept in the `LASTFM_API_KEY` secret and never sent to browsers. Set the non-secret `LASTFM_USER` variable in `wrangler.jsonc` if the profile changes. The Worker caches recent-track data for five minutes. Public watchlist and gamelist responses are cached for five minutes and the local Workers Cache entry is cleared after an admin save.

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

HTML responses get a per-request CSP nonce from `worker.js`, so the site's own inline scripts are still nonce-protected. Tailwind is loaded through `cdn.tailwindcss.com` to preserve the existing design, so the CSP allows Tailwind's runtime requirements. `_headers` keeps a static fallback for non-Worker static serving, but the Worker response headers are the production path.
