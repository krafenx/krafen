# krafen.online

Static personal site served by a single Cloudflare Worker with static assets.

## What is inside

- `index.html` - home page with social links, GitHub, Last.fm, Discord invite and Twitch status widgets.
- `watchlist.html` - public watchlist with admin-only editing.
- `gamelist.html` - public game list with admin-only editing and IGDB search.
- `tasks.html` - local task/progress calculator.
- `worker.js` - API backend and static asset security headers.
- `wrangler.jsonc` - Cloudflare Worker config, KV binding, secrets and rate limiting bindings.

Removed pages: `stream.html`, `test.html`, `wall.html`.

## Deploy

Use Wrangler or a GitHub-connected Cloudflare Worker deploy:

```bash
npx wrangler deploy
```

Required Cloudflare bindings:

- KV namespace: `WATCHLIST`
- Secrets: `ADMIN_PASSWORD`, `TMDB_ACCESS_TOKEN`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`
- Rate limit bindings from `wrangler.jsonc`: `ADMIN_LOGIN_RATE_LIMIT`, `WRITE_RATE_LIMIT`, `GAME_SEARCH_RATE_LIMIT`, `PUBLIC_API_RATE_LIMIT`

See `WATCHLIST_SETUP.md` for the longer setup notes.
