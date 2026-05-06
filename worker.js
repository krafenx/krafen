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
const LANYARD_API_BASE = 'https://api.lanyard.rest/v1';
const DISCORD_CDN_BASE = 'https://cdn.discordapp.com';
const SESSION_COOKIE = 'kf_admin_session';
const SESSION_TTL_SECONDS = 60 * 60 * 8;
const MAX_LOGIN_BODY = 2_048;
const MAX_WATCHLIST_BODY = 100_000;
const MAX_GAMELIST_BODY = 1_500_000;
const VALID_TYPES = new Set(['anime', 'movie', 'series']);
const VALID_STATUSES = new Set(['watched', 'planned', 'dropped']);
const VALID_GAME_STATUSES = new Set(['completed', 'playing', 'dropped', 'wishlist', 'paused']);
const ITEM_ID_PATTERN = /^[A-Za-z0-9:_-]{1,120}$/;
const DISCORD_SNOWFLAKE_PATTERN = /^\d{17,22}$/;
const DISCORD_INVITE_PATTERN = /^[A-Za-z0-9-]{2,64}$/;
const DISCORD_BADGE_FLAGS = [
    { bit: 1 << 0, id: 'staff', label: 'Discord Staff', icon: 'DS' },
    { bit: 1 << 1, id: 'partner', label: 'Partnered Server Owner', icon: 'PA' },
    { bit: 1 << 2, id: 'hypesquad', label: 'HypeSquad Events', icon: 'HS' },
    { bit: 1 << 3, id: 'bug_hunter_1', label: 'Bug Hunter', icon: 'BH' },
    { bit: 1 << 6, id: 'bravery', label: 'House Bravery', icon: 'HB' },
    { bit: 1 << 7, id: 'brilliance', label: 'House Brilliance', icon: 'HI' },
    { bit: 1 << 8, id: 'balance', label: 'House Balance', icon: 'HA' },
    { bit: 1 << 9, id: 'early_supporter', label: 'Early Nitro Supporter', icon: 'EN' },
    { bit: 1 << 14, id: 'bug_hunter_2', label: 'Bug Hunter Gold', icon: 'BG' },
    { bit: 1 << 16, id: 'verified_bot', label: 'Verified Bot', icon: 'VB' },
    { bit: 1 << 17, id: 'verified_developer', label: 'Early Verified Bot Developer', icon: 'VD' },
    { bit: 1 << 18, id: 'certified_moderator', label: 'Moderator Programs Alumni', icon: 'CM' },
    { bit: 1 << 19, id: 'http_interactions', label: 'HTTP Interactions Bot', icon: 'BI' },
];

const SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=()',
    'Cross-Origin-Opener-Policy': 'same-origin',
    'Content-Security-Policy': [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        'font-src https://fonts.gstatic.com',
        "img-src 'self' data: https:",
        "connect-src 'self' https://api.github.com https://ws.audioscrobbler.com https://discord.com https://shikimori.one https://api.themoviedb.org",
    ].join('; '),
};

function withSecurityHeaders(response) {
    const headers = new Headers(response.headers);
    Object.entries(SECURITY_HEADERS).forEach(([key, value]) => headers.set(key, value));
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
            ...SECURITY_HEADERS,
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

function rejectLargeBody(request, maxBytes) {
    const length = Number(request.headers.get('Content-Length') || 0);
    if (Number.isFinite(length) && length > maxBytes) {
        return json({ error: 'payload_too_large' }, 413);
    }
    return null;
}

function requireStorage(env) {
    if (!env.WATCHLIST || typeof env.WATCHLIST.get !== 'function') {
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

async function rateLimit(request, env, name, limit, windowSeconds) {
    if (!env.WATCHLIST || typeof env.WATCHLIST.get !== 'function' || typeof env.WATCHLIST.put !== 'function') {
        return null;
    }

    const ip = request.headers.get('CF-Connecting-IP') || request.headers.get('X-Forwarded-For') || 'unknown';
    const bucket = Math.floor(Date.now() / 1000 / windowSeconds);
    const key = `rate:${name}:${ip}:${bucket}`;
    const current = Number(await env.WATCHLIST.get(key) || 0);

    if (current >= limit) {
        return json({ error: 'rate_limited' }, 429, { 'Retry-After': String(windowSeconds) });
    }

    await env.WATCHLIST.put(key, String(current + 1), { expirationTtl: windowSeconds * 2 });
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

async function handleAdmin(request, env) {
    if (request.method === 'GET') {
        return json({ ok: await verifySession(request, env) });
    }

    if (request.method === 'DELETE') {
        return json({ ok: true }, 200, { 'Set-Cookie': clearSessionCookie() });
    }

    if (request.method !== 'POST') return json({ error: 'method_not_allowed' }, 405);

    const tooLarge = rejectLargeBody(request, MAX_LOGIN_BODY);
    if (tooLarge) return tooLarge;

    const limited = await rateLimit(request, env, 'admin-login', 8, 300);
    if (limited) return limited;

    const body = await request.json().catch(() => ({}));
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
            requireStorage(env);
            const items = await env.WATCHLIST.get(KV_KEY, { type: 'json' });
            return json({ items: Array.isArray(items) ? items : [] });
        } catch (error) {
            return json({ error: error.message || 'storage_error', items: [] }, 500);
        }
    }

    if (request.method === 'PUT') {
        const tooLarge = rejectLargeBody(request, MAX_WATCHLIST_BODY);
        if (tooLarge) return tooLarge;

        const limited = await rateLimit(request, env, 'watchlist-write', 30, 60);
        if (limited) return limited;

        const authError = await requireAdmin(request, env);
        if (authError) return authError;

        try {
            requireStorage(env);
            const body = await request.json().catch(() => ({}));
            const rawItems = Array.isArray(body.items) ? body.items : [];
            const items = rawItems.map(normalizeItem).filter(Boolean).slice(0, 500);
            await env.WATCHLIST.put(KV_KEY, JSON.stringify(items));
            return json({ ok: true, items });
        } catch (error) {
            return json({ error: error.message || 'save_failed' }, 500);
        }
    }

    return json({ error: 'method_not_allowed' }, 405);
}

async function handleGamelist(request, env) {
    if (request.method === 'GET') {
        try {
            requireStorage(env);
            const items = await env.WATCHLIST.get(GAME_KV_KEY, { type: 'json' });
            return json({ items: Array.isArray(items) ? items : [] });
        } catch (error) {
            return json({ error: error.message || 'storage_error', items: [] }, 500);
        }
    }

    if (request.method === 'PUT') {
        const tooLarge = rejectLargeBody(request, MAX_GAMELIST_BODY);
        if (tooLarge) return tooLarge;

        const limited = await rateLimit(request, env, 'gamelist-write', 30, 60);
        if (limited) return limited;

        const authError = await requireAdmin(request, env);
        if (authError) return authError;

        try {
            requireStorage(env);
            const body = await request.json().catch(() => ({}));
            const rawItems = Array.isArray(body.items) ? body.items : [];
            const items = rawItems.map(normalizeGameItem).filter(Boolean).slice(0, 500);
            await env.WATCHLIST.put(GAME_KV_KEY, JSON.stringify(items));
            return json({ ok: true, items });
        } catch (error) {
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

    const limited = await rateLimit(request, env, 'game-search', 45, 60);
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

    const limited = await rateLimit(request, env, 'search', 60, 60);
    if (limited) return limited;

    try {
        if (type === 'anime') return await searchShikimori(query);
        if (type === 'movie' || type === 'series') return await searchTmdb(query, type, env);
        return json({ error: 'invalid_type' }, 400);
    } catch (error) {
        return json({ error: error.message || 'search_failed' }, 500);
    }
}

async function getTwitchToken(env, clientId) {
    const staticToken = await getSecret(env, 'TWITCH_ACCESS_TOKEN');
    if (staticToken) return staticToken.replace(/^Bearer\s+/i, '');

    if (env.WATCHLIST && typeof env.WATCHLIST.get === 'function') {
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
    if (!res.ok) throw new Error('twitch_unavailable');
    return data;
}

function discordDefaultAvatarUrl(id, discriminator) {
    let index = Number(discriminator) % 5;

    try {
        if (!discriminator || discriminator === '0') {
            index = Number((BigInt(id) >> 22n) % 6n);
        }
    } catch {
        index = 0;
    }

    return `${DISCORD_CDN_BASE}/embed/avatars/${index}.png`;
}

function discordAvatarUrl(user) {
    const id = text(user?.id, 32);
    const avatar = text(user?.avatar, 120);
    if (!id) return null;

    if (!avatar) return discordDefaultAvatarUrl(id, user?.discriminator);
    const extension = avatar.startsWith('a_') ? 'gif' : 'webp';
    return `${DISCORD_CDN_BASE}/avatars/${id}/${avatar}.${extension}?size=160`;
}

function discordDecorationUrl(data) {
    const asset = text(data?.asset, 160);
    return asset ? `${DISCORD_CDN_BASE}/avatar-decoration-presets/${asset}.png?size=160` : null;
}

function discordGuildBadgeUrl(guildId, badgeHash) {
    const id = text(guildId, 32);
    const hash = text(badgeHash, 160);
    return id && hash ? `${DISCORD_CDN_BASE}/guild-tag-badges/${id}/${hash}.png?size=48` : null;
}

function discordActivityAssetUrl(activity, asset) {
    const image = text(asset, 600);
    if (!image) return null;
    if (image.startsWith('http://') || image.startsWith('https://')) return cleanUrl(image);
    if (image.startsWith('spotify:')) return cleanUrl(`https://i.scdn.co/image/${image.slice('spotify:'.length)}`);
    if (image.startsWith('mp:external/')) return cleanUrl(`https://media.discordapp.net/external/${image.slice('mp:external/'.length)}`);
    if (image.startsWith('attachments/')) return cleanUrl(`${DISCORD_CDN_BASE}/${image}`);

    const applicationId = text(activity?.application_id, 32);
    if (applicationId && DISCORD_SNOWFLAKE_PATTERN.test(applicationId)) {
        return cleanUrl(`${DISCORD_CDN_BASE}/app-assets/${applicationId}/${image}.png`);
    }

    return null;
}

function normalizeDiscordActivity(activity) {
    const timestamps = activity?.timestamps || {};
    return {
        id: text(activity?.id, 80),
        name: text(activity?.name, 120),
        type: Number.isFinite(Number(activity?.type)) ? Number(activity.type) : 0,
        state: text(activity?.state, 180),
        details: text(activity?.details, 180),
        url: cleanUrl(activity?.url),
        applicationId: text(activity?.application_id, 32) || null,
        createdAt: Number(activity?.created_at) || null,
        startedAt: Number(timestamps.start) || null,
        endsAt: Number(timestamps.end) || null,
        largeImage: discordActivityAssetUrl(activity, activity?.assets?.large_image),
        largeText: text(activity?.assets?.large_text, 120),
        smallImage: discordActivityAssetUrl(activity, activity?.assets?.small_image),
        smallText: text(activity?.assets?.small_text, 120),
        emoji: activity?.emoji ? {
            id: text(activity.emoji.id, 32) || null,
            name: text(activity.emoji.name, 80),
            animated: Boolean(activity.emoji.animated),
        } : null,
    };
}

function discordBadges(user) {
    const flags = Number(user?.public_flags) || 0;
    const badges = DISCORD_BADGE_FLAGS
        .filter(({ bit }) => (flags & bit) === bit)
        .map(({ id, label, icon }) => ({ id, label, icon }));

    const primaryGuild = user?.primary_guild || user?.clan || null;
    if (primaryGuild?.tag) {
        badges.unshift({
            id: 'primary_guild',
            label: `Server Tag: ${text(primaryGuild.tag, 8)}`,
            icon: text(primaryGuild.tag, 8),
            iconUrl: discordGuildBadgeUrl(primaryGuild.identity_guild_id || primaryGuild.guild_id, primaryGuild.badge),
        });
    }

    return badges;
}

function normalizedDiscordProfile({ id, user, presence = {}, activities = [], source = 'discord', presenceUnavailable = false }) {
    return {
        id,
        source,
        displayName: text(user.display_name || user.global_name || user.username || 'Discord User', 80),
        username: text(user.username, 80),
        globalName: text(user.global_name, 80) || null,
        discriminator: text(user.discriminator, 8) || '0',
        avatarUrl: cleanUrl(discordAvatarUrl(user)),
        avatarDecorationUrl: cleanUrl(discordDecorationUrl(user.avatar_decoration_data)),
        profileUrl: `https://discord.com/users/${id}`,
        status: text(presence.discord_status, 24) || 'offline',
        badges: discordBadges(user),
        activities: activities.map(normalizeDiscordActivity).filter(activity => activity.name).slice(0, 6),
        spotify: presence.spotify || null,
        listeningToSpotify: Boolean(presence.listening_to_spotify),
        presenceUnavailable,
        activeOn: {
            web: Boolean(presence.active_on_discord_web),
            desktop: Boolean(presence.active_on_discord_desktop),
            mobile: Boolean(presence.active_on_discord_mobile),
        },
    };
}

async function fetchDiscordInviteProfile(inviteCode, expectedId) {
    const code = text(inviteCode, 80);
    if (!DISCORD_INVITE_PATTERN.test(code)) return null;

    const res = await fetch(`https://discord.com/api/invites/${code}?with_counts=true`, {
        headers: {
            'Accept': 'application/json',
            'User-Agent': 'krafen-site/1.0',
        },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.inviter?.id) return null;

    const inviterId = text(data.inviter.id, 32);
    if (expectedId && expectedId !== inviterId) return null;

    return normalizedDiscordProfile({
        id: inviterId,
        user: data.inviter,
        source: 'discord_invite',
        presenceUnavailable: true,
    });
}

async function handleDiscordProfile(request, env) {
    const url = new URL(request.url);
    const id = text(url.searchParams.get('id') || await getSecret(env, 'DISCORD_USER_ID'), 32);
    const inviteCode = text(url.searchParams.get('invite') || await getSecret(env, 'DISCORD_INVITE_CODE'), 80);
    if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
    if (!DISCORD_SNOWFLAKE_PATTERN.test(id)) return json({ error: 'discord_user_id_missing' }, 400);

    const limited = await rateLimit(request, env, 'discord-profile', 60, 60);
    if (limited) return limited;

    try {
        const res = await fetch(`${LANYARD_API_BASE}/users/${id}`, {
            headers: {
                'Accept': 'application/json',
                'User-Agent': 'krafen-site/1.0',
            },
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.success) throw new Error('discord_profile_unavailable');

        const presence = payload.data || {};
        const user = presence.discord_user || {};
        const activities = Array.isArray(presence.activities) ? presence.activities : [];

        return json(normalizedDiscordProfile({
            id,
            user,
            presence,
            activities,
            source: 'lanyard',
        }));
    } catch (error) {
        const fallback = await fetchDiscordInviteProfile(inviteCode, id).catch(() => null);
        if (fallback) return json(fallback);
        return json({ error: error.message || 'discord_profile_unavailable' }, 502);
    }
}

async function handleTwitch(request, env) {
    const url = new URL(request.url);
    const login = text(url.searchParams.get('login') || 'krafen', 40).toLowerCase();
    if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
    if (!/^[a-z0-9_]{3,25}$/.test(login)) return json({ error: 'invalid_login' }, 400);

    const limited = await rateLimit(request, env, 'twitch', 60, 60);
    if (limited) return limited;

    const clientId = await getSecret(env, 'TWITCH_CLIENT_ID');
    const accessToken = await getTwitchToken(env, clientId);
    if (!clientId || !accessToken) return json({ error: 'twitch_key_missing' }, 500);

    try {
        const [users, streams] = await Promise.all([
            twitchGet('/users', { login }, clientId, accessToken),
            twitchGet('/streams', { user_login: login, first: '1' }, clientId, accessToken),
        ]);

        const user = users.data?.[0] || {};
        const stream = streams.data?.[0] || null;

        return json({
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
        });
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
        if (url.pathname === '/api/twitch') return handleTwitch(request, env);
        if (url.pathname === '/api/discord-profile') return handleDiscordProfile(request, env);

        if (env.ASSETS) return withSecurityHeaders(await env.ASSETS.fetch(request));
        return withSecurityHeaders(new Response('Not found', { status: 404 }));
    },
};
