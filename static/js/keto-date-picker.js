/**
 * Date picker thống nhất toàn hệ thống — lịch tiếng Việt (flatpickr), hiển thị dd/mm/yyyy.
 *
 * - input[type=date]: altInput dd/mm/yyyy, .value vẫn YYYY-MM-DD (tương thích API)
 * - .keto-date-input / .datepicker / [data-keto-date]: value hiển thị dd/mm/yyyy, ISO ở data-iso-value
 * - Tự quét DOM + modal Bootstrap + SweetAlert2 + node mới (MutationObserver)
 */
(function (global) {
    'use strict';

    var SCAN_DEBOUNCE_MS = 80;
    var _scanTimer = null;
    var _observer = null;
    var _modalHooksBound = false;

    function vnLocale() {
        if (typeof global.flatpickr === 'undefined') return 'vn';
        var l10ns = global.flatpickr.l10ns || {};
        return l10ns.vn || 'vn';
    }

    function parseDateInputToISO(dateStr) {
        if (!dateStr) return '';
        var raw = String(dateStr).trim();
        var dmY = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(raw);
        if (dmY) {
            return dmY[3] + '-' + dmY[2].padStart(2, '0') + '-' + dmY[1].padStart(2, '0');
        }
        var ymd = /^(\d{4})-(\d{2})-(\d{2})/.exec(raw);
        if (ymd) return ymd[1] + '-' + ymd[2] + '-' + ymd[3];
        return '';
    }

    function dateToIso(d) {
        if (!(d instanceof Date) || Number.isNaN(d.getTime())) return '';
        return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    }

    function isoToDisplay(iso) {
        if (typeof global.fmtDateVi === 'function') {
            var v = global.fmtDateVi(iso);
            return v === '—' ? '' : v;
        }
        if (typeof global.formatDate === 'function') return global.formatDate(iso);
        if (!iso) return '';
        var parts = String(iso).split(/[-T\s]/);
        if (parts.length < 3) return String(iso);
        return parts[2].padStart(2, '0') + '/' + parts[1].padStart(2, '0') + '/' + parts[0];
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

    function baseCfg(extra) {
        return Object.assign({
            locale: vnLocale(),
            allowInput: true,
            disableMobile: true,
            appendTo: document.body,
            static: false,
            monthSelectorType: 'static',
        }, extra || {});
    }

    function getIsoValue(inputOrId) {
        var input = resolveInput(inputOrId);
        if (!input) return '';
        var fromAttr = input.getAttribute('data-iso-value');
        if (fromAttr) return fromAttr;
        if (input._flatpickr && input._flatpickr.selectedDates[0]) {
            return dateToIso(input._flatpickr.selectedDates[0]);
        }
        if (input.type === 'date' && input.value) return input.value;
        var parsed = parseDateInputToISO(input.value);
        return parsed || String(input.value || '').trim();
    }

    function setDate(inputOrId, dateObj, triggerChange) {
        var input = resolveInput(inputOrId);
        if (!input) return;
        var d = dateObj instanceof Date ? dateObj : new Date(dateObj);
        if (Number.isNaN(d.getTime())) {
            // chấp nhận chuỗi ISO / dd/mm/yyyy
            var isoTry = parseDateInputToISO(dateObj);
            if (!isoTry) return;
            d = new Date(isoTry + 'T00:00:00');
        }
        if (Number.isNaN(d.getTime())) return;
        var iso = dateToIso(d);
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
        var input = resolveInput(inputOrId);
        if (!input || typeof global.flatpickr === 'undefined') return null;
        if (input.dataset.ketoSkip === '1' || input.getAttribute('data-keto-skip') === '1') return null;
        if (input.type === 'date') {
            return initNativeDate(input, dateObj);
        }
        if (input._flatpickr && typeof input._flatpickr.setDate === 'function') {
            if (dateObj) setDate(input, dateObj, false);
            return input._flatpickr;
        }

        var opts = Object.assign({}, options || {});
        var onChangeExtra = opts.onChange;
        delete opts.onChange;

        var cfg = baseCfg(Object.assign({
            dateFormat: 'd/m/Y',
            onChange: function (selectedDates) {
                if (selectedDates[0]) {
                    input.setAttribute('data-iso-value', dateToIso(selectedDates[0]));
                } else {
                    input.removeAttribute('data-iso-value');
                }
                if (typeof onChangeExtra === 'function') {
                    onChangeExtra(selectedDates, input.value, input._flatpickr);
                }
            },
        }, opts));

        if (dateObj) {
            var d = dateObj instanceof Date ? dateObj : new Date(dateObj);
            if (!Number.isNaN(d.getTime())) {
                cfg.defaultDate = d;
                input.setAttribute('data-iso-value', dateToIso(d));
            }
        } else if (input.value) {
            var iso0 = parseDateInputToISO(input.value);
            if (iso0) {
                cfg.defaultDate = iso0;
                input.setAttribute('data-iso-value', iso0);
            }
        }

        var fp = null;
        try {
            fp = asInstance(global.flatpickr(input, cfg));
        } catch (e) {
            console.warn('KetoDatePicker.init', e);
            return null;
        }

        if (!input.dataset.ketoDateBlur) {
            input.dataset.ketoDateBlur = '1';
            input.addEventListener('blur', function onBlur() {
                var isoValue = parseDateInputToISO(this.value);
                if (isoValue && isoValue !== this.getAttribute('data-iso-value')) {
                    this.value = isoToDisplay(isoValue);
                    this.setAttribute('data-iso-value', isoValue);
                    if (this._flatpickr && typeof this._flatpickr.setDate === 'function') {
                        this._flatpickr.setDate(isoValue, false);
                    }
                    if (typeof onChangeExtra === 'function') {
                        onChangeExtra([new Date(isoValue + 'T00:00:00')], this.value, this._flatpickr);
                    }
                }
            });
        }

        input.setAttribute('data-keto-date-init', '1');
        input.setAttribute('placeholder', input.getAttribute('placeholder') || 'dd/mm/yyyy');
        input.setAttribute('autocomplete', 'off');
        return fp;
    }

    /** Native type=date → lịch VN (hiển thị dd/mm/yyyy, value vẫn YYYY-MM-DD). */
    function initNativeDate(input, dateObj) {
        if (!input || typeof global.flatpickr === 'undefined') return null;
        if (input.dataset.ketoSkip === '1' || input.getAttribute('data-keto-skip') === '1') return null;
        if (input._flatpickr && typeof input._flatpickr.setDate === 'function') {
            if (dateObj) setDate(input, dateObj, false);
            return input._flatpickr;
        }

        var existing = (input.value || '').trim();
        var iso = '';
        if (dateObj) {
            var d0 = dateObj instanceof Date ? dateObj : new Date(dateObj);
            if (!Number.isNaN(d0.getTime())) iso = dateToIso(d0);
        }
        if (!iso) iso = parseDateInputToISO(existing) || (/^\d{4}-\d{2}-\d{2}$/.test(existing) ? existing : '');
        var classes = (input.className || '').trim();

        var cfg = baseCfg({
            dateFormat: 'Y-m-d',
            altInput: true,
            altFormat: 'd/m/Y',
            altInputClass: (classes ? classes + ' ' : '') + 'keto-date-alt',
            onChange: function (selectedDates) {
                if (selectedDates[0]) {
                    input.setAttribute('data-iso-value', dateToIso(selectedDates[0]));
                } else {
                    input.removeAttribute('data-iso-value');
                }
            },
        });
        if (iso) cfg.defaultDate = iso;

        var fp = null;
        try {
            fp = asInstance(global.flatpickr(input, cfg));
        } catch (e) {
            console.warn('KetoDatePicker.initNativeDate', e);
            return null;
        }

        if (iso) input.setAttribute('data-iso-value', iso);
        input.setAttribute('data-keto-date-init', '1');
        // Ẩn input gốc type=date khỏi layout (flatpickr đã tạo altInput)
        try {
            input.classList.add('keto-date-native-hidden');
        } catch (e2) { /* ignore */ }
        return fp;
    }

    function initRange(startId, endId, cfg) {
        cfg = cfg || {};
        var onChange = cfg.onChange;
        var wrap = function () { if (typeof onChange === 'function') onChange(); };
        init(startId, cfg.startDate || cfg.start, { onChange: wrap });
        init(endId, cfg.endDate || cfg.end, { onChange: wrap });
    }

    function initMonthToToday(startId, endId, onChange) {
        var now = new Date();
        initRange(startId, endId, {
            startDate: new Date(now.getFullYear(), now.getMonth(), 1),
            endDate: now,
            onChange: onChange,
        });
    }

    function shouldInitInput(input) {
        if (!input || input.nodeName !== 'INPUT') return false;
        if (input.disabled || input.readOnly) return false;
        if (input.dataset.ketoSkip === '1' || input.getAttribute('data-keto-skip') === '1') return false;
        if (input.getAttribute('data-keto-date-init') === '1') return false;
        if (input._flatpickr) return false;
        if (input.classList.contains('flatpickr-alt-input') || input.classList.contains('keto-date-alt')) return false;
        return true;
    }

    function scan(root) {
        if (typeof global.flatpickr === 'undefined') return;
        var scope = root && root.querySelectorAll ? root : document;

        scope.querySelectorAll(
            'input.keto-date-input, input.datepicker, input[data-keto-date], input[data-keto-date="1"]'
        ).forEach(function (input) {
            if (!shouldInitInput(input)) return;
            if (input.type === 'date') {
                initNativeDate(input);
                return;
            }
            var def = input.getAttribute('data-default-date');
            var dateObj = null;
            if (def === 'today') dateObj = new Date();
            else if (def === 'month-start') dateObj = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
            else if (input.value) {
                var iso = parseDateInputToISO(input.value);
                if (iso) dateObj = new Date(iso + 'T00:00:00');
            }
            init(input, dateObj);
        });

        scope.querySelectorAll('input[type="date"]').forEach(function (input) {
            if (!shouldInitInput(input)) return;
            initNativeDate(input);
        });
    }

    function scheduleScan(root) {
        if (_scanTimer) clearTimeout(_scanTimer);
        _scanTimer = setTimeout(function () {
            _scanTimer = null;
            scan(root || document);
        }, SCAN_DEBOUNCE_MS);
    }

    function bindModalHooks() {
        if (_modalHooksBound) return;
        _modalHooksBound = true;
        document.addEventListener('shown.bs.modal', function (ev) {
            scheduleScan(ev.target || document);
        });
        document.addEventListener('shown.bs.offcanvas', function (ev) {
            scheduleScan(ev.target || document);
        });
    }

    function bindSwalHooks() {
        // SweetAlert2: mỗi lần popup mở, quét lại ô ngày trong popup
        if (!global.Swal || typeof global.Swal.fire !== 'function' || global.Swal.__ketoDatePatched) return;
        var originalFire = global.Swal.fire.bind(global.Swal);
        global.Swal.fire = function () {
            var args = arguments;
            var options = args[0];
            if (options && typeof options === 'object' && !Array.isArray(options)) {
                var userDidOpen = options.didOpen;
                options = Object.assign({}, options, {
                    didOpen: function (el) {
                        scheduleScan(el || (global.Swal.getHtmlContainer && global.Swal.getHtmlContainer()));
                        setTimeout(function () {
                            scheduleScan(el || document);
                        }, 50);
                        if (typeof userDidOpen === 'function') userDidOpen(el);
                    },
                });
                return originalFire(options);
            }
            var p = originalFire.apply(global.Swal, args);
            setTimeout(function () {
                var box = global.Swal.getHtmlContainer && global.Swal.getHtmlContainer();
                scheduleScan(box || document);
            }, 50);
            return p;
        };
        global.Swal.__ketoDatePatched = true;
    }

    function bindObserver() {
        if (_observer || typeof MutationObserver === 'undefined' || !document.body) return;
        _observer = new MutationObserver(function (mutations) {
            for (var i = 0; i < mutations.length; i++) {
                var m = mutations[i];
                if (!m.addedNodes || !m.addedNodes.length) continue;
                for (var j = 0; j < m.addedNodes.length; j++) {
                    var node = m.addedNodes[j];
                    if (node.nodeType !== 1) continue;
                    if (
                        node.matches && (
                            node.matches('input[type="date"], input.keto-date-input, input.datepicker, .modal, .swal2-container, .swal2-popup')
                        )
                    ) {
                        scheduleScan(node.matches('input') ? document : node);
                        return;
                    }
                    if (node.querySelector && node.querySelector('input[type="date"], input.keto-date-input, input.datepicker')) {
                        scheduleScan(node);
                        return;
                    }
                }
            }
        });
        _observer.observe(document.body, { childList: true, subtree: true });
    }

    function boot() {
        scan();
        bindModalHooks();
        bindSwalHooks();
        bindObserver();
        setTimeout(scan, 0);
        setTimeout(scan, 300);
        // Swal có thể load sau
        setTimeout(bindSwalHooks, 500);
        setTimeout(bindSwalHooks, 2000);
    }

    global.KetoDatePicker = {
        parseDateInputToISO: parseDateInputToISO,
        dateToIso: dateToIso,
        isoToDisplay: isoToDisplay,
        getIsoValue: getIsoValue,
        setDate: setDate,
        init: init,
        initNativeDate: initNativeDate,
        initRange: initRange,
        initMonthToToday: initMonthToToday,
        scan: scan,
        scheduleScan: scheduleScan,
        ensureHooks: function () {
            bindModalHooks();
            bindSwalHooks();
            bindObserver();
        },
        asInstance: asInstance,
        vnLocale: vnLocale,
    };

    // Alias ngắn cho template
    global.getKetoDateIso = getIsoValue;
    global.setKetoDate = setDate;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})(window);
