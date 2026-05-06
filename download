const TWITCH_API_BASE = 'https://api.twitch.tv/helix';
const TWITCH_TOKEN_URL = 'https://id.twitch.tv/oauth2/token';
const TWITCH_TOKEN_KEY = 'twitch:token';

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

export async function onRequestGet({ request, env }) {
    const url = new URL(request.url);
    const login = text(url.searchParams.get('login') || 'krafen', 40).toLowerCase();
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

export function onRequestOptions() {
    return json({ ok: true });
}
