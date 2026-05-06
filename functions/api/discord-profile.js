const LANYARD_API_BASE = 'https://api.lanyard.rest/v1';
const DISCORD_CDN_BASE = 'https://cdn.discordapp.com';
const DISCORD_SNOWFLAKE_PATTERN = /^\d{17,22}$/;
const DISCORD_INVITE_PATTERN = /^[A-Za-z0-9-]{2,64}$/;
const DISCORD_BADGE_FLAGS = [
    { bit: 1 << 0, id: 'staff', label: 'Discord Staff', icon: 'DS' },
    { bit: 1 << 1, id: 'partner', label: 'Partnered Server Owner', icon: 'PA' },
    { bit: 1 << 2, id: 'hypesquad', label: 'HypeSquad Events', icon: 'HS' },
    { bit: 1 << 3, id: 'bug_hunter_1', label: 'Bug Hunter', icon: 'BH' },
    { bit: 1 << 6, id: 'bravery', label: 'House Bravery', icon: 'HB' },
    { bit: 1 << 7, id: 'brilliance', label: 'House Brilliance', icon: 'HI' },
    { bit: 1 << 8, id: 'balance', label: 'House Balance', icon: 'HA' },
    { bit: 1 << 9, id: 'early_supporter', label: 'Early Nitro Supporter', icon: 'EN' },
    { bit: 1 << 14, id: 'bug_hunter_2', label: 'Bug Hunter Gold', icon: 'BG' },
    { bit: 1 << 16, id: 'verified_bot', label: 'Verified Bot', icon: 'VB' },
    { bit: 1 << 17, id: 'verified_developer', label: 'Early Verified Bot Developer', icon: 'VD' },
    { bit: 1 << 18, id: 'certified_moderator', label: 'Moderator Programs Alumni', icon: 'CM' },
    { bit: 1 << 19, id: 'http_interactions', label: 'HTTP Interactions Bot', icon: 'BI' },
];

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

function discordDefaultAvatarUrl(id, discriminator) {
    let index = Number(discriminator) % 5;

    try {
        if (!discriminator || discriminator === '0') {
            index = Number((BigInt(id) >> 22n) % 6n);
        }
    } catch {
        index = 0;
    }

    return `${DISCORD_CDN_BASE}/embed/avatars/${index}.png`;
}

function discordAvatarUrl(user) {
    const id = text(user?.id, 32);
    const avatar = text(user?.avatar, 120);
    if (!id) return null;
    if (!avatar) return discordDefaultAvatarUrl(id, user?.discriminator);

    const extension = avatar.startsWith('a_') ? 'gif' : 'webp';
    return `${DISCORD_CDN_BASE}/avatars/${id}/${avatar}.${extension}?size=160`;
}

function discordDecorationUrl(data) {
    const asset = text(data?.asset, 160);
    return asset ? `${DISCORD_CDN_BASE}/avatar-decoration-presets/${asset}.png?size=160` : null;
}

function discordGuildBadgeUrl(guildId, badgeHash) {
    const id = text(guildId, 32);
    const hash = text(badgeHash, 160);
    return id && hash ? `${DISCORD_CDN_BASE}/guild-tag-badges/${id}/${hash}.png?size=48` : null;
}

function discordActivityAssetUrl(activity, asset) {
    const image = text(asset, 600);
    if (!image) return null;
    if (image.startsWith('http://') || image.startsWith('https://')) return cleanUrl(image);
    if (image.startsWith('spotify:')) return cleanUrl(`https://i.scdn.co/image/${image.slice('spotify:'.length)}`);
    if (image.startsWith('mp:external/')) return cleanUrl(`https://media.discordapp.net/external/${image.slice('mp:external/'.length)}`);
    if (image.startsWith('attachments/')) return cleanUrl(`${DISCORD_CDN_BASE}/${image}`);

    const applicationId = text(activity?.application_id, 32);
    if (applicationId && DISCORD_SNOWFLAKE_PATTERN.test(applicationId)) {
        return cleanUrl(`${DISCORD_CDN_BASE}/app-assets/${applicationId}/${image}.png`);
    }

    return null;
}

function normalizeDiscordActivity(activity) {
    const timestamps = activity?.timestamps || {};
    return {
        id: text(activity?.id, 80),
        name: text(activity?.name, 120),
        type: Number.isFinite(Number(activity?.type)) ? Number(activity.type) : 0,
        state: text(activity?.state, 180),
        details: text(activity?.details, 180),
        url: cleanUrl(activity?.url),
        applicationId: text(activity?.application_id, 32) || null,
        createdAt: Number(activity?.created_at) || null,
        startedAt: Number(timestamps.start) || null,
        endsAt: Number(timestamps.end) || null,
        largeImage: discordActivityAssetUrl(activity, activity?.assets?.large_image),
        largeText: text(activity?.assets?.large_text, 120),
        smallImage: discordActivityAssetUrl(activity, activity?.assets?.small_image),
        smallText: text(activity?.assets?.small_text, 120),
        emoji: activity?.emoji ? {
            id: text(activity.emoji.id, 32) || null,
            name: text(activity.emoji.name, 80),
            animated: Boolean(activity.emoji.animated),
        } : null,
    };
}

function discordBadges(user) {
    const flags = Number(user?.public_flags) || 0;
    const badges = DISCORD_BADGE_FLAGS
        .filter(({ bit }) => (flags & bit) === bit)
        .map(({ id, label, icon }) => ({ id, label, icon }));

    const primaryGuild = user?.primary_guild || user?.clan || null;
    if (primaryGuild?.tag) {
        badges.unshift({
            id: 'primary_guild',
            label: `Server Tag: ${text(primaryGuild.tag, 8)}`,
            icon: text(primaryGuild.tag, 8),
            iconUrl: discordGuildBadgeUrl(primaryGuild.identity_guild_id || primaryGuild.guild_id, primaryGuild.badge),
        });
    }

    return badges;
}

function normalizedDiscordProfile({ id, user, presence = {}, activities = [], source = 'discord', presenceUnavailable = false }) {
    return {
        id,
        source,
        displayName: text(user.display_name || user.global_name || user.username || 'Discord User', 80),
        username: text(user.username, 80),
        globalName: text(user.global_name, 80) || null,
        discriminator: text(user.discriminator, 8) || '0',
        avatarUrl: cleanUrl(discordAvatarUrl(user)),
        avatarDecorationUrl: cleanUrl(discordDecorationUrl(user.avatar_decoration_data)),
        profileUrl: `https://discord.com/users/${id}`,
        status: text(presence.discord_status, 24) || 'offline',
        badges: discordBadges(user),
        activities: activities.map(normalizeDiscordActivity).filter(activity => activity.name).slice(0, 6),
        spotify: presence.spotify || null,
        listeningToSpotify: Boolean(presence.listening_to_spotify),
        presenceUnavailable,
        activeOn: {
            web: Boolean(presence.active_on_discord_web),
            desktop: Boolean(presence.active_on_discord_desktop),
            mobile: Boolean(presence.active_on_discord_mobile),
        },
    };
}

async function fetchDiscordInviteProfile(inviteCode, expectedId) {
    const code = text(inviteCode, 80);
    if (!DISCORD_INVITE_PATTERN.test(code)) return null;

    const res = await fetch(`https://discord.com/api/invites/${code}?with_counts=true`, {
        headers: {
            'Accept': 'application/json',
            'User-Agent': 'krafen-site/1.0',
        },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.inviter?.id) return null;

    const inviterId = text(data.inviter.id, 32);
    if (expectedId && expectedId !== inviterId) return null;

    return normalizedDiscordProfile({
        id: inviterId,
        user: data.inviter,
        source: 'discord_invite',
        presenceUnavailable: true,
    });
}

export async function onRequestGet({ request, env }) {
    const url = new URL(request.url);
    const id = text(url.searchParams.get('id') || await getSecret(env, 'DISCORD_USER_ID'), 32);
    const inviteCode = text(url.searchParams.get('invite') || await getSecret(env, 'DISCORD_INVITE_CODE'), 80);
    if (!DISCORD_SNOWFLAKE_PATTERN.test(id)) return json({ error: 'discord_user_id_missing' }, 400);

    const limited = await rateLimit(request, env, 'discord-profile', 60, 60);
    if (limited) return limited;

    try {
        const res = await fetch(`${LANYARD_API_BASE}/users/${id}`, {
            headers: {
                'Accept': 'application/json',
                'User-Agent': 'krafen-site/1.0',
            },
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.success) throw new Error('discord_profile_unavailable');

        const presence = payload.data || {};
        const user = presence.discord_user || {};
        const activities = Array.isArray(presence.activities) ? presence.activities : [];

        return json(normalizedDiscordProfile({
            id,
            user,
            presence,
            activities,
            source: 'lanyard',
        }));
    } catch (error) {
        const fallback = await fetchDiscordInviteProfile(inviteCode, id).catch(() => null);
        if (fallback) return json(fallback);
        return json({ error: error.message || 'discord_profile_unavailable' }, 502);
    }
}

export function onRequestOptions() {
    return json({ ok: true });
}
