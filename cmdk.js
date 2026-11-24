/* ==========================================================================
   Krafen — скрытая палитра быстрого поиска (Ctrl+K / Cmd+K / /)
   Без навязчивых подсказок на экране; вызывается только с клавиатуры.
   ========================================================================== */
(() => {
    if (window.__kfCmdkLoaded) return;
    window.__kfCmdkLoaded = true;

    const CSS = `
        .kf-cmd-backdrop {
            position: fixed;
            inset: 0;
            z-index: 260;
            display: flex;
            align-items: flex-start;
            justify-content: center;
            padding: min(14vh, 110px) 16px 24px;
            background: rgba(3, 3, 5, .74);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            opacity: 0;
            pointer-events: none;
            transition: opacity .2s cubic-bezier(.16, 1, .3, 1);
        }
        .kf-cmd-backdrop.open {
            opacity: 1;
            pointer-events: auto;
        }
        .kf-cmd-box {
            width: min(620px, 100%);
            max-height: min(72vh, 560px);
            display: flex;
            flex-direction: column;
            border-radius: 20px;
            background: rgba(11, 11, 15, .94);
            border: 1px solid rgba(163, 19, 19, .36);
            box-shadow: 0 32px 90px rgba(0, 0, 0, .88), 0 0 42px rgba(124, 10, 10, .18), inset 0 1px 0 rgba(255, 255, 255, .05);
            overflow: hidden;
            transform: translate3d(0, -10px, 0) scale(.985);
            transition: transform .22s cubic-bezier(.16, 1, .3, 1);
        }
        .kf-cmd-backdrop.open .kf-cmd-box {
            transform: translate3d(0, 0, 0) scale(1);
        }
        .kf-cmd-head {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 16px 20px;
            border-bottom: 1px solid rgba(160, 160, 160, .14);
        }
        .kf-cmd-icon {
            width: 17px;
            height: 17px;
            flex-shrink: 0;
            stroke: #a31313;
            fill: none;
            stroke-width: 1.9;
            stroke-linecap: round;
            stroke-linejoin: round;
        }
        .kf-cmd-input {
            flex: 1;
            min-width: 0;
            background: transparent;
            border: none;
            outline: none;
            color: #fff;
            font-family: var(--font-sans, "Manrope", sans-serif);
            font-size: 15px;
            letter-spacing: .01em;
        }
        .kf-cmd-input::placeholder {
            color: rgba(160, 160, 160, .48);
        }
        .kf-cmd-esc {
            padding: 4px 9px;
            border-radius: 7px;
            border: 1px solid rgba(160, 160, 160, .2);
            background: rgba(255, 255, 255, .04);
            color: #a0a0a0;
            font-family: var(--font-mono, monospace);
            font-size: 10px;
            letter-spacing: .08em;
            cursor: pointer;
        }
        .kf-cmd-list {
            flex: 1;
            overflow-y: auto;
            overscroll-behavior: contain;
            padding: 10px;
        }
        .kf-cmd-group {
            padding: 8px 12px 6px;
            font-size: 9px;
            letter-spacing: .26em;
            text-transform: uppercase;
            color: #666;
        }
        .kf-cmd-item {
            width: 100%;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 14px;
            padding: 11px 14px;
            border-radius: 12px;
            border: 1px solid transparent;
            background: transparent;
            color: #e0e0e0;
            text-align: left;
            font-family: var(--font-sans, "Manrope", sans-serif);
            cursor: pointer;
            transition: background .14s ease, border-color .14s ease, color .14s ease;
        }
        .kf-cmd-item.active,
        .kf-cmd-item:hover {
            background: rgba(124, 10, 10, .2);
            border-color: rgba(163, 19, 19, .45);
            color: #fff;
        }
        .kf-cmd-main {
            min-width: 0;
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 3px;
        }
        .kf-cmd-title {
            font-size: 14px;
            font-weight: 500;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .kf-cmd-sub {
            font-size: 11px;
            color: #888;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .kf-cmd-item.active .kf-cmd-sub {
            color: #c7c7c7;
        }
        .kf-cmd-tag {
            flex-shrink: 0;
            padding: 3px 9px;
            border-radius: 999px;
            border: 1px solid rgba(160, 160, 160, .18);
            background: rgba(255, 255, 255, .03);
            font-family: var(--font-mono, monospace);
            font-size: 10px;
            color: #a0a0a0;
        }
        .kf-cmd-empty {
            padding: 34px 16px;
            text-align: center;
            color: #666;
            font-size: 13px;
        }
        .kf-cmd-foot {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 9px 18px;
            border-top: 1px solid rgba(160, 160, 160, .1);
            background: rgba(5, 5, 5, .55);
            font-size: 10px;
            color: #666;
            letter-spacing: .06em;
        }
        .kf-cmd-foot kbd {
            padding: 2px 6px;
            border-radius: 5px;
            border: 1px solid rgba(160, 160, 160, .2);
            background: rgba(255, 255, 255, .04);
            font-family: var(--font-mono, monospace);
            font-size: 10px;
            color: #a0a0a0;
        }
        @media (max-width: 620px) {
            .kf-cmd-backdrop { padding: 12px; }
            .kf-cmd-foot { display: none; }
        }
    `;

    const isDanonsha = location.pathname === '/danonsha' || location.pathname.startsWith('/danonsha/');
    const profile = isDanonsha ? 'danonsha' : 'krafen';
    const prefix = isDanonsha ? '/danonsha' : '';

    const BASE_PAGES = [
        { group: 'Разделы', title: 'Главная', sub: 'Музыка, статус Twitch, Discord и ссылки', url: '/', tag: 'krafen' },
        { group: 'Разделы', title: 'Watchlist', sub: 'Фильмы, сериалы и аниме с оценками и рецензиями', url: `${prefix}/watchlist`, tag: 'коллекция' },
        { group: 'Разделы', title: 'Gamelist', sub: 'Пройденные и активные игры с отзывами', url: `${prefix}/gamelist`, tag: 'коллекция' },
        { group: 'Разделы', title: 'Setup', sub: 'Комплектующие ПК и рабочая периферия', url: `${prefix}/setup`, tag: 'железо' },
        { group: 'Разделы', title: 'Wishlist', sub: 'Список желаемых покупок и бюджет', url: `${prefix}/wishlist`, tag: 'покупки' },
        { group: 'Разделы', title: 'Wrapped / Итоги', sub: 'Статистика коллекций и стримов по годам', url: `/wrapped${isDanonsha ? '?profile=danonsha' : ''}`, tag: 'recap' },
        { group: 'Разделы', title: 'danonsha', sub: 'Прямые эфиры и кинотеатр записей VOD', url: '/danonsha', tag: 'стримы' },
        { group: 'Разделы', title: 'Предложка', sub: 'Предложить фильм, сериал, аниме или игру на стрим', url: '/suggest', tag: 'идеи' },
        { group: 'Разделы', title: 'Панель управления', sub: 'Аналитика, кэш, резервные копии и логи', url: '/admin', tag: 'admin' },
        { group: 'Разделы', title: 'Задачи BP', sub: 'Калькулятор заданий и таймеры', url: '/tasks', tag: 'утилита' },
    ];

    const ACTIONS = [
        {
            group: 'Команды',
            title: 'Переключить атмосферу',
            sub: 'Включить или приглушить дождь и молнии',
            tag: 'эффект',
            run: () => document.getElementById('atmo')?.click(),
        },
        {
            group: 'Команды',
            title: 'Прокрутить наверх',
            sub: 'Вернуться в начало текущей страницы',
            tag: '↑',
            run: () => {
                window.scrollTo({ top: 0, behavior: 'smooth' });
            },
        },
    ];

    let styleInjected = false;
    let backdrop = null;
    let inputEl = null;
    let listEl = null;
    let results = [];
    let activeIdx = 0;
    let collectionItems = null;
    let loadingCollections = false;

    function injectStyle() {
        if (styleInjected) return;
        styleInjected = true;
        const st = document.createElement('style');
        st.id = 'kf-cmd-style';
        const nonceEl = document.querySelector('style[nonce], script[nonce]');
        if (nonceEl && nonceEl.nonce) st.nonce = nonceEl.nonce;
        st.textContent = CSS;
        document.head.appendChild(st);
    }

    function ensureDom() {
        if (backdrop) return;
        injectStyle();
        backdrop = document.createElement('div');
        backdrop.className = 'kf-cmd-backdrop';
        backdrop.setAttribute('data-scroll', '');
        backdrop.setAttribute('role', 'dialog');
        backdrop.setAttribute('aria-modal', 'true');
        backdrop.setAttribute('aria-label', 'Быстрый поиск по сайту');
        backdrop.innerHTML = `
            <div class="kf-cmd-box">
                <div class="kf-cmd-head">
                    <svg class="kf-cmd-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
                    <input id="kf-cmd-input" class="kf-cmd-input" type="search" placeholder="Поиск по страницам, фильмам, играм, сетапу…" autocomplete="off" spellcheck="false">
                    <button type="button" class="kf-cmd-esc" id="kf-cmd-close">ESC</button>
                </div>
                <div class="kf-cmd-list" id="kf-cmd-list" data-scroll></div>
                <div class="kf-cmd-foot">
                    <span><kbd>↑</kbd> <kbd>↓</kbd> выбор · <kbd>Enter</kbd> открыть</span>
                    <span><kbd>Ctrl+K</kbd> · <kbd>/</kbd></span>
                </div>
            </div>`;
        document.body.appendChild(backdrop);

        inputEl = backdrop.querySelector('#kf-cmd-input');
        listEl = backdrop.querySelector('#kf-cmd-list');

        backdrop.addEventListener('click', (e) => {
            if (e.target === backdrop) close();
        });
        backdrop.querySelector('#kf-cmd-close').addEventListener('click', close);
        backdrop.addEventListener('wheel', (e) => e.stopPropagation(), { passive: true });

        inputEl.addEventListener('input', () => {
            activeIdx = 0;
            renderResults();
        });

        inputEl.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (results.length) {
                    activeIdx = (activeIdx + 1) % results.length;
                    syncActive();
                }
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (results.length) {
                    activeIdx = (activeIdx - 1 + results.length) % results.length;
                    syncActive();
                }
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (results[activeIdx]) activateItem(results[activeIdx]);
            }
        });

        listEl.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-cmd-idx]');
            if (!btn) return;
            const item = results[Number(btn.dataset.cmdIdx)];
            if (item) activateItem(item);
        });
    }

    async function loadCollectionsOnce() {
        if (collectionItems !== null || loadingCollections) return;
        loadingCollections = true;
        const specs = [
            { name: 'watchlist', label: 'Watchlist', path: `${prefix}/watchlist` },
            { name: 'gamelist', label: 'Gamelist', path: `${prefix}/gamelist` },
            { name: 'setup', label: 'Setup', path: `${prefix}/setup` },
            { name: 'wishlist', label: 'Wishlist', path: `${prefix}/wishlist` },
        ];
        const loaded = [];
        await Promise.all(specs.map(async (sp) => {
            try {
                const res = await fetch(`/api/${sp.name}`, {
                    headers: { Accept: 'application/json', 'X-Krafen-Profile': profile },
                    credentials: 'same-origin',
                });
                if (!res.ok) return;
                const data = await res.json();
                const items = Array.isArray(data.items) ? data.items : [];
                for (const it of items) {
                    const title = String(it.titleRu || it.title || it.name || '').trim();
                    if (!title) continue;
                    const altTitle = it.titleRu && it.title && it.title !== it.titleRu ? ` (${it.title})` : '';
                    const tags = Array.isArray(it.tags) ? it.tags.join(' ') : '';
                    const subParts = [];
                    if (it.category) subParts.push(it.category);
                    if (it.year && it.year !== '-') subParts.push(it.year);
                    if (Number(it.rating) > 0) subParts.push(`★ ${it.rating}`);
                    if (it.note) subParts.push(String(it.note).slice(0, 60));
                    const searchBlob = `${title} ${it.title || ''} ${tags} ${it.category || ''} ${it.note || ''} ${it.review || ''}`.toLowerCase();
                    const dest = sp.name === 'setup'
                        ? sp.path
                        : `${sp.path}?q=${encodeURIComponent(title)}`;
                    loaded.push({
                        group: sp.label,
                        title: title + altTitle,
                        sub: subParts.join(' · ') || sp.label,
                        url: dest,
                        tag: sp.name,
                        searchBlob,
                    });
                }
            } catch {}
        }));
        collectionItems = loaded;
        loadingCollections = false;
        if (backdrop && backdrop.classList.contains('open')) renderResults();
    }

    function esc(s) {
        return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function renderResults() {
        const q = (inputEl ? inputEl.value : '').trim().toLowerCase();
        const terms = q ? q.split(/\s+/).filter(Boolean) : [];
        const matchFn = (hay) => terms.every((t) => hay.includes(t));

        const matchedPages = BASE_PAGES.filter((p) => {
            if (!terms.length) return true;
            return matchFn(`${p.title} ${p.sub} ${p.tag} ${p.url}`.toLowerCase());
        });

        const matchedActions = ACTIONS.filter((a) => {
            if (!terms.length) return true;
            return matchFn(`${a.title} ${a.sub} ${a.tag}`.toLowerCase());
        });

        const matchedCol = q && Array.isArray(collectionItems)
            ? collectionItems.filter((it) => matchFn(it.searchBlob)).slice(0, 24)
            : [];

        results = [...matchedPages, ...matchedCol, ...matchedActions];
        if (activeIdx >= results.length) activeIdx = Math.max(0, results.length - 1);

        if (!results.length) {
            listEl.innerHTML = `<div class="kf-cmd-empty">${loadingCollections ? 'Загрузка коллекций…' : 'Ничего не найдено'}</div>`;
            return;
        }

        let html = '';
        let lastGroup = '';
        results.forEach((item, idx) => {
            if (item.group !== lastGroup) {
                lastGroup = item.group;
                html += `<div class="kf-cmd-group">${esc(lastGroup)}</div>`;
            }
            html += `
                <button type="button" class="kf-cmd-item${idx === activeIdx ? ' active' : ''}" data-cmd-idx="${idx}">
                    <div class="kf-cmd-main">
                        <div class="kf-cmd-title">${esc(item.title)}</div>
                        <div class="kf-cmd-sub">${esc(item.sub)}</div>
                    </div>
                    <span class="kf-cmd-tag">${esc(item.tag)}</span>
                </button>`;
        });
        listEl.innerHTML = html;
    }

    function syncActive() {
        const nodes = listEl.querySelectorAll('.kf-cmd-item');
        nodes.forEach((el, i) => {
            const on = i === activeIdx;
            el.classList.toggle('active', on);
            if (on) el.scrollIntoView({ block: 'nearest' });
        });
    }

    function activateItem(item) {
        close();
        if (typeof item.run === 'function') {
            item.run();
            return;
        }
        if (item.url) {
            location.href = item.url;
        }
    }

    function open() {
        ensureDom();
        loadCollectionsOnce();
        backdrop.classList.add('open');
        inputEl.value = '';
        activeIdx = 0;
        renderResults();
        requestAnimationFrame(() => inputEl.focus());
    }

    function close() {
        if (!backdrop) return;
        backdrop.classList.remove('open');
    }

    function isOpen() {
        return Boolean(backdrop && backdrop.classList.contains('open'));
    }

    window.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.code === 'KeyK' || String(e.key || '').toLowerCase() === 'k')) {
            e.preventDefault();
            e.stopPropagation();
            if (isOpen()) close();
            else open();
            return;
        }
        if (e.key === 'Escape' && isOpen()) {
            e.preventDefault();
            e.stopPropagation();
            close();
            return;
        }
        if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && !isOpen()) {
            const tag = (e.target && e.target.tagName) || '';
            if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || e.target?.isContentEditable) return;
            if (document.querySelector('.modal.open, .mobile-nav.open, .scroll-modal.open, .vod-watch.open')) return;
            e.preventDefault();
            open();
        }
    }, true);
})();
