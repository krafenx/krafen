function json(body, status = 200) {
    return new Response(JSON.stringify(body), {
        status,
        headers: {
            'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store',
        },
    });
}

function isValidPassword(env, password) {
    return Boolean(env.ADMIN_PASSWORD && password && password === env.ADMIN_PASSWORD);
}

export async function onRequestPost({ request, env }) {
    if (!env.ADMIN_PASSWORD) {
        return json({ error: 'admin_password_missing' }, 500);
    }

    const body = await request.json().catch(() => ({}));
    if (!isValidPassword(env, body.password)) {
        return json({ error: 'invalid_password' }, 401);
    }

    return json({ ok: true });
}

export function onRequestOptions() {
    return json({ ok: true });
}
