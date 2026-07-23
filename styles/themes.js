(() => {
    'use strict';

    const STORAGE_KEY = 'krafen.theme';
    const themes = [
        { id: 'original', label: 'Оригинальная', description: 'Исходный вид сайта', swatch: 'linear-gradient(135deg, #030303, #5b2333)' },
        { id: 'system', label: 'Система', description: 'Авто — как в ОС', swatch: 'linear-gradient(135deg, #f5f7fc 50%, #0a0c12 50%)' },
        { id: 'light', label: 'Светлая', description: 'Ровный дневной режим', swatch: 'linear-gradient(135deg, #f5f7fc, #5d48e6)' },
        { id: 'dark', label: 'Тёмная', description: 'Глубокий контраст', swatch: 'linear-gradient(135deg, #0a0c12, #8695ff)' },
        { id: 'sakura-noir', label: 'Sakura Noir', description: 'Вишнёвые тени', swatch: 'linear-gradient(135deg, #0b070a, #d35f82)' },
        { id: 'aurora-tide', label: 'Aurora Tide', description: 'Северное сияние', swatch: 'linear-gradient(135deg, #041215, #53d5c5 55%, #6686ff)' },
        { id: 'paper-orbit', label: 'Paper Orbit', description: 'Тёплая бумага и индиго', swatch: 'linear-gradient(135deg, #f4eedf, #303b73 58%, #d16445)' },
        { id: 'neon-grove', label: 'Neon Grove', description: 'Лайм в сумерках', swatch: 'linear-gradient(135deg, #09100c, #b8e84b 58%, #62c97c)' },
        { id: 'violet-arcade', label: 'Violet Arcade', description: 'Аркадный фиолетовый', swatch: 'linear-gradient(135deg, #0d0920, #aa7cff 55%, #ea72d5)' },
        { id: 'emberfall', label: 'Emberfall', description: 'Тёплые искры в ночи', swatch: 'linear-gradient(135deg, #160c09, #ff9e55 58%, #e7513b)' },
        { id: 'mist-garden', label: 'Mist Garden', description: 'Сад в утреннем тумане', swatch: 'linear-gradient(135deg, #edf5ef, #79b77d 58%, #e2b25e)' },
        { id: 'cobalt-ink', label: 'Cobalt Ink', description: 'Чернильный синий', swatch: 'linear-gradient(135deg, #061322, #5cb8ff 58%, #5f7df1)' },
        { id: 'rose-quartz', label: 'Rose Quartz', description: 'Розовый кварц и лиловый', swatch: 'linear-gradient(135deg, #fff2f6, #e998ba 58%, #8f72ca)' },
        { id: 'mono-terminal', label: 'Mono Terminal', description: 'Зелёный терминал', swatch: 'linear-gradient(135deg, #07100c, #78e7a3 58%, #5ec9b2)' },
    ];
    const availableThemes = new Set(themes.map(theme => theme.id));
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');

    function storedPreference() {
        try {
            const value = localStorage.getItem(STORAGE_KEY);
            return availableThemes.has(value) ? value : 'original';
        } catch {
            return 'original';
        }
    }

    function resolvedTheme(preference) {
        return preference === 'system' ? (mediaQuery.matches ? 'dark' : 'light') : preference;
    }

    function applyTheme(preference = storedPreference()) {
        const selected = availableThemes.has(preference) ? preference : 'original';
        const resolved = resolvedTheme(selected);
        const root = document.documentElement;
        root.dataset.theme = resolved;
        root.dataset.themePreference = selected;
        root.style.colorScheme = ['light', 'paper-orbit', 'mist-garden', 'rose-quartz'].includes(resolved) ? 'light' : 'dark';
        return selected;
    }

    // Run immediately to avoid a flash of the wrong palette.
    applyTheme();

    function saveTheme(preference) {
        try {
            localStorage.setItem(STORAGE_KEY, preference);
        } catch {
            // A private browser session can prohibit storage; the selected theme still works.
        }
        applyTheme(preference);
        updateControl();
    }

    let control;
    let trigger;
    let panel;
    let valueLabel;

    function optionMarkup(theme) {
        return `<button class="theme-switcher__option" type="button" role="radio" data-theme-option="${theme.id}" aria-checked="false">
            <span class="theme-switcher__swatch" style="background:${theme.swatch}" aria-hidden="true"></span>
            <span><span class="theme-switcher__option-name">${theme.label}</span><span class="theme-switcher__option-description">${theme.description}</span></span>
            <span class="theme-switcher__check" aria-hidden="true">✓</span>
        </button>`;
    }

    function updateControl() {
        if (!control) return;
        const selected = document.documentElement.dataset.themePreference || 'system';
        const theme = themes.find(item => item.id === selected) || themes[0];
        valueLabel.textContent = theme.label;
        control.querySelectorAll('[data-theme-option]').forEach(option => {
            option.setAttribute('aria-checked', String(option.dataset.themeOption === selected));
        });
    }

    function closePanel() {
        if (!panel || panel.hidden) return;
        panel.hidden = true;
        trigger.setAttribute('aria-expanded', 'false');
    }

    function mountControl() {
        if (document.querySelector('[data-theme-switcher]')) return;

        // Some sections keep the admin lock in the lower-right corner.
        // Reserve its space so the theme launcher never covers that control.
        document.body.classList.toggle('has-admin-toggle', Boolean(document.querySelector('#lock-btn')));

        document.body.insertAdjacentHTML('beforeend', `<div class="theme-switcher" data-theme-switcher>
            <button class="theme-switcher__trigger" type="button" aria-expanded="false" aria-controls="theme-switcher-panel" title="Выбрать тему">
                <span class="theme-switcher__glyph" aria-hidden="true">◐</span><span class="theme-switcher__label">Тема</span>
            </button>
            <section class="theme-switcher__panel" id="theme-switcher-panel" aria-label="Выбор темы" hidden>
                <div class="theme-switcher__heading"><h2 class="theme-switcher__title">Оформление</h2><span class="theme-switcher__value"></span></div>
                <div class="theme-switcher__list" role="radiogroup" aria-label="Тема сайта">${themes.map(optionMarkup).join('')}</div>
            </section>
        </div>`);

        control = document.querySelector('[data-theme-switcher]');
        trigger = control.querySelector('.theme-switcher__trigger');
        panel = control.querySelector('.theme-switcher__panel');
        valueLabel = control.querySelector('.theme-switcher__value');

        trigger.addEventListener('click', () => {
            const willOpen = panel.hidden;
            panel.hidden = !willOpen;
            trigger.setAttribute('aria-expanded', String(willOpen));
            if (willOpen) panel.querySelector('[aria-checked="true"]')?.focus();
        });

        panel.addEventListener('click', event => {
            const option = event.target.closest('[data-theme-option]');
            if (!option) return;
            saveTheme(option.dataset.themeOption);
            closePanel();
            trigger.focus();
        });

        document.addEventListener('click', event => {
            if (control && !control.contains(event.target)) closePanel();
        });
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape') {
                closePanel();
                trigger?.focus();
            }
        });
        updateControl();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountControl, { once: true });
    else mountControl();

    mediaQuery.addEventListener?.('change', () => {
        if (storedPreference() === 'system') {
            applyTheme('system');
            updateControl();
        }
    });
})();
