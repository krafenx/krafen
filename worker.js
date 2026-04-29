const KV_KEY = 'items';
const SHIKIMORI_BASE = 'https://shikimori.one';
const OMDB_BASE = 'https://www.omdbapi.com/';
const SESSION_COOKIE = 'kf_admin_session';
const SESSION_TTL_SECONDS = 60 * 60 * 8;
const MAX_LOGIN_BODY = 2_048;
const MAX_WATCHLIST_BODY = 100_000;
const VALID_TYPES = new Set(['anime', 'movie', 'series']);
const VALID_STATUSES = new Set(['watched', 'planned', 'dropped']);
const ITEM_ID_PATTERN = /^[A-Za-z0-9:_-]{1,120}$/;

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
        "connect-src 'self' https://api.github.com https://ws.audioscrobbler.com https://discord.com https://shikimori.one https://www.omdbapi.com",
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

async function searchOmdb(query, type, env) {
    const omdbApiKey = await getSecret(env, 'OMDB_API_KEY');
    if (!omdbApiKey) return json({ error: 'omdb_key_missing' }, 500);

    const url = new URL(OMDB_BASE);
    url.searchParams.set('apikey', omdbApiKey);
    url.searchParams.set('s', query);
    url.searchParams.set('type', type);

    const res = await fetch(url);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return json({ error: 'omdb_unavailable' }, 502);
    if (data.Response === 'False') return json({ results: [] });

    const results = (data.Search || []).slice(0, 10).map(item => ({
        id: text(item.imdbID, 80),
        title: text(item.Title),
        titleRu: null,
        year: yearFrom(item.Year),
        type: item.Type === 'series' ? 'series' : 'movie',
        poster: cleanUrl(item.Poster),
    })).filter(item => item.id && item.title);

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
        if (type === 'movie' || type === 'series') return await searchOmdb(query, type, env);
        return json({ error: 'invalid_type' }, 400);
    } catch (error) {
        return json({ error: error.message || 'search_failed' }, 500);
    }
}

export default {
    async fetch(request, env) {
        const url = new URL(request.url);

        if (request.method === 'OPTIONS') return json({ ok: true });
        if (url.pathname === '/api/admin') return handleAdmin(request, env);
        if (url.pathname === '/api/watchlist') return handleWatchlist(request, env);
        if (url.pathname === '/api/search') return handleSearch(request, env);

        if (env.ASSETS) return withSecurityHeaders(await env.ASSETS.fetch(request));
        return withSecurityHeaders(new Response('Not found', { status: 404 }));
    },
};
