/**
 * Date picker thống nhất (flatpickr d/m/Y) — cùng chuẩn với inventory.html
 */
(function (global) {
    function parseDateInputToISO(dateStr) {
        if (!dateStr) return '';
        const m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(String(dateStr).trim());
        if (!m) return '';
        return `${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`;
    }

    function dateToIso(d) {
        if (!(d instanceof Date) || Number.isNaN(d.getTime())) return '';
        return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    }

    function isoToDisplay(iso) {
        if (typeof global.formatDate === 'function') return global.formatDate(iso);
        if (!iso) return '';
        const parts = String(iso).split(/[-T\s]/);
        if (parts.length < 3) return iso;
        return `${parts[2].padStart(2, '0')}/${parts[1].padStart(2, '0')}/${parts[0]}`;
    }

    function resolveInput(inputOrId) {
        if (!inputOrId) return null;
        return typeof inputOrId === 'string' ? document.getElementById(inputOrId) : inputOrId;
    }

    function getIsoValue(inputOrId) {
        const input = resolveInput(inputOrId);
        if (!input) return '';
        const fromAttr = input.getAttribute('data-iso-value');
        if (fromAttr) return fromAttr;
        if (input._flatpickr && input._flatpickr.selectedDates[0]) {
            return dateToIso(input._flatpickr.selectedDates[0]);
        }
        const parsed = parseDateInputToISO(input.value);
        return parsed || String(input.value || '').trim();
    }

    function setDate(inputOrId, dateObj, triggerChange) {
        const input = resolveInput(inputOrId);
        if (!input) return;
        const d = dateObj instanceof Date ? dateObj : new Date(dateObj);
        if (Number.isNaN(d.getTime())) return;
        const iso = dateToIso(d);
        if (input._flatpickr) {
            input._flatpickr.setDate(d, triggerChange !== false);
            input.setAttribute('data-iso-value', iso);
        } else {
            input.value = isoToDisplay(iso);
            input.setAttribute('data-iso-value', iso);
        }
    }

    function init(inputOrId, dateObj, options) {
        const input = resolveInput(inputOrId);
        if (!input || typeof global.flatpickr === 'undefined') return null;
        if (input._flatpickr) {
            if (dateObj) setDate(input, dateObj, false);
            return input._flatpickr;
        }

        const opts = Object.assign({}, options || {});
        const onChangeExtra = opts.onChange;
        delete opts.onChange;

        if (dateObj) {
            const d = dateObj instanceof Date ? dateObj : new Date(dateObj);
            if (!Number.isNaN(d.getTime())) {
                input.value = isoToDisplay(dateToIso(d));
                input.setAttribute('data-iso-value', dateToIso(d));
            }
        }

        const fp = global.flatpickr(input, Object.assign({
            dateFormat: 'd/m/Y',
            locale: 'vn',
            allowInput: true,
            disableMobile: true,
            onChange(selectedDates) {
                if (selectedDates[0]) {
                    input.setAttribute('data-iso-value', dateToIso(selectedDates[0]));
                } else {
                    input.removeAttribute('data-iso-value');
                }
                if (typeof onChangeExtra === 'function') {
                    onChangeExtra(selectedDates, input.value, fp);
                }
            },
        }, opts));

        if (!input.dataset.ketoDateBlur) {
            input.dataset.ketoDateBlur = '1';
            input.addEventListener('blur', function onBlur() {
                const isoValue = parseDateInputToISO(this.value);
                if (isoValue && isoValue !== this.getAttribute('data-iso-value')) {
                    this.value = isoToDisplay(isoValue);
                    this.setAttribute('data-iso-value', isoValue);
                    if (typeof onChangeExtra === 'function') {
                        onChangeExtra([new Date(`${isoValue}T00:00:00`)], this.value, fp);
                    }
                }
            });
        }

        return fp;
    }

    function initRange(startId, endId, cfg) {
        cfg = cfg || {};
        const onChange = cfg.onChange;
        const wrap = () => { if (typeof onChange === 'function') onChange(); };
        init(startId, cfg.startDate || cfg.start, { onChange: wrap });
        init(endId, cfg.endDate || cfg.end, { onChange: wrap });
    }

    function initMonthToToday(startId, endId, onChange) {
        const now = new Date();
        initRange(startId, endId, {
            startDate: new Date(now.getFullYear(), now.getMonth(), 1),
            endDate: now,
            onChange,
        });
    }

    function scan(root) {
        (root || document).querySelectorAll('input.keto-date-input:not([data-keto-date-init])').forEach(input => {
            input.setAttribute('data-keto-date-init', '1');
            const def = input.getAttribute('data-default-date');
            let dateObj = null;
            if (def === 'today') dateObj = new Date();
            else if (def === 'month-start') dateObj = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
            init(input, dateObj);
        });
    }

    global.KetoDatePicker = {
        parseDateInputToISO,
        dateToIso,
        isoToDisplay,
        getIsoValue,
        setDate,
        init,
        initRange,
        initMonthToToday,
        scan,
    };

    document.addEventListener('DOMContentLoaded', () => scan());
})(window);
