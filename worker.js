const KV_KEY = 'items';
const SHIKIMORI_BASE = 'https://shikimori.one';
const OMDB_BASE = 'https://www.omdbapi.com/';
const VALID_TYPES = new Set(['anime', 'movie', 'series']);
const VALID_STATUSES = new Set(['watched', 'planned', 'dropped']);

function json(body, status = 200) {
    return new Response(JSON.stringify(body), {
        status,
        headers: {
            'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store',
        },
    });
}

function text(value, max = 200) {
    return String(value || '').trim().slice(0, max);
}

function yearFrom(value) {
    const match = String(value || '').match(/\d{4}/);
    return match ? match[0] : '-';
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

async function requireAdmin(request, env) {
    const password = request.headers.get('X-Admin-Password') || '';
    const adminPassword = await getSecret(env, 'ADMIN_PASSWORD');
    if (!adminPassword) return json({ error: 'admin_password_missing' }, 500);
    if (password !== adminPassword) return json({ error: 'unauthorized' }, 401);
    return null;
}

function normalizeItem(item) {
    const id = text(item.id, 120);
    const title = text(item.title, 180);
    const type = text(item.type, 20);
    const status = text(item.status, 20);

    if (!id || !title || !VALID_TYPES.has(type) || !VALID_STATUSES.has(status)) {
        return null;
    }

    return {
        id,
        title,
        titleRu: text(item.titleRu, 180) || null,
        year: text(item.year, 24) || '-',
        type,
        status,
        poster: text(item.poster, 600) || null,
        addedAt: Number.isFinite(Number(item.addedAt)) ? Number(item.addedAt) : Date.now(),
        updatedAt: Number.isFinite(Number(item.updatedAt)) ? Number(item.updatedAt) : Date.now(),
    };
}

async function handleAdmin(request, env) {
    if (request.method !== 'POST') return json({ error: 'method_not_allowed' }, 405);

    const body = await request.json().catch(() => ({}));
    const adminPassword = await getSecret(env, 'ADMIN_PASSWORD');
    if (!adminPassword) return json({ error: 'admin_password_missing' }, 500);

    if (!body.password || body.password !== adminPassword) {
        return json({ error: 'invalid_password' }, 401);
    }

    return json({ ok: true });
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
        poster: item.Poster && item.Poster !== 'N/A' ? text(item.Poster, 600) : null,
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
        poster: item.image?.original ? `${SHIKIMORI_BASE}${item.image.original}` : null,
    })).filter(item => item.id && item.title);

    return json({ results });
}

async function handleSearch(request, env) {
    const url = new URL(request.url);
    const query = text(url.searchParams.get('q'), 80);
    const type = text(url.searchParams.get('type'), 20);

    if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
    if (query.length < 2) return json({ results: [] });

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

        if (env.ASSETS) return env.ASSETS.fetch(request);
        return new Response('Not found', { status: 404 });
    },
};
