# Watchlist setup for Cloudflare

This page can run either as Cloudflare Pages Functions or as one Cloudflare Worker with static assets.

## Files

- `watchlist.html` - the public watchlist page and mini admin panel.
- `gamelist.html` - the public game list page with ratings and reviews.
- `testwatchlist.html` - same page kept as the working IDE copy.
- `functions/api/watchlist.js` - public list read and admin-only save.
- `functions/api/search.js` - search proxy for Shikimori and TMDB.
- `functions/api/gamelist.js` - public game list read and admin-only save.
- `functions/api/game-search.js` - server-side IGDB search proxy.
- `functions/api/twitch.js` - server-side Twitch channel/stream status proxy for the home page.
- `functions/api/admin.js` - checks the admin password.
- `worker.js` - same backend, but for a single Cloudflare Worker with static assets.
- `wrangler.jsonc` - Worker deploy config for the current Cloudflare Workers UI.
- `.assetsignore` - prevents backend/config files from being uploaded as public assets.

## Cloudflare setup

Important: do not deploy this through the Cloudflare browser uploader that says it only supports static assets.
That uploader accepts only HTML, CSS and JS static files, so it will ignore or reject the `functions/api` backend.
Use the GitHub-connected Cloudflare Pages project, or deploy with Wrangler.

If the dashboard page says `Worker` / `static assets` and bindings or variables do not save after redeploy,
create a separate **Pages** project from the GitHub repository:

1. Open `Workers & Pages`.
2. Click `Create application`.
3. Choose `Pages`, not `Worker`.
4. Choose `Import an existing Git repository`.
5. Select the repository with these files.
6. Leave build command empty if this is a plain HTML site.
7. Set output directory to `/` or leave the default if your repo root is deployed.
8. Deploy.

The `functions/api` folder is a Cloudflare Pages Functions convention. A static-assets Worker project will not pick
it up from the browser uploader.

## Worker static assets setup

Use this path if Cloudflare keeps showing the project as `Worker` / `static assets`.
This uses `worker.js` instead of the `functions/api` folder.

1. Push these files to GitHub:
   - `worker.js`
   - `wrangler.jsonc`
   - `.assetsignore`
   - `watchlist.html`
   - the rest of the static site files
2. In Cloudflare, connect the GitHub repo to a Worker build, or deploy with:
   `npx wrangler deploy`
3. Open `Workers KV -> krafen-watchlist` and copy the Namespace ID.
4. In `wrangler.jsonc`, replace `PASTE_KRAFEN_WATCHLIST_NAMESPACE_ID_HERE` with that Namespace ID.
5. Push `wrangler.jsonc` and redeploy. The `WATCHLIST` binding will now persist after every deploy.
6. Add secrets in the Worker settings, not as graph bindings:
   - `Settings -> Variables and Secrets -> Add -> Secret -> ADMIN_PASSWORD`
   - `Settings -> Variables and Secrets -> Add -> Secret -> TMDB_ACCESS_TOKEN`
   - `Settings -> Variables and Secrets -> Add -> Secret -> TWITCH_CLIENT_ID`
   - `Settings -> Variables and Secrets -> Add -> Secret -> TWITCH_CLIENT_SECRET`
7. Deploy the secret changes.

The important difference is that `worker.js` makes the project no longer "static assets only",
so Cloudflare can attach runtime bindings and secrets.

1. Create a Workers KV namespace in Cloudflare.
2. Open your Cloudflare Pages project.
3. Go to `Settings -> Bindings -> Add -> KV namespace`.
4. Set the binding variable name to `WATCHLIST` and select the namespace.
5. Go to `Settings -> Environment variables`.
6. Add `ADMIN_PASSWORD` with your private admin password.
7. Add `TMDB_ACCESS_TOKEN` if you want movie and series search. `TMDB_API_KEY` is also supported as a fallback,
   but the TMDB read access token is preferred.
8. Add `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` if you want the home page Twitch card and `/gamelist`
   game search through IGDB.
8. Redeploy the Pages project.

Anime search uses Shikimori and does not need a key.
Movie and series search uses TMDB. Create a TMDB API key/read access token in your TMDB account settings, then add
`TMDB_ACCESS_TOKEN` as a Cloudflare Pages secret/environment variable before redeploying the GitHub-connected Pages
project.
The home page Twitch card uses the official Twitch Helix API to show live/offline status, stream title, game, viewers,
and profile data.
Game search uses IGDB, which authenticates through a Twitch Developer application. Create a Twitch developer app,
copy its client ID, generate a client secret, then add them as `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` in
Cloudflare. The backend caches the temporary IGDB access token in the existing `WATCHLIST` KV namespace.

## How it works

Visitors can open the page and read `/api/watchlist`.
Only an active admin session can write the saved list through `/api/watchlist`.
The admin password is posted only to `/api/admin`; the backend returns a signed `HttpOnly`, `Secure`,
`SameSite=Strict` cookie for the current browser session. Frontend JavaScript no longer stores or resends
the raw password.

The API also has basic KV-backed rate limits for admin login, search, and watchlist writes. If `WATCHLIST`
is missing, rate limiting is skipped because there is no shared storage available.

The current CSP still allows inline scripts/styles and Tailwind CDN because the site is a plain HTML project.
For a stricter production CSP, install Node/npm locally, build Tailwind into a static CSS file, and then move the
inline page scripts/styles into versioned local assets.

TMDB is used for movies and series because it has a public API with localized titles and poster paths. The secret stays
server-side in the Cloudflare Function/Worker; the browser only calls `/api/search`.
IGDB is used for games. The browser only calls `/api/game-search`; Twitch credentials and IGDB access tokens stay
server-side in Cloudflare.
The browser only calls `/api/twitch` for Twitch status; the OAuth client secret and temporary app access token also stay
server-side in Cloudflare.
