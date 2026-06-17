const KV_KEY = 'youtube_stream';
const SESSION_COOKIE = 'kf_admin_session';
const MAX_STREAM_BODY = 120_000;
const YOUTUBE_ID_PATTERN = /^[A-Za-z0-9_-]{11}$/;

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

function requireStorage(env) {
    if (!env.WATCHLIST || typeof env.WATCHLIST.get !== 'function') {
        throw new Error('watchlist_kv_missing');
    }
}

function text(value, max = 200) {
    return String(value || '').trim().slice(0, max);
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

function normalizeStreamItem(item) {
    const videoId = text(item.videoId, 20);
    if (!YOUTUBE_ID_PATTERN.test(videoId)) return null;

    const durationSeconds = Math.round(Math.max(10, Math.min(21_600, Number(item.durationSeconds) || 600)));

    return {
        id: text(item.id, 80) || `${videoId}_${Date.now()}`,
        videoId,
        title: text(item.title, 160) || `youtube:${videoId}`,
        durationSeconds,
        addedAt: Number.isFinite(Number(item.addedAt)) ? Number(item.addedAt) : Date.now(),
    };
}

function normalizeStreamState(body) {
    const queue = (Array.isArray(body.queue) ? body.queue : [])
        .map(normalizeStreamItem)
        .filter(Boolean)
        .slice(0, 120);

    const startedAt = Number.isFinite(Number(body.startedAt)) ? Number(body.startedAt) : Date.now();
    const parsedPausedAt = Number(body.pausedAt);
    const pausedAt = body.pausedAt === null || body.pausedAt === undefined || body.pausedAt === ''
        ? null
        : Number.isFinite(parsedPausedAt) && parsedPausedAt > 0
            ? parsedPausedAt
            : null;

    return {
        queue,
        startedAt,
        pausedAt,
        updatedAt: Date.now(),
    };
}

function emptyStreamState() {
    return {
        queue: [],
        startedAt: Date.now(),
        pausedAt: null,
        updatedAt: Date.now(),
    };
}

export async function onRequestGet({ env }) {
    try {
        requireStorage(env);
        const state = await env.WATCHLIST.get(KV_KEY, { type: 'json' });
        return json({ ...(state || emptyStreamState()), serverTime: Date.now() });
    } catch (error) {
        return json({ error: error.message || 'storage_error', ...emptyStreamState(), serverTime: Date.now() }, 500);
    }
}

export async function onRequestPut({ request, env }) {
    const tooLarge = rejectLargeBody(request, MAX_STREAM_BODY);
    if (tooLarge) return tooLarge;

    const limited = await rateLimit(request, env, 'stream-write', 45, 60);
    if (limited) return limited;

    const authError = await requireAdmin(request, env);
    if (authError) return authError;

    try {
        requireStorage(env);
        const body = await request.json().catch(() => ({}));
        const state = normalizeStreamState(body);
        await env.WATCHLIST.put(KV_KEY, JSON.stringify(state));
        return json({ ok: true, ...state, serverTime: Date.now() });
    } catch (error) {
        return json({ error: error.message || 'save_failed' }, 500);
    }
}

export function onRequestOptions() {
    return json({ ok: true });
}
