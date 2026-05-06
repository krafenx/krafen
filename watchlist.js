const SHIKIMORI_BASE = 'https://shikimori.one';
const TMDB_BASE = 'https://api.themoviedb.org';
const TMDB_IMAGE_BASE = 'https://image.tmdb.org/t/p/w342';

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

function yearFrom(value) {
    const match = String(value || '').match(/\d{4}/);
    return match ? match[0] : '-';
}

function tmdbPoster(value) {
    const path = text(value, 120);
    return path && path.startsWith('/') ? `${TMDB_IMAGE_BASE}${path}` : null;
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

async function searchTmdb(query, type, env) {
    const tmdbAccessToken = await getSecret(env, 'TMDB_ACCESS_TOKEN');
    const tmdbApiKey = await getSecret(env, 'TMDB_API_KEY');
    if (!tmdbAccessToken && !tmdbApiKey) {
        return json({ error: 'tmdb_key_missing' }, 500);
    }

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
    if (!res.ok) {
        return json({ error: 'tmdb_unavailable' }, 502);
    }

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

export async function onRequestGet({ request, env }) {
    const url = new URL(request.url);
    const query = text(url.searchParams.get('q'), 80);
    const type = text(url.searchParams.get('type'), 20);

    if (query.length < 2) {
        return json({ results: [] });
    }

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

export function onRequestOptions() {
    return json({ ok: true });
}
