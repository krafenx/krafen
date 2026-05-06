const KV_KEY = 'games';
const SESSION_COOKIE = 'kf_admin_session';
const MAX_GAMELIST_BODY = 1_500_000;
const VALID_STATUSES = new Set(['completed', 'playing', 'dropped', 'wishlist', 'paused']);
const ITEM_ID_PATTERN = /^[A-Za-z0-9:_-]{1,120}$/;

const SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=()',
    'Cross-Origin-Opener-Policy': 'same-origin',
};

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

async function getSecret(env, name) {
    const value = env[name];
    if (!value) return '';
    if (typeof value === 'string') return value;
    if (typeof value.get === 'function') return await value.get();
    return '';
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

function requireStorage(env) {
    if (!env.WATCHLIST || typeof env.WATCHLIST.get !== 'function') {
        throw new Error('watchlist_kv_missing');
    }
}

function rejectLargeBody(request, maxBytes) {
    const length = Number(request.headers.get('Content-Length') || 0);
    if (Number.isFinite(length) && length > maxBytes) {
        return json({ error: 'payload_too_large' }, 413);
    }
    return null;
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

async function verifySession(request, env) {
    const token = parseCookies(request)[SESSION_COOKIE];
    const adminPassword = await getSecret(env, 'ADMIN_PASSWORD');
    if (!token || !adminPassword) return false;

    const [payload, signature] = token.split('.');
    if (!payload || !signature) return false;
    if (!constantTimeEqual(signature, await hmac(adminPassword, payload))) return false;

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

function cleanStringArray(value, maxItems, maxText) {
    return Array.isArray(value)
        ? value.map(item => text(item, maxText)).filter(Boolean).slice(0, maxItems)
        : [];
}

function normalizeGame(item) {
    const id = text(item.id, 120);
    const title = text(item.title, 180);
    const status = text(item.status, 20);
    const rating = Math.max(0, Math.min(10, Number(item.rating) || 0));

    if (!ITEM_ID_PATTERN.test(id) || !title || !VALID_STATUSES.has(status)) {
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

export async function onRequestGet({ env }) {
    try {
        requireStorage(env);
        const items = await env.WATCHLIST.get(KV_KEY, { type: 'json' });
        return json({ items: Array.isArray(items) ? items : [] });
    } catch (error) {
        return json({ error: error.message || 'storage_error', items: [] }, 500);
    }
}

export async function onRequestPut({ request, env }) {
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
        const items = rawItems.map(normalizeGame).filter(Boolean).slice(0, 500);
        await env.WATCHLIST.put(KV_KEY, JSON.stringify(items));
        return json({ ok: true, items });
    } catch (error) {
        return json({ error: error.message || 'save_failed' }, 500);
    }
}

export function onRequestOptions() {
    return json({ ok: true });
}
