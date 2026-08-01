/**
 * Date picker thống nhất — lịch tiếng Việt (flatpickr), hiển thị d/m/Y.
 * - .keto-date-input / .datepicker: value hiển thị d/m/Y, ISO lưu data-iso-value
 * - input[type=date]: altInput d/m/Y, .value vẫn Y-m-d (tương thích API)
 */
(function (global) {
    function vnLocale() {
        if (typeof global.flatpickr === 'undefined') return 'vn';
        const l10ns = global.flatpickr.l10ns || {};
        return l10ns.vn || 'vn';
    }

    function parseDateInputToISO(dateStr) {
        if (!dateStr) return '';
        const raw = String(dateStr).trim();
        const dmY = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(raw);
        if (dmY) {
            return `${dmY[3]}-${dmY[2].padStart(2, '0')}-${dmY[1].padStart(2, '0')}`;
        }
        const ymd = /^(\d{4})-(\d{2})-(\d{2})/.exec(raw);
        if (ymd) return `${ymd[1]}-${ymd[2]}-${ymd[3]}`;
        return '';
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

    function asInstance(result) {
        if (!result) return null;
        if (Array.isArray(result)) return result[0] || null;
        return result;
    }

    function getIsoValue(inputOrId) {
        const input = resolveInput(inputOrId);
        if (!input) return '';
        const fromAttr = input.getAttribute('data-iso-value');
        if (fromAttr) return fromAttr;
        if (input._flatpickr && input._flatpickr.selectedDates[0]) {
            return dateToIso(input._flatpickr.selectedDates[0]);
        }
        if (input.type === 'date' && input.value) return input.value;
        const parsed = parseDateInputToISO(input.value);
        return parsed || String(input.value || '').trim();
    }

    function setDate(inputOrId, dateObj, triggerChange) {
        const input = resolveInput(inputOrId);
        if (!input) return;
        const d = dateObj instanceof Date ? dateObj : new Date(dateObj);
        if (Number.isNaN(d.getTime())) return;
        const iso = dateToIso(d);
        if (input._flatpickr && typeof input._flatpickr.setDate === 'function') {
            input._flatpickr.setDate(d, triggerChange !== false);
            input.setAttribute('data-iso-value', iso);
        } else if (input.type === 'date') {
            input.value = iso;
            input.setAttribute('data-iso-value', iso);
        } else {
            input.value = isoToDisplay(iso);
            input.setAttribute('data-iso-value', iso);
        }
    }

    function init(inputOrId, dateObj, options) {
        const input = resolveInput(inputOrId);
        if (!input || typeof global.flatpickr === 'undefined') return null;
        if (input._flatpickr && typeof input._flatpickr.setDate === 'function') {
            if (dateObj) setDate(input, dateObj, false);
            return input._flatpickr;
        }

        const opts = Object.assign({}, options || {});
        const onChangeExtra = opts.onChange;
        delete opts.onChange;

        const cfg = Object.assign({
            dateFormat: 'd/m/Y',
            locale: vnLocale(),
            allowInput: true,
            disableMobile: true,
            onChange(selectedDates) {
                if (selectedDates[0]) {
                    input.setAttribute('data-iso-value', dateToIso(selectedDates[0]));
                } else {
                    input.removeAttribute('data-iso-value');
                }
                if (typeof onChangeExtra === 'function') {
                    onChangeExtra(selectedDates, input.value, input._flatpickr);
                }
            },
        }, opts);

        if (dateObj) {
            const d = dateObj instanceof Date ? dateObj : new Date(dateObj);
            if (!Number.isNaN(d.getTime())) {
                cfg.defaultDate = d;
                input.setAttribute('data-iso-value', dateToIso(d));
            }
        }

        let fp = null;
        try {
            fp = asInstance(global.flatpickr(input, cfg));
        } catch (e) {
            console.warn('KetoDatePicker.init', e);
            return null;
        }

        if (!input.dataset.ketoDateBlur) {
            input.dataset.ketoDateBlur = '1';
            input.addEventListener('blur', function onBlur() {
                const isoValue = parseDateInputToISO(this.value);
                if (isoValue && isoValue !== this.getAttribute('data-iso-value')) {
                    this.value = isoToDisplay(isoValue);
                    this.setAttribute('data-iso-value', isoValue);
                    if (this._flatpickr && typeof this._flatpickr.setDate === 'function') {
                        this._flatpickr.setDate(isoValue, false);
                    }
                    if (typeof onChangeExtra === 'function') {
                        onChangeExtra([new Date(`${isoValue}T00:00:00`)], this.value, this._flatpickr);
                    }
                }
            });
        }

        input.setAttribute('data-keto-date-init', '1');
        return fp;
    }

    /** Native type=date → lịch VN (hiển thị d/m/Y, value vẫn Y-m-d). */
    function initNativeDate(input) {
        if (!input || typeof global.flatpickr === 'undefined') return null;
        if (input.dataset.ketoSkip === '1' || input.getAttribute('data-keto-skip') === '1') return null;
        if (input._flatpickr && typeof input._flatpickr.setDate === 'function') return input._flatpickr;

        const existing = (input.value || '').trim();
        const iso = parseDateInputToISO(existing) || (/^\d{4}-\d{2}-\d{2}$/.test(existing) ? existing : '');
        const classes = (input.className || '').trim();

        const cfg = {
            dateFormat: 'Y-m-d',
            altInput: true,
            altFormat: 'd/m/Y',
            altInputClass: classes ? `${classes} keto-date-alt` : 'form-control keto-date-alt',
            locale: vnLocale(),
            allowInput: true,
            disableMobile: true,
            onChange(selectedDates) {
                if (selectedDates[0]) {
                    input.setAttribute('data-iso-value', dateToIso(selectedDates[0]));
                } else {
                    input.removeAttribute('data-iso-value');
                }
            },
        };
        if (iso) cfg.defaultDate = iso;

        let fp = null;
        try {
            fp = asInstance(global.flatpickr(input, cfg));
        } catch (e) {
            console.warn('KetoDatePicker.initNativeDate', e);
            return null;
        }

        if (iso) input.setAttribute('data-iso-value', iso);
        input.setAttribute('data-keto-date-init', '1');
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
        if (typeof global.flatpickr === 'undefined') return;
        const scope = root || document;

        scope.querySelectorAll('input.keto-date-input:not([data-keto-date-init]), input.datepicker:not([data-keto-date-init])').forEach(input => {
            if (input.type === 'date') {
                initNativeDate(input);
                return;
            }
            const def = input.getAttribute('data-default-date');
            let dateObj = null;
            if (def === 'today') dateObj = new Date();
            else if (def === 'month-start') dateObj = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
            else if (input.value) {
                const iso = parseDateInputToISO(input.value);
                if (iso) dateObj = new Date(`${iso}T00:00:00`);
            }
            init(input, dateObj);
        });

        scope.querySelectorAll('input[type="date"]:not([data-keto-date-init]):not([data-keto-skip])').forEach(input => {
            initNativeDate(input);
        });
    }

    global.KetoDatePicker = {
        parseDateInputToISO,
        dateToIso,
        isoToDisplay,
        getIsoValue,
        setDate,
        init,
        initNativeDate,
        initRange,
        initMonthToToday,
        scan,
        asInstance,
        vnLocale,
    };

    document.addEventListener('DOMContentLoaded', () => {
        scan();
        setTimeout(scan, 0);
        setTimeout(scan, 300);
    });
})(window);
