const KV_KEY = 'items';
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

function requireStorage(env) {
    if (!env.WATCHLIST || typeof env.WATCHLIST.get !== 'function') {
        throw new Error('watchlist_kv_missing');
    }
}

function requireAdmin(request, env) {
    const password = request.headers.get('X-Admin-Password') || '';
    if (!env.ADMIN_PASSWORD) {
        return json({ error: 'admin_password_missing' }, 500);
    }
    if (password !== env.ADMIN_PASSWORD) {
        return json({ error: 'unauthorized' }, 401);
    }
    return null;
}

function cleanText(value, max = 180) {
    return String(value || '').trim().slice(0, max);
}

function normalizeItem(item) {
    const id = cleanText(item.id, 120);
    const title = cleanText(item.title, 180);
    const type = cleanText(item.type, 20);
    const status = cleanText(item.status, 20);

    if (!id || !title || !VALID_TYPES.has(type) || !VALID_STATUSES.has(status)) {
        return null;
    }

    return {
        id,
        title,
        titleRu: cleanText(item.titleRu, 180) || null,
        year: cleanText(item.year, 24) || '-',
        type,
        status,
        poster: cleanText(item.poster, 600) || null,
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
    const authError = requireAdmin(request, env);
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

export function onRequestOptions() {
    return json({ ok: true });
}
