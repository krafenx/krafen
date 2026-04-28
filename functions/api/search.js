const SHIKIMORI_BASE = 'https://shikimori.one';
const OMDB_BASE = 'https://www.omdbapi.com/';

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

async function searchOmdb(query, type, env) {
    if (!env.OMDB_API_KEY) {
        return json({ error: 'omdb_key_missing' }, 500);
    }

    const url = new URL(OMDB_BASE);
    url.searchParams.set('apikey', env.OMDB_API_KEY);
    url.searchParams.set('s', query);
    url.searchParams.set('type', type);

    const res = await fetch(url);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        return json({ error: 'omdb_unavailable' }, 502);
    }
    if (data.Response === 'False') {
        return json({ results: [] });
    }

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

export async function onRequestGet({ request, env }) {
    const url = new URL(request.url);
    const query = text(url.searchParams.get('q'), 80);
    const type = text(url.searchParams.get('type'), 20);

    if (query.length < 2) {
        return json({ results: [] });
    }

    try {
        if (type === 'anime') return await searchShikimori(query);
        if (type === 'movie' || type === 'series') return await searchOmdb(query, type, env);
        return json({ error: 'invalid_type' }, 400);
    } catch (error) {
        return json({ error: error.message || 'search_failed' }, 500);
    }
}

export function onRequestOptions() {
    return json({ ok: true });
}
