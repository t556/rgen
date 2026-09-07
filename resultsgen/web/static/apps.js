/* Randomizing the application table. Every control here is client-side only:
   the range inputs carry no name attribute, so they are never submitted, and
   the controls stay hidden until this script un-hides them. */
(() => {
    const table = document.getElementById('app-table');
    if (!table) return;

    // Suffix of the form field each column edits, and the defaults its panel starts with.
    const COLUMNS = {
        test_count: {kind: 'int', min: 50, max: 5000, include: true},
        suite_count: {kind: 'int', min: 1, max: 50, include: true},
        personality: {kind: 'choice', choices: ['stable', 'typical', 'noisy', 'legacy'], include: true},
        removals: {kind: 'int', min: 0, max: 5, include: true},
        additions: {kind: 'int', min: 0, max: 5, include: true},
        duration_lo: {kind: 'float', min: 8, max: 60, decimals: 1, include: false},
        duration_hi: {kind: 'float', min: 60, max: 240, decimals: 1, include: false},
        partial_rate: {kind: 'float', min: 0, max: 0.02, decimals: 4, include: false},
    };
    const ORDER = Object.keys(COLUMNS);
    const STORE = 'resultsgen.app-ranges';
    const settings = {};
    for (const [key, spec] of Object.entries(COLUMNS)) {
        settings[key] = {min: spec.min, max: spec.max, include: spec.include};
    }

    const read = () => {
        try {
            const saved = JSON.parse(window.localStorage.getItem(STORE) || '{}');
            for (const key of ORDER) {
                const entry = saved[key];
                if (!entry) continue;
                if (typeof entry.include === 'boolean') settings[key].include = entry.include;
                for (const bound of ['min', 'max']) {
                    if (Number.isFinite(entry[bound])) settings[key][bound] = entry[bound];
                }
            }
        } catch (error) { /* Unreadable or blocked storage just means defaults. */ }
    };
    const write = () => {
        try {
            window.localStorage.setItem(STORE, JSON.stringify(settings));
        } catch (error) { /* Ranges are a convenience; losing them is not an error. */ }
    };

    read();

    const panels = new Map();
    const toggles = new Map();
    for (const panel of document.querySelectorAll('.col-panel[data-column]')) panels.set(panel.dataset.column, panel);
    for (const toggle of table.querySelectorAll('.col-toggle')) toggles.set(toggle.dataset.column, toggle);

    const bounds = (key) => {
        const setting = settings[key];
        const low = Math.min(setting.min, setting.max);
        return [low, Math.max(setting.min, setting.max)];
    };
    const draw = (key) => {
        const spec = COLUMNS[key];
        if (spec.kind === 'choice') return spec.choices[Math.floor(Math.random() * spec.choices.length)];
        const [low, high] = bounds(key);
        if (spec.kind === 'int') return String(Math.floor(low + Math.random() * (high - low + 1)));
        return (low + Math.random() * (high - low)).toFixed(spec.decimals);
    };

    const cell = (row, key) => row.querySelector(`[name$="_${key}"]`);
    const flash = (field) => {
        field.classList.remove('flash');
        void field.offsetWidth;  // Restart the animation when the same field is redrawn.
        field.classList.add('flash');
    };
    const put = (row, key, value) => {
        const field = cell(row, key);
        if (!field) return;
        field.value = value;
        flash(field);
    };

    const number = (row, key, parse) => {
        const field = cell(row, key);
        return field ? parse(field.value) : NaN;
    };
    const settle = (row) => {
        // Keep drawn values inside the rules the config validator enforces.
        const tests = number(row, 'test_count', (value) => Number.parseInt(value, 10));
        const suites = number(row, 'suite_count', (value) => Number.parseInt(value, 10));
        if (Number.isFinite(tests) && Number.isFinite(suites) && suites > tests) {
            put(row, 'suite_count', String(Math.max(1, tests)));
        }
        const low = number(row, 'duration_lo', Number.parseFloat);
        const high = number(row, 'duration_hi', Number.parseFloat);
        if (Number.isFinite(low) && Number.isFinite(high) && low > high) {
            put(row, 'duration_lo', high.toFixed(COLUMNS.duration_hi.decimals));
            put(row, 'duration_hi', low.toFixed(COLUMNS.duration_lo.decimals));
        }
    };

    const randomizeRow = (row, keys) => {
        for (const key of keys) put(row, key, draw(key));
        settle(row);
    };
    const rows = () => [...table.tBodies[0].rows];
    const included = () => ORDER.filter((key) => settings[key].include);

    const announce = (text) => {
        const status = document.getElementById('randomize-status');
        if (status) status.textContent = text;
    };

    // Panels are fixed to the viewport and anchored under their heading button.
    const place = (key) => {
        const panel = panels.get(key);
        const anchor = toggles.get(key).getBoundingClientRect();
        const clamp = (value, size, limit) => Math.max(8, Math.min(value, limit - size - 8));
        panel.style.left = `${clamp(anchor.left, panel.offsetWidth, window.innerWidth)}px`;
        const height = panel.offsetHeight;
        const below = anchor.bottom + 4;
        // Flip above the heading when the panel would not fit below it.
        panel.style.top = `${clamp(below + height + 8 > window.innerHeight ? anchor.top - height - 4 : below, height, window.innerHeight)}px`;
    };
    const openKey = () => [...panels.keys()].find((key) => !panels.get(key).hidden);
    const close = (key, restoreFocus = false) => {
        if (key === undefined || panels.get(key).hidden) return;
        panels.get(key).hidden = true;
        toggles.get(key).setAttribute('aria-expanded', 'false');
        if (restoreFocus) toggles.get(key).focus();
    };
    const open = (key) => {
        close(openKey());
        panels.get(key).hidden = false;
        toggles.get(key).setAttribute('aria-expanded', 'true');
        place(key);
        const first = panels.get(key).querySelector('[data-range]');
        if (first) first.focus();
    };

    for (const [key, panel] of panels) {
        const setting = settings[key];
        const field = (bound) => panel.querySelector(`[data-range="${bound}"]`);
        const include = field('include');
        for (const bound of ['min', 'max']) {
            const input = field(bound);
            if (!input) continue;
            input.value = String(setting[bound]);
            input.addEventListener('change', () => {
                const value = Number.parseFloat(input.value);
                if (Number.isFinite(value)) setting[bound] = value;
                else input.value = String(setting[bound]);
                write();
            });
        }
        include.checked = setting.include;
        include.addEventListener('change', () => {
            setting.include = include.checked;
            write();
        });
        panel.querySelector('[data-action="randomize-column"]').addEventListener('click', () => {
            for (const row of rows()) {
                put(row, key, draw(key));
                settle(row);
            }
            announce(`Randomized ${panel.dataset.label} for ${rows().length} application(s).`);
        });
        panel.querySelector('[data-action="reset-range"]').addEventListener('click', () => {
            Object.assign(setting, {min: COLUMNS[key].min, max: COLUMNS[key].max, include: COLUMNS[key].include});
            for (const bound of ['min', 'max']) {
                const input = field(bound);
                if (input) input.value = String(setting[bound]);
            }
            include.checked = setting.include;
            write();
        });
        const toggle = toggles.get(key);
        toggle.addEventListener('click', () => (panel.hidden ? open(key) : close(key)));
        toggle.hidden = false;
    }

    for (const button of table.querySelectorAll('[data-randomize-row]')) {
        button.addEventListener('click', () => {
            const keys = included();
            if (!keys.length) {
                announce('No columns are included in randomize. Open a column menu to include one.');
                return;
            }
            randomizeRow(button.closest('tr'), keys);
            announce(`Randomized ${keys.length} value(s) for one application.`);
        });
        button.hidden = false;
    }

    const all = document.getElementById('randomize-all');
    if (all) {
        all.addEventListener('click', () => {
            const keys = included();
            if (!keys.length) {
                announce('No columns are included in randomize. Open a column menu to include one.');
                return;
            }
            for (const row of rows()) randomizeRow(row, keys);
            announce(`Randomized ${keys.length} value(s) for ${rows().length} application(s).`);
        });
        all.hidden = false;
    }

    document.addEventListener('click', (event) => {
        const key = openKey();
        if (key !== undefined && !panels.get(key).contains(event.target) && !toggles.get(key).contains(event.target)) close(key);
    });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') close(openKey(), true);
    });
    for (const event of ['scroll', 'resize']) {
        window.addEventListener(event, () => {
            const key = openKey();
            if (key !== undefined) place(key);
        }, true);
    }
})();
