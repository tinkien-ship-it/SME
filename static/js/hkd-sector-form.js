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

    function buildDtTierOptions(selectedCode) {
        const tiers = (_cache && _cache.revenue_tiers) || [
            { code: 'DT1', label: 'DT1 — Doanh thu ≤ 1 tỷ/năm' },
            { code: 'DT2', label: 'DT2 — Doanh thu 1–3 tỷ/năm' },
            { code: 'DT3', label: 'DT3 — Doanh thu 3–50 tỷ/năm' },
            { code: 'DT4', label: 'DT4 — Doanh thu > 50 tỷ/năm' },
        ];
        return tiers.map(function (t) {
            const sel = (selectedCode || 'DT1') === t.code ? ' selected' : '';
            return `<option value="${t.code}"${sel}>${escapeHtml(t.label || t.code)}</option>`;
        }).join('');
    }

    function buildNnCheckboxGroup(containerId, helpId, sectors, selectedCodes, legalIntro) {
        selectedCodes = selectedCodes && selectedCodes.length ? selectedCodes : ['NN1'];
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
            `<div class="mb-1">` +
            `<label class="form-label small fw-bold mb-1" title="${intro}">` +
            `Ngành nghề kinh doanh (NN1–NN4) — chọn một hoặc nhiều` +
            ` <i class="fas fa-circle-info text-primary" title="${intro}"></i>` +
            `</label>` +
            `<div id="${containerId}" class="border rounded p-2 bg-light">${boxes}</div>` +
            `<div id="${helpId}" class="alert alert-light border small py-2 px-2 mt-1 mb-0" role="note"></div>` +
            `<div class="form-text">${intro}</div>` +
            `<div class="form-text">HKD đa ngành: ví dụ DT1 có thể kinh doanh NN1 + NN2 + NN3 cùng lúc.</div>` +
            `</div>`
        );
    }

    function getSelectedNnSectors(containerId) {
        const root = document.getElementById(containerId);
        if (!root) return ['NN1'];
        const picked = Array.from(root.querySelectorAll('.nn-sector-cb:checked')).map(function (el) {
            return el.value;
        });
        return picked.length ? picked : ['NN1'];
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
            renderNnHelp(sectors, getSelectedNnSectors(containerId), helpEl, legalIntro);
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
            const suggested = suggestNnForBusinessLine(bl.value, sectors);
            const root = document.getElementById(nnContainerId);
            if (!root) return;
            root.querySelectorAll('.nn-sector-cb').forEach(function (el) {
                el.checked = suggested.indexOf(el.value) >= 0;
            });
            bindNnSectorHelp(nnContainerId, helpId, sectors, legalIntro);
        });
    }

    global.HkdSectorForm = {
        loadProfileOptions: loadProfileOptions,
        buildDtTierOptions: buildDtTierOptions,
        buildNnCheckboxGroup: buildNnCheckboxGroup,
        getSelectedNnSectors: getSelectedNnSectors,
        bindNnSectorHelp: bindNnSectorHelp,
        bindBusinessLineNnSuggest: bindBusinessLineNnSuggest,
        suggestNnForBusinessLine: suggestNnForBusinessLine,
    };
})(window);
