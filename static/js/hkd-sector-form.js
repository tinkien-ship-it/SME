/** NN1–NN4 & DT1–DT4 — form tạo tenant / đăng ký trial. */
(function (global) {
    'use strict';

    let _cache = null;

    function escapeHtml(text) {
        return String(text || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function isSmeRegime(code) {
        return String(code || '').toUpperCase().indexOf('SME') === 0;
    }

    async function loadProfileOptions() {
        if (_cache) return _cache;
        const res = await fetch('/api/tenant/profile/options');
        const data = await res.json();
        if (!data.success && !data.nn_sectors && !data.hkd_sectors) {
            throw new Error(data.error || 'Không tải được cấu hình');
        }
        data.nn_sectors = data.nn_sectors || data.hkd_sectors || [];
        data.nn_sector_legal_intro = data.nn_sector_legal_intro || data.hkd_sector_legal_intro || '';
        _cache = data;
        return data;
    }

    function buildDtTierOptions(selectedCode, opts) {
        opts = opts || {};
        const allowEmpty = !!opts.allowEmpty;
        const tiers = (_cache && _cache.revenue_tiers) || [
            { code: 'DT1', label: 'DT1 — Doanh thu ≤ 1 tỷ/năm' },
            { code: 'DT2', label: 'DT2 — Doanh thu 1–3 tỷ/năm' },
            { code: 'DT3', label: 'DT3 — Doanh thu 3–50 tỷ/năm' },
            { code: 'DT4', label: 'DT4 — Doanh thu > 50 tỷ/năm' },
        ];
        let html = '';
        if (allowEmpty) {
            const emptySel = !selectedCode ? ' selected' : '';
            html += `<option value=""${emptySel}>— Không áp dụng —</option>`;
        }
        html += tiers.map(function (t) {
            const sel = (selectedCode || '') === t.code ? ' selected' : '';
            return `<option value="${t.code}"${sel}>${escapeHtml(t.label || t.code)}</option>`;
        }).join('');
        return html;
    }

    function buildRegimeOptions(selectedCode) {
        const regimes = (_cache && _cache.accounting_regimes) || [
            { code: 'HKD', label: 'HKD (Hộ kinh doanh)', selectable: true },
        ];
        const selected = selectedCode || 'HKD';
        return regimes.map(function (r) {
            const sel = selected === r.code ? ' selected' : '';
            const disabled = r.selectable === false ? ' disabled' : '';
            const soon = r.coming_soon ? ' (sắp ra mắt)' : '';
            return (
                `<option value="${escapeHtml(r.code)}"${sel}${disabled}>` +
                `${escapeHtml(r.label || r.code)}${soon}</option>`
            );
        }).join('');
    }

    function buildNnCheckboxGroup(containerId, helpId, sectors, selectedCodes, legalIntro) {
        selectedCodes = selectedCodes && selectedCodes.length ? selectedCodes : [];
        const intro = escapeHtml(legalIntro || '');
        let boxes = (sectors || []).map(function (s) {
            const checked = selectedCodes.indexOf(s.code) >= 0 ? ' checked' : '';
            return (
                `<div class="form-check">` +
                `<input class="form-check-input nn-sector-cb" type="checkbox" value="${s.code}" ` +
                `id="${containerId}-${s.code}" title="${escapeHtml(s.tooltip || s.help_text)}"${checked}>` +
                `<label class="form-check-label" for="${containerId}-${s.code}" ` +
                `title="${escapeHtml(s.tooltip || s.help_text)}">${escapeHtml(s.label || s.code)}</label>` +
                `</div>`
            );
        }).join('');
        return (
            `<div class="mb-1" id="${containerId}-wrap">` +
            `<label class="form-label small fw-bold mb-1" title="${intro}">` +
            `Ngành nghề kinh doanh (NN1–NN4) — chọn một hoặc nhiều` +
            ` <i class="fas fa-circle-info text-primary" title="${intro}"></i>` +
            `</label>` +
            `<div id="${containerId}" class="border rounded p-2 bg-light">${boxes}</div>` +
            `<div id="${helpId}" class="alert alert-light border small py-2 px-2 mt-1 mb-0" role="note"></div>` +
            `<div class="form-text">${intro}</div>` +
            `<div class="form-text nn-hkd-hint">HKD đa ngành: ví dụ DT1 có thể kinh doanh NN1 + NN2 + NN3 cùng lúc.</div>` +
            `</div>`
        );
    }

    function getSelectedNnSectors(containerId, opts) {
        opts = opts || {};
        const allowEmpty = !!opts.allowEmpty;
        const root = document.getElementById(containerId);
        if (!root) return allowEmpty ? [] : ['NN1'];
        const picked = Array.from(root.querySelectorAll('.nn-sector-cb:checked')).map(function (el) {
            return el.value;
        });
        if (picked.length) return picked;
        return allowEmpty ? [] : ['NN1'];
    }

    function clearNnSectors(containerId) {
        const root = document.getElementById(containerId);
        if (!root) return;
        root.querySelectorAll('.nn-sector-cb').forEach(function (el) {
            el.checked = false;
        });
    }

    function setNnSectors(containerId, codes) {
        const root = document.getElementById(containerId);
        if (!root) return;
        const set = codes || [];
        root.querySelectorAll('.nn-sector-cb').forEach(function (el) {
            el.checked = set.indexOf(el.value) >= 0;
        });
    }

    function renderNnHelp(sectors, selected, helpEl, legalIntro) {
        if (!helpEl) return;
        const lines = (selected || []).map(function (code) {
            const s = (sectors || []).find(function (x) { return x.code === code; });
            if (!s) return '';
            return `<div class="mb-1"><strong>${escapeHtml(s.code)}</strong> — ${escapeHtml(s.title)}</div>`;
        }).join('');
        helpEl.innerHTML = lines || `<div class="text-muted">${escapeHtml(legalIntro || '')}</div>`;
    }

    function bindNnSectorHelp(containerId, helpId, sectors, legalIntro) {
        const root = document.getElementById(containerId);
        const helpEl = document.getElementById(helpId);
        if (!root || !helpEl) return;
        const refresh = function () {
            renderNnHelp(sectors, getSelectedNnSectors(containerId, { allowEmpty: true }), helpEl, legalIntro);
        };
        root.querySelectorAll('.nn-sector-cb').forEach(function (el) {
            el.addEventListener('change', refresh);
            el.addEventListener('mouseenter', refresh);
        });
        refresh();
    }

    function suggestNnForBusinessLine(businessLine, sectors) {
        if (businessLine === 'fb_service') return ['NN2', 'NN1'];
        if (businessLine === 'rental_service') return ['NN2'];
        return ['NN1', 'NN2', 'NN3', 'NN4'];
    }

    function bindBusinessLineNnSuggest(businessLineSelectId, nnContainerId, helpId, sectors, legalIntro) {
        const bl = document.getElementById(businessLineSelectId);
        if (!bl) return;
        bl.addEventListener('change', function () {
            const regimeEl = document.getElementById('swal-regime');
            if (regimeEl && isSmeRegime(regimeEl.value)) return;
            const suggested = suggestNnForBusinessLine(bl.value, sectors);
            setNnSectors(nnContainerId, suggested);
            bindNnSectorHelp(nnContainerId, helpId, sectors, legalIntro);
        });
    }

    /**
     * Khi chọn DN / DN siêu nhỏ → DT & NN để trống + khóa.
     * Khi chọn HKD → bật lại và gợi ý mặc định.
     */
    function bindRegimeHkdFields(regimeSelectId, opts) {
        opts = opts || {};
        const regimeEl = document.getElementById(regimeSelectId);
        const tierEl = document.getElementById(opts.tierSelectId || 'swal-revenue-tier');
        const nnBox = opts.nnContainerId || 'swal-nn-box';
        const helpId = opts.helpId || 'swal-nn-help';
        const wrap = document.getElementById(nnBox + '-wrap');
        const vatWrap = document.getElementById(opts.vatFilingWrapId || 'swal-vat-filing-wrap');
        const vatEl = document.getElementById(opts.vatFilingSelectId || 'swal-vat-filing');
        const sectors = opts.sectors || [];
        const legalIntro = opts.legalIntro || '';
        if (!regimeEl) return;

        const apply = function () {
            const sme = isSmeRegime(regimeEl.value);
            if (tierEl) {
                tierEl.disabled = sme;
                if (sme) {
                    tierEl.value = '';
                } else if (!tierEl.value) {
                    tierEl.value = 'DT1';
                }
            }
            const root = document.getElementById(nnBox);
            if (root) {
                root.querySelectorAll('.nn-sector-cb').forEach(function (el) {
                    el.disabled = sme;
                    if (sme) el.checked = false;
                });
            }
            if (wrap) {
                wrap.style.opacity = sme ? '0.55' : '1';
                wrap.style.pointerEvents = sme ? 'none' : '';
            }
            const hint = wrap && wrap.querySelector('.nn-hkd-hint');
            if (hint) {
                hint.textContent = sme
                    ? 'Doanh nghiệp (TT58/TT99) không dùng nhóm doanh thu DT hay ngành nghề NN của HKD.'
                    : 'HKD đa ngành: ví dụ DT1 có thể kinh doanh NN1 + NN2 + NN3 cùng lúc.';
            }
            if (vatWrap) {
                vatWrap.style.display = sme ? '' : 'none';
            }
            if (sme && vatEl && !vatEl.dataset.userTouched) {
                const code = String(regimeEl.value || '').toUpperCase();
                // Chỉ gợi ý mặc định khi tạo mới / chưa chọn
                if (!vatEl.value || vatEl.dataset.autoDefault === '1') {
                    vatEl.value = code.indexOf('TT58') >= 0 ? 'quarterly' : 'monthly';
                    vatEl.dataset.autoDefault = '1';
                }
            }
            if (!sme && root && !root.querySelector('.nn-sector-cb:checked')) {
                const bl = document.getElementById(opts.businessLineId || 'swal-business-line');
                setNnSectors(nnBox, suggestNnForBusinessLine(bl ? bl.value : 'pos', sectors));
            }
            bindNnSectorHelp(nnBox, helpId, sectors, legalIntro);
        };

        if (vatEl && !vatEl._vatBound) {
            vatEl.addEventListener('change', function () {
                vatEl.dataset.userTouched = '1';
                vatEl.dataset.autoDefault = '0';
            });
            vatEl._vatBound = true;
        }

        regimeEl.addEventListener('change', apply);
        apply();
    }

    global.HkdSectorForm = {
        loadProfileOptions: loadProfileOptions,
        buildDtTierOptions: buildDtTierOptions,
        buildRegimeOptions: buildRegimeOptions,
        buildNnCheckboxGroup: buildNnCheckboxGroup,
        getSelectedNnSectors: getSelectedNnSectors,
        clearNnSectors: clearNnSectors,
        setNnSectors: setNnSectors,
        bindNnSectorHelp: bindNnSectorHelp,
        bindBusinessLineNnSuggest: bindBusinessLineNnSuggest,
        bindRegimeHkdFields: bindRegimeHkdFields,
        suggestNnForBusinessLine: suggestNnForBusinessLine,
        isSmeRegime: isSmeRegime,
    };
})(window);
