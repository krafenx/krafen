(() => {
    'use strict';

    const STORAGE_KEY = 'krafen.theme';
    const themes = [
        { id: 'original', label: 'Оригинальная', swatch: 'linear-gradient(135deg, #030303, #5b2333)' },
        { id: 'dusky-orchid', label: 'Dusky Orchid', swatch: '#9a7182' },
        { id: 'red-velvet', label: 'Red Velvet', swatch: 'linear-gradient(135deg, #19171b 25%, #75020f 25% 50%, #51080d 50% 75%, #2b0307 75%)' },
        { id: 'deep-amethyst', label: 'Deep Amethyst', swatch: '#1c101a' },
        { id: 'neon-navy', label: 'Neon Navy', swatch: '#131936' },
        { id: 'ponderosa-pine', label: 'Ponderosa Pine', swatch: '#0f2c24' },
        { id: 'plum-noir', label: 'Plum Noir', swatch: '#351e28' },
        { id: 'coffee-bag', label: 'Coffee Bag', swatch: 'linear-gradient(135deg, #dad6d4 25%, #aeaaa8 25% 50%, #7a7774 50% 75%, #3b3a37 75%)' },
        { id: 'eerie-black', label: 'Eerie Black', swatch: 'linear-gradient(135deg, #1b1b1b 25%, #595959 25% 50%, #a2a2a2 50% 75%, #dddddd 75%)' },
    ];
    const validThemes = new Set(themes.map(theme => theme.id));
    const root = document.documentElement;

    function readTheme() {
        try {
            const stored = localStorage.getItem(STORAGE_KEY);
            return validThemes.has(stored) ? stored : 'original';
        } catch {
            return 'original';
        }
    }

    function applyTheme(themeId) {
        const selected = validThemes.has(themeId) ? themeId : 'original';
        root.dataset.theme = selected;
        root.dataset.themePreference = selected;
        root.style.colorScheme = selected === 'coffee-bag' ? 'light' : 'dark';
        return selected;
    }

    // Apply the saved palette before mounting the control to avoid a color flash.
    applyTheme(readTheme());

    function mountControl() {
        if (document.querySelector('[data-theme-switcher]')) return;

        const hasAdminToggle = Boolean(document.querySelector('#lock-btn'));
        document.body.classList.toggle('has-admin-toggle', hasAdminToggle);
        document.body.insertAdjacentHTML('beforeend', `<div class="theme-switcher" data-theme-switcher>
            <button class="theme-switcher__trigger" type="button" aria-expanded="false" aria-controls="theme-switcher-panel" title="Выбрать тему">
                <span aria-hidden="true">◐</span><span>Тема</span>
            </button>
            <section class="theme-switcher__panel" id="theme-switcher-panel" aria-label="Выбор темы" hidden>
                ${themes.map(theme => `<button class="theme-switcher__option" type="button" role="radio" data-theme-option="${theme.id}" aria-checked="false">
                    <span class="theme-switcher__swatch" style="background:${theme.swatch}" aria-hidden="true"></span>
                    <span class="theme-switcher__option-name">${theme.label}</span>
                    <span class="theme-switcher__option-check" aria-hidden="true">✓</span>
                </button>`).join('')}
            </section>
        </div>`);

        const control = document.querySelector('[data-theme-switcher]');
        const trigger = control.querySelector('.theme-switcher__trigger');
        const panel = control.querySelector('.theme-switcher__panel');

        function closePanel() {
            panel.hidden = true;
            trigger.setAttribute('aria-expanded', 'false');
        }

        function updateOptions() {
            control.querySelectorAll('[data-theme-option]').forEach(option => {
                option.setAttribute('aria-checked', String(option.dataset.themeOption === root.dataset.theme));
            });
        }

        trigger.addEventListener('click', () => {
            const willOpen = panel.hidden;
            panel.hidden = !willOpen;
            trigger.setAttribute('aria-expanded', String(willOpen));
        });

        panel.addEventListener('click', event => {
            const option = event.target.closest('[data-theme-option]');
            if (!option) return;
            const selected = applyTheme(option.dataset.themeOption);
            try {
                localStorage.setItem(STORAGE_KEY, selected);
            } catch {
                // The selection remains active until the page is reloaded.
            }
            updateOptions();
            closePanel();
            trigger.focus();
        });

        document.addEventListener('click', event => {
            if (!control.contains(event.target)) closePanel();
        });
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape' && !panel.hidden) {
                closePanel();
                trigger.focus();
            }
        });

        updateOptions();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', mountControl, { once: true });
    } else {
        mountControl();
    }
})();
