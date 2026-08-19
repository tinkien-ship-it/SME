/* Column resize + persist (localStorage) — dùng chung cho các trang import. */
(function (global) {
    'use strict';

    function safeJsonParse(raw) {
        try { return JSON.parse(raw); } catch (e) { return null; }
    }

    function applyWidths(table, widths) {
        if (!widths || !Array.isArray(widths)) return;
        const ths = table.querySelectorAll('thead th');
        const trs = table.querySelectorAll('tbody tr');
        for (let i = 0; i < ths.length && i < widths.length; i++) {
            const w = widths[i];
            if (!w || !Number.isFinite(w)) continue;
            ths[i].style.width = w + 'px';
            ths[i].style.minWidth = w + 'px';
            for (let r = 0; r < trs.length; r++) {
                const cell = trs[r].cells[i];
                if (cell) {
                    cell.style.width = w + 'px';
                }
            }
        }
    }

    function initResizableTableColumns(tableId, storageKey, opts) {
        opts = opts || {};
        const minWidth = parseInt(opts.minWidth || '40', 10);

        const table = document.getElementById(tableId);
        if (!table) return;

        table.style.tableLayout = 'fixed';

        const key = storageKey || ('colWidths:' + tableId);
        const saved = safeJsonParse(localStorage.getItem(key));
        const widths = Array.isArray(saved) ? saved : null;
        if (widths) applyWidths(table, widths);

        const ths = table.querySelectorAll('thead th');
        ths.forEach(function (th, colIndex) {
            th.style.position = th.style.position || 'relative';

            // Nếu đã có handle thì dùng lại
            let handle = th.querySelector('.col-resize-handle');
            if (!handle) {
                handle = document.createElement('div');
                handle.className = 'col-resize-handle';
                handle.style.cssText =
                    'position:absolute; top:0; right:0; height:100%; width:8px; cursor:col-resize; ' +
                    'z-index:5;';
                th.appendChild(handle);
            }

            let startX = 0;
            let startW = 0;

            function onMouseMove(e) {
                const delta = e.clientX - startX;
                const nextW = Math.max(minWidth, startW + delta);
                th.style.width = nextW + 'px';
                th.style.minWidth = nextW + 'px';
                const trs = table.querySelectorAll('tbody tr');
                for (let r = 0; r < trs.length; r++) {
                    const cell = trs[r].cells[colIndex];
                    if (cell) cell.style.width = nextW + 'px';
                }
            }

            function onMouseUp() {
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);

                const currentWidths = [];
                ths.forEach(function (h) {
                    const raw = h.style.width || '';
                    if (raw && raw.toString().endsWith('px')) {
                        const n = parseInt(raw, 10);
                        currentWidths.push(Number.isFinite(n) ? n : h.offsetWidth);
                    } else {
                        currentWidths.push(h.offsetWidth);
                    }
                });
                try {
                    localStorage.setItem(key, JSON.stringify(currentWidths));
                } catch (e) { /* ignore quota */ }
            }

            handle.addEventListener('mousedown', function (e) {
                // Tránh kéo chọn text
                e.preventDefault();
                startX = e.clientX;
                startW = th.offsetWidth;
                document.addEventListener('mousemove', onMouseMove);
                document.addEventListener('mouseup', onMouseUp);
            });
        });
    }

    global.initResizableTableColumns = initResizableTableColumns;
})(window);

