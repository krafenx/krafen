const KV_KEY = 'items';
const GAME_KV_KEY = 'games';
const IGDB_TOKEN_KEY = 'igdb:token';
const TWITCH_TOKEN_KEY = 'twitch:token';
const SHIKIMORI_BASE = 'https://shikimori.one';
const TMDB_BASE = 'https://api.themoviedb.org';
const TMDB_IMAGE_BASE = 'https://image.tmdb.org/t/p/w342';
const IGDB_BASE = 'https://api.igdb.com/v4';
const TWITCH_API_BASE = 'https://api.twitch.tv/helix';
const TWITCH_TOKEN_URL = 'https://id.twitch.tv/oauth2/token';
const LASTFM_API_BASE = 'https://ws.audioscrobbler.com/2.0/';
const TWITCH_STATUS_CACHE_TTL_SECONDS = 5 * 60;
const LASTFM_CACHE_TTL_SECONDS = 5 * 60;
const PUBLIC_LIST_CACHE_TTL_SECONDS = 5 * 60;
const SESSION_COOKIE = 'kf_admin_session';
const SESSION_TTL_SECONDS = 60 * 60 * 8;
const MAX_LOGIN_BODY = 2_048;
const MAX_WATCHLIST_BODY = 100_000;
const MAX_GAMELIST_BODY = 1_500_000;
const VALID_TYPES = new Set(['anime', 'movie', 'series']);
const VALID_STATUSES = new Set(['watched', 'planned', 'dropped']);
const VALID_GAME_STATUSES = new Set(['completed', 'playing', 'dropped', 'wishlist', 'paused']);
const ITEM_ID_PATTERN = /^[A-Za-z0-9:_-]{1,120}$/;
const CSP_NONCE_PLACEHOLDER = '__CSP_NONCE__';

const BASE_SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=()',
    'Cross-Origin-Opener-Policy': 'same-origin',
};

function contentSecurityPolicy(nonce = '') {
    const scriptSrc = nonce
        ? `script-src 'self' 'nonce-${nonce}' 'unsafe-eval' https://cdn.tailwindcss.com`
        : "script-src 'self' https://cdn.tailwindcss.com";
    const styleSrc = nonce
        ? `'self' 'nonce-${nonce}' 'unsafe-inline' https://fonts.googleapis.com`
        : "'self' 'unsafe-inline' https://fonts.googleapis.com";

    return [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        scriptSrc,
        `style-src ${styleSrc}`,
        `style-src-elem ${styleSrc}`,
        "style-src-attr 'unsafe-inline'",
        'font-src https://fonts.gstatic.com',
        "img-src 'self' data: https:",
        "connect-src 'self' https://api.github.com https://discord.com https://shikimori.one https://api.themoviedb.org",
    ].join('; ');
}

function securityHeaders(nonce = '') {
    return {
        ...BASE_SECURITY_HEADERS,
        'Content-Security-Policy': contentSecurityPolicy(nonce),
    };
}

async function withSecurityHeaders(response) {
    const headers = new Headers(response.headers);
    const isHtml = (headers.get('Content-Type') || '').includes('text/html');

    if (isHtml) {
        const nonce = createNonce();
        const body = (await response.text()).replaceAll(CSP_NONCE_PLACEHOLDER, nonce);
        Object.entries(securityHeaders(nonce)).forEach(([key, value]) => headers.set(key, value));
        return new Response(body, {
            status: response.status,
            statusText: response.statusText,
            headers,
        });
    }

    Object.entries(securityHeaders()).forEach(([key, value]) => headers.set(key, value));
    return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers,
    });
}

function json(body, status = 200, extraHeaders = {}) {
    return new Response(JSON.stringify(body), {
        status,
        headers: {
            ...securityHeaders(),
            'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store',
            ...extraHeaders,
        },
    });
}

function text(value, max = 200) {
    return String(value || '').trim().slice(0, max);
}

function cleanUrl(value, max = 600) {
    const url = text(value, max);
    if (!url) return null;

    try {
        return new URL(url).protocol === 'https:' ? url : null;
    } catch {
        return null;
    }
}

function yearFrom(value) {
    const match = String(value || '').match(/\d{4}/);
    return match ? match[0] : '-';
}

function tmdbPoster(value) {
    const path = text(value, 120);
    return path && path.startsWith('/') ? `${TMDB_IMAGE_BASE}${path}` : null;
}

function igdbCover(imageId) {
    const id = text(imageId, 80);
    return id ? `https://images.igdb.com/igdb/image/upload/t_cover_big/${id}.jpg` : null;
}

function yearFromUnix(value) {
    const ts = Number(value);
    if (!Number.isFinite(ts) || ts <= 0) return '-';
    return String(new Date(ts * 1000).getUTCFullYear());
}

function escapeIgdbString(value) {
    return text(value, 80).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

function createNonce() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return base64UrlEncode(bytes);
}

class RequestBodyError extends Error {
    constructor(code) {
        super(code);
        this.code = code;
    }
}

async function readJsonLimited(request, maxBytes) {
    const length = Number(request.headers.get('Content-Length') || 0);
    if (Number.isFinite(length) && length > maxBytes) {
        throw new RequestBodyError('payload_too_large');
    }

    if (!request.body) throw new RequestBodyError('invalid_json');

    const reader = request.body.getReader();
    const chunks = [];
    let total = 0;

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        total += value.byteLength;
        if (total > maxBytes) {
            await reader.cancel().catch(() => {});
            throw new RequestBodyError('payload_too_large');
        }
        chunks.push(value);
    }

    const bytes = new Uint8Array(total);
    let offset = 0;
    chunks.forEach(chunk => {
        bytes.set(chunk, offset);
        offset += chunk.byteLength;
    });

    const raw = new TextDecoder().decode(bytes);
    if (!raw.trim()) throw new RequestBodyError('invalid_json');

    try {
        return JSON.parse(raw);
    } catch {
        throw new RequestBodyError('invalid_json');
    }
}

function bodyErrorResponse(error) {
    if (!(error instanceof RequestBodyError)) return null;
    return json({ error: error.code }, error.code === 'payload_too_large' ? 413 : 400);
}

function requireStorage(env, writable = false) {
    if (!env.WATCHLIST || typeof env.WATCHLIST.get !== 'function' || (writable && typeof env.WATCHLIST.put !== 'function')) {
        throw new Error('watchlist_kv_missing');
    }
}

async function getSecret(env, name) {
    const value = env[name];
    if (!value) return '';
    if (typeof value === 'string') return value;
    if (typeof value.get === 'function') return await value.get();
    return '';
}

function parseCookies(request) {
    return Object.fromEntries((request.headers.get('Cookie') || '')
        .split(';')
        .map(cookie => cookie.trim().split('='))
        .filter(([name, value]) => name && value)
        .map(([name, value]) => [name, value]));
}

function base64UrlEncode(value) {
    const bytes = typeof value === 'string' ? new TextEncoder().encode(value) : value;
    let binary = '';
    bytes.forEach(byte => { binary += String.fromCharCode(byte); });
    return btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
}

function base64UrlDecode(value) {
    const padded = value.replaceAll('-', '+').replaceAll('_', '/') + '==='.slice((value.length + 3) % 4);
    const binary = atob(padded);
    return Uint8Array.from(binary, char => char.charCodeAt(0));
}

async function hmac(secret, value) {
    const key = await crypto.subtle.importKey(
        'raw',
        new TextEncoder().encode(secret),
        { name: 'HMAC', hash: 'SHA-256' },
        false,
        ['sign'],
    );
    const signature = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(value));
    return base64UrlEncode(new Uint8Array(signature));
}

function constantTimeEqual(a, b) {
    if (a.length !== b.length) return false;
    let diff = 0;
    for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
    return diff === 0;
}

async function createSession(adminPassword) {
    const payload = base64UrlEncode(JSON.stringify({
        exp: Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS,
        nonce: crypto.randomUUID(),
    }));
    const signature = await hmac(adminPassword, payload);
    return `${payload}.${signature}`;
}

async function verifySession(request, env) {
    const token = parseCookies(request)[SESSION_COOKIE];
    const adminPassword = await getSecret(env, 'ADMIN_PASSWORD');
    if (!token || !adminPassword) return false;

    const [payload, signature] = token.split('.');
    if (!payload || !signature) return false;

    const expected = await hmac(adminPassword, payload);
    if (!constantTimeEqual(signature, expected)) return false;

    try {
        const data = JSON.parse(new TextDecoder().decode(base64UrlDecode(payload)));
        return Number(data.exp) > Math.floor(Date.now() / 1000);
    } catch {
        return false;
    }
}

async function requireAdmin(request, env) {
    if (!await verifySession(request, env)) {
        return json({ error: 'unauthorized' }, 401);
    }
    return null;
}

function sessionCookie(value) {
    return `${SESSION_COOKIE}=${value}; Max-Age=${SESSION_TTL_SECONDS}; Path=/; HttpOnly; Secure; SameSite=Strict`;
}

function clearSessionCookie() {
    return `${SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Strict`;
}

async function rateLimit(request, env, bindingName, windowSeconds) {
    const limiter = env[bindingName];
    if (!limiter || typeof limiter.limit !== 'function') {
        return json({ error: 'rate_limiter_missing' }, 503);
    }

    const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
    const result = await limiter.limit({ key: ip });
    if (!result.success) return json({ error: 'rate_limited' }, 429, { 'Retry-After': String(windowSeconds) });

    return null;
}

function normalizeItem(item) {
    const id = text(item.id, 120);
    const title = text(item.title, 180);
    const type = text(item.type, 20);
    const status = text(item.status, 20);

    if (!ITEM_ID_PATTERN.test(id) || !title || !VALID_TYPES.has(type) || !VALID_STATUSES.has(status)) {
        return null;
    }

    return {
        id,
        title,
        titleRu: text(item.titleRu, 180) || null,
        year: text(item.year, 24) || '-',
        type,
        status,
        poster: cleanUrl(item.poster),
        addedAt: Number.isFinite(Number(item.addedAt)) ? Number(item.addedAt) : Date.now(),
        updatedAt: Number.isFinite(Number(item.updatedAt)) ? Number(item.updatedAt) : Date.now(),
    };
}

function cleanStringArray(value, maxItems, maxText) {
    return Array.isArray(value)
        ? value.map(item => text(item, maxText)).filter(Boolean).slice(0, maxItems)
        : [];
}

function normalizeGameItem(item) {
    const id = text(item.id, 120);
    const title = text(item.title, 180);
    const status = text(item.status, 20);
    const rating = Math.max(0, Math.min(10, Number(item.rating) || 0));

    if (!ITEM_ID_PATTERN.test(id) || !title || !VALID_GAME_STATUSES.has(status)) {
        return null;
    }

    return {
        id,
        igdbId: Number.isFinite(Number(item.igdbId)) ? Number(item.igdbId) : null,
        title,
        year: text(item.year, 24) || '-',
        status,
        rating: Math.round(rating * 10) / 10,
        review: text(item.review, 5000),
        cover: cleanUrl(item.cover),
        genres: cleanStringArray(item.genres, 5, 32),
        platforms: cleanStringArray(item.platforms, 8, 32),
        category: Number.isFinite(Number(item.category)) ? Number(item.category) : 0,
        addedAt: Number.isFinite(Number(item.addedAt)) ? Number(item.addedAt) : Date.now(),
        updatedAt: Number.isFinite(Number(item.updatedAt)) ? Number(item.updatedAt) : Date.now(),
    };
}

function normalizeStoredList(value) {
    if (Array.isArray(value)) return { revision: 0, items: value };

    if (value && typeof value === 'object' && Array.isArray(value.items)) {
        const revision = Number(value.revision);
        return {
            revision: Number.isSafeInteger(revision) && revision >= 0 ? revision : 0,
            items: value.items,
        };
    }

    return { revision: 0, items: [] };
}

function expectedRevision(value) {
    const revision = Number(value);
    return Number.isSafeInteger(revision) && revision >= 0 ? revision : null;
}

function revisionConflict(state, error = 'revision_conflict') {
    return json({
        error,
        items: state.items,
        revision: state.revision,
    }, 409);
}

async function handleAdmin(request, env) {
    if (request.method === 'GET') {
        return json({ ok: await verifySession(request, env) });
    }

    if (request.method === 'DELETE') {
        return json({ ok: true }, 200, { 'Set-Cookie': clearSessionCookie() });
    }

    if (request.method !== 'POST') return json({ error: 'method_not_allowed' }, 405);

    const limited = await rateLimit(request, env, 'ADMIN_LOGIN_RATE_LIMIT', 60);
    if (limited) return limited;

    let body;
    try {
        body = await readJsonLimited(request, MAX_LOGIN_BODY);
    } catch (error) {
        return bodyErrorResponse(error) || json({ error: error.message || 'invalid_request' }, 400);
    }

    const adminPassword = await getSecret(env, 'ADMIN_PASSWORD');
    if (!adminPassword) return json({ error: 'admin_password_missing' }, 500);

    if (!body.password || !constantTimeEqual(String(body.password), adminPassword)) {
        return json({ error: 'invalid_password' }, 401);
    }

    return json({ ok: true }, 200, { 'Set-Cookie': sessionCookie(await createSession(adminPassword)) });
}

async function handleWatchlist(request, env) {
    if (request.method === 'GET') {
        try {
            const cacheKey = publicListCacheKey(request, '/api/watchlist');
            const cachedResponse = await readWorkerCache(cacheKey);
            if (cachedResponse) return cachedResponse;

            requireStorage(env);
            const state = normalizeStoredList(await env.WATCHLIST.get(KV_KEY, { type: 'json' }));
            const response = json({ items: state.items, revision: state.revision }, 200, {
                'Cache-Control': `public, max-age=${PUBLIC_LIST_CACHE_TTL_SECONDS}`,
            });
            await writeWorkerCache(cacheKey, response);
            return response;
        } catch (error) {
            return json({ error: error.message || 'storage_error', items: [], revision: 0 }, 500);
        }
    }

    if (request.method === 'PUT') {
        const limited = await rateLimit(request, env, 'WRITE_RATE_LIMIT', 60);
        if (limited) return limited;

        const authError = await requireAdmin(request, env);
        if (authError) return authError;

        try {
            requireStorage(env, true);
            const body = await readJsonLimited(request, MAX_WATCHLIST_BODY);
            const state = normalizeStoredList(await env.WATCHLIST.get(KV_KEY, { type: 'json' }));
            const revision = expectedRevision(body.revision);
            if (revision === null) return revisionConflict(state, 'revision_required');
            if (revision !== state.revision) return revisionConflict(state);

            const rawItems = Array.isArray(body.items) ? body.items : [];
            const items = rawItems.map(normalizeItem).filter(Boolean).slice(0, 500);
            const nextRevision = state.revision + 1;
            await env.WATCHLIST.put(KV_KEY, JSON.stringify({ revision: nextRevision, items }));
            await deleteWorkerCache(publicListCacheKey(request, '/api/watchlist'));
            return json({ ok: true, items, revision: nextRevision });
        } catch (error) {
            const bodyError = bodyErrorResponse(error);
            if (bodyError) return bodyError;
            return json({ error: error.message || 'save_failed' }, 500);
        }
    }

    return json({ error: 'method_not_allowed' }, 405);
}

async function handleGamelist(request, env) {
    if (request.method === 'GET') {
        try {
            const cacheKey = publicListCacheKey(request, '/api/gamelist');
            const cachedResponse = await readWorkerCache(cacheKey);
            if (cachedResponse) return cachedResponse;

            requireStorage(env);
            const state = normalizeStoredList(await env.WATCHLIST.get(GAME_KV_KEY, { type: 'json' }));
            const response = json({ items: state.items, revision: state.revision }, 200, {
                'Cache-Control': `public, max-age=${PUBLIC_LIST_CACHE_TTL_SECONDS}`,
            });
            await writeWorkerCache(cacheKey, response);
            return response;
        } catch (error) {
            return json({ error: error.message || 'storage_error', items: [], revision: 0 }, 500);
        }
    }

    if (request.method === 'PUT') {
        const limited = await rateLimit(request, env, 'WRITE_RATE_LIMIT', 60);
        if (limited) return limited;

        const authError = await requireAdmin(request, env);
        if (authError) return authError;

        try {
            requireStorage(env, true);
            const body = await readJsonLimited(request, MAX_GAMELIST_BODY);
            const state = normalizeStoredList(await env.WATCHLIST.get(GAME_KV_KEY, { type: 'json' }));
            const revision = expectedRevision(body.revision);
            if (revision === null) return revisionConflict(state, 'revision_required');
            if (revision !== state.revision) return revisionConflict(state);

            const rawItems = Array.isArray(body.items) ? body.items : [];
            const items = rawItems.map(normalizeGameItem).filter(Boolean).slice(0, 500);
            const nextRevision = state.revision + 1;
            await env.WATCHLIST.put(GAME_KV_KEY, JSON.stringify({ revision: nextRevision, items }));
            await deleteWorkerCache(publicListCacheKey(request, '/api/gamelist'));
            return json({ ok: true, items, revision: nextRevision });
        } catch (error) {
            const bodyError = bodyErrorResponse(error);
            if (bodyError) return bodyError;
            return json({ error: error.message || 'save_failed' }, 500);
        }
    }

    return json({ error: 'method_not_allowed' }, 405);
}

async function searchTmdb(query, type, env) {
    const tmdbAccessToken = await getSecret(env, 'TMDB_ACCESS_TOKEN');
    const tmdbApiKey = await getSecret(env, 'TMDB_API_KEY');
    if (!tmdbAccessToken && !tmdbApiKey) return json({ error: 'tmdb_key_missing' }, 500);

    const isSeries = type === 'series';
    const url = new URL(isSeries ? '/3/search/tv' : '/3/search/movie', TMDB_BASE);
    url.searchParams.set('query', query);
    url.searchParams.set('language', 'ru-RU');
    url.searchParams.set('include_adult', 'false');
    url.searchParams.set('page', '1');
    if (!tmdbAccessToken) {
        url.searchParams.set('api_key', tmdbApiKey);
    }

    const headers = { 'Accept': 'application/json' };
    if (tmdbAccessToken) {
        headers.Authorization = `Bearer ${tmdbAccessToken.replace(/^Bearer\s+/i, '')}`;
    }

    const res = await fetch(url, { headers });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return json({ error: 'tmdb_unavailable' }, 502);

    const results = (Array.isArray(data.results) ? data.results : []).slice(0, 10).map(item => {
        const localizedTitle = text(isSeries ? item.name : item.title);
        const originalTitle = text(isSeries ? item.original_name : item.original_title) || localizedTitle;

        return {
            id: `tmdb_${isSeries ? 'tv' : 'movie'}_${text(item.id, 40)}`,
            title: originalTitle,
            titleRu: localizedTitle && localizedTitle !== originalTitle ? localizedTitle : null,
            year: yearFrom(isSeries ? item.first_air_date : item.release_date),
            type,
            poster: cleanUrl(tmdbPoster(item.poster_path)),
        };
    }).filter(item => item.id && item.title);

    return json({ results });
}

async function searchShikimori(query) {
    const url = new URL('/api/animes', SHIKIMORI_BASE);
    url.searchParams.set('search', query);
    url.searchParams.set('limit', '10');
    url.searchParams.set('order', 'popularity');

    const res = await fetch(url, {
        headers: {
            'Accept': 'application/json',
            'User-Agent': 'krafen-watchlist/1.0',
        },
    });
    const data = await res.json().catch(() => []);
    if (!res.ok || !Array.isArray(data)) {
        return json({ error: 'shikimori_unavailable' }, 502);
    }

    const results = data.map(item => ({
        id: `sh_${item.id}`,
        title: text(item.name),
        titleRu: text(item.russian) || null,
        year: yearFrom(item.aired_on),
        type: 'anime',
        poster: item.image?.original ? cleanUrl(`${SHIKIMORI_BASE}${item.image.original}`) : null,
    })).filter(item => item.id && item.title);

    return json({ results });
}

async function getIgdbToken(env, clientId) {
    const staticToken = await getSecret(env, 'IGDB_ACCESS_TOKEN');
    if (staticToken) return staticToken.replace(/^Bearer\s+/i, '');

    if (env.WATCHLIST && typeof env.WATCHLIST.get === 'function') {
        const cached = await env.WATCHLIST.get(IGDB_TOKEN_KEY, { type: 'json' }).catch(() => null);
        if (cached?.accessToken && Number(cached.expiresAt) > Date.now() + 60_000) {
            return cached.accessToken;
        }
    }

    const clientSecret = await getSecret(env, 'TWITCH_CLIENT_SECRET') || await getSecret(env, 'IGDB_CLIENT_SECRET');
    if (!clientId || !clientSecret) return '';

    const url = new URL(TWITCH_TOKEN_URL);
    url.searchParams.set('client_id', clientId);
    url.searchParams.set('client_secret', clientSecret);
    url.searchParams.set('grant_type', 'client_credentials');

    const res = await fetch(url, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.access_token) return '';

    const accessToken = text(data.access_token, 3000);
    const expiresIn = Number(data.expires_in) || 3600;
    if (env.WATCHLIST && typeof env.WATCHLIST.put === 'function') {
        await env.WATCHLIST.put(IGDB_TOKEN_KEY, JSON.stringify({
            accessToken,
            expiresAt: Date.now() + Math.max(60, expiresIn - 300) * 1000,
        }), { expirationTtl: Math.max(60, expiresIn - 300) });
    }

    return accessToken;
}

function normalizeIgdbSearchItem(item) {
    const platforms = (item.platforms || [])
        .map(platform => text(platform.abbreviation || platform.name, 24))
        .filter(Boolean)
        .slice(0, 4);
    const genres = (item.genres || [])
        .map(genre => text(genre.name, 28))
        .filter(Boolean)
        .slice(0, 3);

    return {
        id: `igdb_${text(item.id, 40)}`,
        igdbId: Number(item.id) || null,
        title: text(item.name, 180),
        year: yearFromUnix(item.first_release_date),
        type: 'game',
        cover: igdbCover(item.cover?.image_id),
        genres,
        platforms,
        category: Number.isFinite(Number(item.category)) ? Number(item.category) : 0,
    };
}

async function searchIgdb(query, env) {
    const clientId = await getSecret(env, 'TWITCH_CLIENT_ID') || await getSecret(env, 'IGDB_CLIENT_ID');
    const accessToken = await getIgdbToken(env, clientId);
    if (!clientId || !accessToken) return json({ error: 'igdb_key_missing' }, 500);

    const body = [
        'fields name,cover.image_id,genres.name,platforms.abbreviation,platforms.name,first_release_date,category;',
        `search "${escapeIgdbString(query)}";`,
        'where version_parent = null;',
        'limit 10;',
    ].join(' ');

    const res = await fetch(`${IGDB_BASE}/games`, {
        method: 'POST',
        headers: {
            'Accept': 'application/json',
            'Client-ID': clientId,
            'Authorization': `Bearer ${accessToken}`,
        },
        body,
    });
    const data = await res.json().catch(() => []);
    if (!res.ok || !Array.isArray(data)) return json({ error: 'igdb_unavailable' }, 502);

    const results = data.map(normalizeIgdbSearchItem).filter(item => item.id && item.title);
    return json({ results });
}

async function handleGameSearch(request, env) {
    const url = new URL(request.url);
    const query = text(url.searchParams.get('q'), 80);

    if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
    if (query.length < 2) return json({ results: [] });

    const limited = await rateLimit(request, env, 'GAME_SEARCH_RATE_LIMIT', 60);
    if (limited) return limited;

    try {
        return await searchIgdb(query, env);
    } catch (error) {
        return json({ error: error.message || 'game_search_failed' }, 500);
    }
}

async function handleSearch(request, env) {
    const url = new URL(request.url);
    const query = text(url.searchParams.get('q'), 80);
    const type = text(url.searchParams.get('type'), 20);

    if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
    if (query.length < 2) return json({ results: [] });

    const limited = await rateLimit(request, env, 'PUBLIC_API_RATE_LIMIT', 60);
    if (limited) return limited;

    try {
        if (type === 'anime') return await searchShikimori(query);
        if (type === 'movie' || type === 'series') return await searchTmdb(query, type, env);
        return json({ error: 'invalid_type' }, 400);
    } catch (error) {
        return json({ error: error.message || 'search_failed' }, 500);
    }
}

function publicListCacheKey(request, pathname) {
    const url = new URL(request.url);
    url.pathname = pathname;
    url.search = '';
    return new Request(url.toString(), { method: 'GET' });
}

async function readWorkerCache(cacheKey) {
    if (!globalThis.caches?.default) return null;
    return await globalThis.caches.default.match(cacheKey).catch(() => null);
}

async function writeWorkerCache(cacheKey, response) {
    if (!globalThis.caches?.default) return;
    await globalThis.caches.default.put(cacheKey, response.clone()).catch(() => null);
}

async function deleteWorkerCache(cacheKey) {
    if (!globalThis.caches?.default) return;
    await globalThis.caches.default.delete(cacheKey).catch(() => null);
}

async function handleLastfm(request, env) {
    if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);

    const cacheKey = publicListCacheKey(request, '/api/lastfm');
    const cachedResponse = await readWorkerCache(cacheKey);
    if (cachedResponse) return cachedResponse;

    const limited = await rateLimit(request, env, 'PUBLIC_API_RATE_LIMIT', 60);
    if (limited) return limited;

    const apiKey = await getSecret(env, 'LASTFM_API_KEY');
    const user = text(env.LASTFM_USER || 'krafen', 80);
    if (!apiKey || !user) return json({ error: 'lastfm_key_missing' }, 500);

    try {
        const url = new URL(LASTFM_API_BASE);
        url.searchParams.set('method', 'user.getrecenttracks');
        url.searchParams.set('user', user);
        url.searchParams.set('api_key', apiKey);
        url.searchParams.set('format', 'json');
        url.searchParams.set('limit', '1');

        const res = await fetch(url);
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) return json({ error: 'lastfm_unavailable' }, 502);

        const rawTrack = data.recenttracks?.track?.[0] || null;
        const track = rawTrack ? {
            name: text(rawTrack.name, 200),
            artist: text(rawTrack.artist?.['#text'], 200),
            album: text(rawTrack.album?.['#text'], 200),
            image: cleanUrl(rawTrack.image?.[3]?.['#text'] || rawTrack.image?.[2]?.['#text']),
            nowPlaying: rawTrack['@attr']?.nowplaying === 'true',
        } : null;

        const response = json({ track }, 200, {
            'Cache-Control': `public, max-age=${LASTFM_CACHE_TTL_SECONDS}`,
        });
        await writeWorkerCache(cacheKey, response);
        return response;
    } catch {
        return json({ error: 'lastfm_unavailable' }, 502);
    }
}

async function getTwitchToken(env, clientId, forceRefresh = false) {
    const staticToken = await getSecret(env, 'TWITCH_ACCESS_TOKEN');
    if (staticToken) return staticToken.replace(/^Bearer\s+/i, '');

    if (!forceRefresh && env.WATCHLIST && typeof env.WATCHLIST.get === 'function') {
        const cached = await env.WATCHLIST.get(TWITCH_TOKEN_KEY, { type: 'json' }).catch(() => null);
        if (cached?.accessToken && Number(cached.expiresAt) > Date.now() + 60_000) {
            return cached.accessToken;
        }
    }

    const clientSecret = await getSecret(env, 'TWITCH_CLIENT_SECRET');
    if (!clientId || !clientSecret) return '';

    const url = new URL(TWITCH_TOKEN_URL);
    url.searchParams.set('client_id', clientId);
    url.searchParams.set('client_secret', clientSecret);
    url.searchParams.set('grant_type', 'client_credentials');

    const res = await fetch(url, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.access_token) return '';

    const accessToken = text(data.access_token, 3000);
    const expiresIn = Number(data.expires_in) || 3600;
    if (env.WATCHLIST && typeof env.WATCHLIST.put === 'function') {
        await env.WATCHLIST.put(TWITCH_TOKEN_KEY, JSON.stringify({
            accessToken,
            expiresAt: Date.now() + Math.max(60, expiresIn - 300) * 1000,
        }), { expirationTtl: Math.max(60, expiresIn - 300) });
    }

    return accessToken;
}

async function twitchGet(path, params, clientId, accessToken) {
    const url = new URL(path.replace(/^\/+/, ''), `${TWITCH_API_BASE}/`);
    Object.entries(params).forEach(([key, value]) => {
        if (value) url.searchParams.set(key, value);
    });

    const res = await fetch(url, {
        headers: {
            'Accept': 'application/json',
            'Client-ID': clientId,
            'Authorization': `Bearer ${accessToken}`,
        },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(res.status === 401 ? 'twitch_unauthorized' : 'twitch_unavailable');
    return data;
}

function twitchCacheKey(request, login) {
    const url = new URL(request.url);
    url.search = '';
    url.searchParams.set('login', login);
    return new Request(url.toString(), { method: 'GET' });
}

async function fetchTwitchStatus(login, clientId, accessToken) {
    const [users, streams] = await Promise.all([
        twitchGet('/users', { login }, clientId, accessToken),
        twitchGet('/streams', { user_login: login, first: '1' }, clientId, accessToken),
    ]);

    const user = users.data?.[0] || {};
    const stream = streams.data?.[0] || null;

    return {
        login,
        displayName: text(user.display_name || login, 80),
        profileImage: cleanUrl(user.profile_image_url),
        description: text(user.description, 220),
        viewCount: Number(user.view_count) || 0,
        url: `https://twitch.tv/${login}`,
        live: Boolean(stream),
        stream: stream ? {
            title: text(stream.title, 180),
            gameName: text(stream.game_name, 120),
            viewerCount: Number(stream.viewer_count) || 0,
            startedAt: text(stream.started_at, 80),
            language: text(stream.language, 12),
            thumbnail: cleanUrl(stream.thumbnail_url
                ?.replace('{width}', '640')
                ?.replace('{height}', '360')),
        } : null,
    };
}

async function handleTwitch(request, env) {
    const url = new URL(request.url);
    const login = text(url.searchParams.get('login') || 'krafen', 40).toLowerCase();
    if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
    if (!/^[a-z0-9_]{3,25}$/.test(login)) return json({ error: 'invalid_login' }, 400);

    const cacheKey = twitchCacheKey(request, login);
    const cachedResponse = await readWorkerCache(cacheKey);
    if (cachedResponse) return cachedResponse;

    const limited = await rateLimit(request, env, 'PUBLIC_API_RATE_LIMIT', 60);
    if (limited) return limited;

    const clientId = await getSecret(env, 'TWITCH_CLIENT_ID');
    let accessToken = await getTwitchToken(env, clientId);
    if (!clientId || !accessToken) return json({ error: 'twitch_key_missing' }, 500);

    try {
        let status;
        try {
            status = await fetchTwitchStatus(login, clientId, accessToken);
        } catch (error) {
            if (error.message !== 'twitch_unauthorized') throw error;

            accessToken = await getTwitchToken(env, clientId, true);
            if (!accessToken) throw error;
            status = await fetchTwitchStatus(login, clientId, accessToken);
        }

        const response = json(status, 200, {
            'Cache-Control': `public, max-age=${TWITCH_STATUS_CACHE_TTL_SECONDS}`,
        });
        await writeWorkerCache(cacheKey, response);
        return response;
    } catch (error) {
        return json({ error: error.message || 'twitch_unavailable' }, 502);
    }
}

export default {
    async fetch(request, env) {
        const url = new URL(request.url);

        if (request.method === 'OPTIONS') return json({ ok: true });
        if (url.pathname === '/api/admin') return handleAdmin(request, env);
        if (url.pathname === '/api/watchlist') return handleWatchlist(request, env);
        if (url.pathname === '/api/gamelist') return handleGamelist(request, env);
        if (url.pathname === '/api/search') return handleSearch(request, env);
        if (url.pathname === '/api/game-search') return handleGameSearch(request, env);
        if (url.pathname === '/api/lastfm') return handleLastfm(request, env);
        if (url.pathname === '/api/twitch') return handleTwitch(request, env);

        if (env.ASSETS) return await withSecurityHeaders(await env.ASSETS.fetch(request));
        return await withSecurityHeaders(new Response('Not found', { status: 404 }));
    },
};
