# Watchlist setup for Cloudflare

This page can run either as Cloudflare Pages Functions or as one Cloudflare Worker with static assets.

## Files

- `watchlist.html` - the public watchlist page and mini admin panel.
- `testwatchlist.html` - same page kept as the working IDE copy.
- `functions/api/watchlist.js` - public list read and admin-only save.
- `functions/api/search.js` - search proxy for Shikimori and OMDb.
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
   - `Settings -> Variables and Secrets -> Add -> Secret -> OMDB_API_KEY`
7. Deploy the secret changes.

The important difference is that `worker.js` makes the project no longer "static assets only",
so Cloudflare can attach runtime bindings and secrets.

1. Create a Workers KV namespace in Cloudflare.
2. Open your Cloudflare Pages project.
3. Go to `Settings -> Bindings -> Add -> KV namespace`.
4. Set the binding variable name to `WATCHLIST` and select the namespace.
5. Go to `Settings -> Environment variables`.
6. Add `ADMIN_PASSWORD` with your private admin password.
7. Add `OMDB_API_KEY` if you want movie and series search.
8. Redeploy the Pages project.

Anime search uses Shikimori and does not need a key.

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

OMDb is used because IMDb does not provide a simple public browser-friendly search API. If you later get access to an official IMDb provider, only `functions/api/search.js` needs to change.
