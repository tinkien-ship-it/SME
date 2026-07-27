/**
 * Bảng báo cáo chi tiết hàng mua/bán: độ rộng cột tùy chỉnh + lưu localStorage.
 */
(function (global) {
    const DETAIL_COL_MIN = {
        stt: 40,
        doc_no: 80,
        invoice_no: 80,
        date: 90,
        product_code: 70,
        product_name: 160,
        unit: 50,
        quantity: 70,
        unit_price: 80,
        discount_pct: 70,
        tax_pct: 60,
        discount_amount: 80,
        tax_amount: 80,
        line_total: 90,
    };

    function loadWidths(storageKey, defaults) {
        try {
            const raw = localStorage.getItem(storageKey);
            if (!raw) return { ...defaults };
            return { ...defaults, ...JSON.parse(raw) };
        } catch (e) {
            return { ...defaults };
        }
    }

    function saveWidths(storageKey, widths) {
        localStorage.setItem(storageKey, JSON.stringify(widths));
    }

    function applyWidths(colgroup, widths, defaults) {
        if (!colgroup) return;
        colgroup.querySelectorAll('col[data-col]').forEach(col => {
            const key = col.dataset.col;
            const px = widths[key] || defaults[key] || 100;
            col.style.width = `${px}px`;
        });
    }

    function initDetailReportTable(options) {
        const {
            tableId = 'detailTable',
            colgroupId = 'detailColgroup',
            storageKey,
            defaults,
            resetBtnId,
        } = options || {};

        const table = document.getElementById(tableId);
        const colgroup = document.getElementById(colgroupId);
        if (!table || !colgroup || !storageKey || !defaults) return;

        let widths = loadWidths(storageKey, defaults);
        applyWidths(colgroup, widths, defaults);

        let activeCol = null;
        let startX = 0;
        let startWidth = 0;

        function onMouseMove(e) {
            if (!activeCol) return;
            const delta = e.clientX - startX;
            const min = DETAIL_COL_MIN[activeCol] || 50;
            widths[activeCol] = Math.max(min, startWidth + delta);
            applyWidths(colgroup, widths, defaults);
        }

        function onMouseUp() {
            if (!activeCol) return;
            saveWidths(storageKey, widths);
            activeCol = null;
            table.classList.remove('detail-resizing');
            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseup', onMouseUp);
        }

        table.querySelectorAll('.col-resizer').forEach(handle => {
            handle.addEventListener('mousedown', e => {
                e.preventDefault();
                e.stopPropagation();
                activeCol = handle.dataset.col;
                startX = e.clientX;
                startWidth = widths[activeCol] || defaults[activeCol] || 100;
                table.classList.add('detail-resizing');
                document.addEventListener('mousemove', onMouseMove);
                document.addEventListener('mouseup', onMouseUp);
            });
        });

        const resetBtn = resetBtnId ? document.getElementById(resetBtnId) : null;
        if (resetBtn) {
            resetBtn.addEventListener('click', () => {
                widths = { ...defaults };
                saveWidths(storageKey, widths);
                applyWidths(colgroup, widths, defaults);
            });
        }
    }

    global.DETAIL_REPORT_COL_DEFAULTS = {
        stt: 48,
        doc_no: 112,
        invoice_no: 120,
        date: 128,
        product_code: 96,
        product_name: 340,
        unit: 68,
        quantity: 88,
        unit_price: 108,
        discount_pct: 92,
        discount_amount: 108,
        tax_pct: 76,
        tax_amount: 100,
        line_total: 128,
    };

    global.initDetailReportTable = initDetailReportTable;
})(window);
