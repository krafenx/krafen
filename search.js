const IGDB_BASE = 'https://api.igdb.com/v4';
const TWITCH_TOKEN_URL = 'https://id.twitch.tv/oauth2/token';
const IGDB_TOKEN_KEY = 'igdb:token';

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

function yearFromUnix(value) {
    const ts = Number(value);
    if (!Number.isFinite(ts) || ts <= 0) return '-';
    return String(new Date(ts * 1000).getUTCFullYear());
}

function escapeIgdbString(value) {
    return text(value, 80).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

function igdbCover(imageId) {
    const id = text(imageId, 80);
    return id ? `https://images.igdb.com/igdb/image/upload/t_cover_big/${id}.jpg` : null;
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

function normalizeGame(item) {
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

    return json({ results: data.map(normalizeGame).filter(item => item.id && item.title) });
}

export async function onRequestGet({ request, env }) {
    const url = new URL(request.url);
    const query = text(url.searchParams.get('q'), 80);
    if (query.length < 2) return json({ results: [] });

    const limited = await rateLimit(request, env, 'game-search', 45, 60);
    if (limited) return limited;

    try {
        return await searchIgdb(query, env);
    } catch (error) {
        return json({ error: error.message || 'game_search_failed' }, 500);
    }
}

export function onRequestOptions() {
    return json({ ok: true });
}
