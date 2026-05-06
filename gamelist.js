const SESSION_COOKIE = 'kf_admin_session';
const SESSION_TTL_SECONDS = 60 * 60 * 8;
const MAX_LOGIN_BODY = 2_048;

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

async function createSession(adminPassword) {
    const payload = base64UrlEncode(JSON.stringify({
        exp: Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS,
        nonce: crypto.randomUUID(),
    }));
    return `${payload}.${await hmac(adminPassword, payload)}`;
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

export async function onRequestGet({ request, env }) {
    return json({ ok: await verifySession(request, env) });
}

export async function onRequestPost({ request, env }) {
    const tooLarge = rejectLargeBody(request, MAX_LOGIN_BODY);
    if (tooLarge) return tooLarge;

    const limited = await rateLimit(request, env, 'admin-login', 8, 300);
    if (limited) return limited;

    const adminPassword = await getSecret(env, 'ADMIN_PASSWORD');
    if (!adminPassword) {
        return json({ error: 'admin_password_missing' }, 500);
    }

    const body = await request.json().catch(() => ({}));
    if (!body.password || !constantTimeEqual(String(body.password), adminPassword)) {
        return json({ error: 'invalid_password' }, 401);
    }

    return json({ ok: true }, 200, { 'Set-Cookie': sessionCookie(await createSession(adminPassword)) });
}

export function onRequestDelete() {
    return json({ ok: true }, 200, { 'Set-Cookie': clearSessionCookie() });
}

export function onRequestOptions() {
    return json({ ok: true });
}
