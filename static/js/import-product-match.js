/** Liên kết tên HĐ NCC với SKU trong kho — modal, không gắn chip trên dòng. */
(function (global) {
    const HIGH_SCORE = 70;
    const CAND_KEY = '_ketoCands';
    let injected = false;
    let hooksRef = { setProductToRow: null };

    function ensureUi() {
        if (injected) return;
        injected = true;
        const st = document.createElement('style');
        st.textContent = [
            'tr.row-exact-match { background-color: #ecfdf5 !important; }',
            'tr.row-suggested { background-color: #fffbeb !important; }',
            '.match-open-btn { font-size: 11px; padding: 1px 8px; white-space: nowrap; }',
            '.keto-link-overlay { position: fixed; inset: 0; z-index: 20000; background: rgba(15,23,42,.45); display: flex; align-items: center; justify-content: center; padding: 16px; }',
            '.keto-link-overlay.d-none { display: none !important; }',
            '.keto-link-card { background: #fff; border-radius: 12px; width: min(920px, 100%); max-height: 86vh; display: flex; flex-direction: column; box-shadow: 0 20px 50px rgba(15,23,42,.25); }',
            '.keto-link-card header { padding: 16px 20px 12px; border-bottom: 1px solid #e2e8f0; }',
            '.keto-link-card header h5 { margin: 0; font-weight: 700; }',
            '.keto-link-card .keto-link-body { padding: 12px 20px; overflow: auto; }',
            '.keto-link-card table { width: 100%; font-size: 13px; }',
            '.keto-link-card th { text-align: left; color: #64748b; font-weight: 600; padding: 6px 8px; border-bottom: 1px solid #e2e8f0; }',
            '.keto-link-card td { padding: 8px; vertical-align: middle; border-bottom: 1px solid #f1f5f9; }',
            '.keto-link-card footer { padding: 12px 20px; border-top: 1px solid #e2e8f0; display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }',
        ].join('\n');
        document.head.appendChild(st);

        const wrap = document.createElement('div');
        wrap.id = 'ketoLinkModal';
        wrap.className = 'keto-link-overlay d-none';
        wrap.innerHTML = [
            '<div class="keto-link-card" role="dialog" aria-modal="true">',
            '  <header>',
            '    <h5>Liên kết hàng hóa trên hóa đơn</h5>',
            '    <p class="small text-muted mb-0">Tên trên HĐ được giữ nguyên. Chọn mã hàng đã có trong kho hoặc tạo hàng mới.</p>',
            '  </header>',
            '  <div class="keto-link-body"><table><thead><tr>',
            '    <th>Tên trên hóa đơn</th><th class="text-end">SL</th><th>Hàng trong kho</th><th></th>',
            '  </tr></thead><tbody></tbody></table></div>',
            '  <footer>',
            '    <a class="btn btn-link btn-sm me-auto" href="/product-aliases">Sửa liên kết đã lưu</a>',
            '    <button type="button" class="btn btn-outline-secondary btn-sm" data-act="close">Đóng</button>',
            '    <button type="button" class="btn btn-outline-dark btn-sm" data-act="new-all">Tạo mới tất cả</button>',
            '    <button type="button" class="btn btn-primary btn-sm" data-act="apply">Áp dụng liên kết</button>',
            '  </footer>',
            '</div>',
        ].join('');
        document.body.appendChild(wrap);
        wrap.addEventListener('click', function (e) {
            if (e.target === wrap) hideModal();
        });
        wrap.querySelector('[data-act="close"]').addEventListener('click', hideModal);
        wrap.querySelector('[data-act="new-all"]').addEventListener('click', function () {
            pendingRows().forEach(markCreateNew);
            hideModal();
        });
        wrap.querySelector('[data-act="apply"]').addEventListener('click', applyModal);
    }

    function supplierId() {
        return parseInt(document.getElementById('supplierId')?.value, 10) || null;
    }

    function invoiceNameOf(row) {
        const hidden = (row.querySelector('.invoice-name-hidden')?.value || '').trim();
        const typed = (row.querySelector('.product-name')?.value || '').trim();
        return hidden || typed;
    }

    function preserveInvoiceName(row, name) {
        const h = row.querySelector('.invoice-name-hidden');
        if (h && name) h.value = name;
        const vis = row.querySelector('.product-name');
        if (vis && name) vis.value = name;
    }

    function esc(s) {
        return String(s || '').replace(/[&<>"']/g, function (c) {
            return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
        });
    }

    function normName(s) {
        return String(s || '').trim().toLowerCase();
    }

    function isExactCatalogMatch(cand, invoiceName) {
        if (!cand || !cand.product) return false;
        if (cand.match_type === 'name' && (cand.score || 0) >= 100) return true;
        const inv = normName(invoiceName);
        const cat = normName(cand.product.name);
        return !!(inv && cat && inv === cat);
    }

    function shouldAutoBind(top, invoiceName) {
        if (!top || !top.product) return false;
        if (top.auto_bind) return true;
        return isExactCatalogMatch(top, invoiceName);
    }

    function rowHasProduct(row) {
        return (parseInt(row.querySelector('.p-id')?.value, 10) || 0) > 0;
    }

    /** Gợi ý gõ tay: tên trùng hẳn danh mục → gán luôn, không mở hộp gợi ý. */
    function tryExactFromManageList(row, searchVal, products, setProductToRow) {
        if (!row || rowHasProduct(row) || !Array.isArray(products) || !products.length) return false;
        const search = normName(searchVal);
        if (!search) return false;
        const exact = products.find(function (p) {
            return normName(p.name) === search;
        });
        if (!exact || !setProductToRow) return false;
        const inv = invoiceNameOf(row) || searchVal;
        preserveInvoiceName(row, inv);
        setProductToRow(row, exact, 'exact', row.querySelector('.invoice-unit')?.value || '');
        preserveInvoiceName(row, inv);
        onProductChosen(row, exact);
        return true;
    }

    async function matchCandidates(payload) {
        const res = await fetch('/api/products/match-candidates', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload || {}),
        });
        return res.json().catch(function () { return { success: false, candidates: [] }; });
    }

    async function linkAlias(payload) {
        try {
            await fetch('/api/products/link-alias', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload || {}),
            });
        } catch (e) { /* lưu lại khi ghi phiếu */ }
    }

    function hideHint(row) {
        const el = row.querySelector('.match-hint');
        if (el) { el.remove(); }
    }

    function markCreateNew(row) {
        row.dataset.createNew = '1';
        row.dataset.matchPending = '';
        row[CAND_KEY] = null;
        hideHint(row);
        const btn = row.querySelector('.match-open-btn');
        if (btn) btn.remove();
        row.classList.remove('row-suggested', 'row-exact-match');
        row.classList.add('row-not-found');
    }

    function setLinkButton(row) {
        ensureUi();
        hideHint(row);
        const input = row.querySelector('.product-name');
        const cell = input && input.closest('td');
        if (!cell) return;
        let btn = row.querySelector('.match-open-btn');
        if (!btn) {
            btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-outline-warning btn-sm match-open-btn ms-1';
            btn.textContent = 'Liên kết';
            btn.addEventListener('click', function (ev) {
                ev.preventDefault();
                ev.stopPropagation();
                openModal([row]);
            });
            input.insertAdjacentElement('afterend', btn);
        }
        btn.classList.remove('d-none');
    }

    function applyLink(row, product, unit) {
        if (!product || !hooksRef.setProductToRow) return;
        const inv = invoiceNameOf(row);
        hooksRef.setProductToRow(row, product, 'exact', unit || row.querySelector('.invoice-unit')?.value || '');
        preserveInvoiceName(row, inv);
        row.dataset.catalogName = product.name || '';
        row.dataset.matchPending = '';
        row.dataset.createNew = '';
        row[CAND_KEY] = null;
        hideHint(row);
        const btn = row.querySelector('.match-open-btn');
        if (btn) btn.remove();
        linkAlias({
            product_id: product.id,
            invoice_name: inv,
            supplier_id: supplierId(),
            barcode: (row.querySelector('.line-barcode')?.value || '').trim(),
        });
    }

    function pendingRows() {
        return Array.prototype.filter.call(
            document.querySelectorAll('#itemsTable tbody tr'),
            function (tr) {
                if (tr.dataset.createNew === '1') return false;
                if (typeof isServiceLineType === 'function'
                    && isServiceLineType(tr.querySelector('.line-type-select')?.value)) {
                    return false;
                }
                const pid = parseInt(tr.querySelector('.p-id')?.value, 10) || 0;
                return !pid && tr.dataset.matchPending === '1';
            }
        );
    }

    function hideModal() {
        const el = document.getElementById('ketoLinkModal');
        if (el) el.classList.add('d-none');
    }

    function openModal(rows) {
        ensureUi();
        const list = (rows && rows.length) ? rows : pendingRows();
        const box = document.getElementById('ketoLinkModal');
        const tb = box.querySelector('tbody');
        if (!list.length) {
            hideModal();
            return;
        }
        tb.innerHTML = list.map(function (row, idx) {
            const inv = invoiceNameOf(row);
            const qty = row.querySelector('.qty')?.value || '';
            const cands = row[CAND_KEY] || [];
            const opts = ['<option value="">— Chọn hàng trong kho —</option>'].concat(
                cands.map(function (c) {
                    const p = c.product || {};
                    const why = (c.reasons || []).join(', ');
                    return '<option value="' + esc(p.id) + '">'
                        + esc((p.product_code ? p.product_code + ' — ' : '') + p.name)
                        + (why ? ' (' + esc(why) + ')' : '')
                        + '</option>';
                })
            );
            if (cands[0] && cands[0].product) {
                opts[1] = opts[1].replace('<option ', '<option selected ');
            }
            return '<tr data-row-idx="' + idx + '">'
                + '<td><b>' + esc(inv) + '</b></td>'
                + '<td class="text-end">' + esc(qty) + '</td>'
                + '<td><select class="form-select form-select-sm keto-cand-sel">' + opts.join('') + '</select></td>'
                + '<td><button type="button" class="btn btn-sm btn-outline-secondary keto-new-one">Hàng mới</button></td>'
                + '</tr>';
        }).join('');
        tb.querySelectorAll('.keto-new-one').forEach(function (btn, i) {
            btn.addEventListener('click', function () {
                markCreateNew(list[i]);
                btn.closest('tr').remove();
                if (!tb.querySelector('tr')) hideModal();
            });
        });
        box._rows = list;
        box.classList.remove('d-none');
    }

    function applyModal() {
        const box = document.getElementById('ketoLinkModal');
        const list = box._rows || [];
        box.querySelectorAll('tbody tr').forEach(function (tr) {
            const idx = parseInt(tr.getAttribute('data-row-idx'), 10);
            const row = list[idx];
            if (!row) return;
            const sel = tr.querySelector('.keto-cand-sel');
            const pid = sel && sel.value;
            if (!pid) return;
            const cands = row[CAND_KEY] || [];
            const cand = cands.find(function (c) { return String((c.product || {}).id) === String(pid); });
            if (cand && cand.product) applyLink(row, cand.product);
        });
        hideModal();
    }

    async function applyToRow(row, opts) {
        opts = opts || {};
        if (!row) return;
        if (opts.setProductToRow) hooksRef.setProductToRow = opts.setProductToRow;
        if (typeof isServiceLineType === 'function'
            && isServiceLineType(row.querySelector('.line-type-select')?.value)) {
            return;
        }
        const name = (opts.name || row.querySelector('.product-name')?.value || '').trim();
        const barcode = (opts.barcode || row.querySelector('.line-barcode')?.value || '').trim();
        const unit = opts.unit || row.querySelector('.invoice-unit')?.value || '';
        if (!name && !barcode) return;
        preserveInvoiceName(row, name);
        try {
            const json = await matchCandidates({
                invoice_name: name,
                supplier_id: supplierId(),
                barcode: barcode,
                limit: 5,
            });
            const cands = json.candidates || [];
            const top = cands[0];
            if (shouldAutoBind(top, name)) {
                applyLink(row, top.product, unit);
                return;
            }
            const pid = parseInt(row.querySelector('.p-id')?.value, 10) || 0;
            if (cands.length && !pid) {
                row[CAND_KEY] = cands;
                row.dataset.matchPending = '1';
                row.dataset.matchScore = String((top && top.score) || 0);
                row.classList.add('row-suggested');
                setLinkButton(row);
                return;
            }
            row.dataset.matchPending = '';
            row[CAND_KEY] = null;
        } catch (e) {
            console.warn('[Khớp hàng]', e);
        }
    }

    async function scanTable(hooks) {
        if (hooks && hooks.setProductToRow) hooksRef.setProductToRow = hooks.setProductToRow;
        ensureUi();
        const rows = Array.prototype.slice.call(document.querySelectorAll('#itemsTable tbody tr'));
        for (let i = 0; i < rows.length; i++) {
            const row = rows[i];
            const pid = parseInt(row.querySelector('.p-id')?.value, 10) || 0;
            if (pid) continue;
            const name = invoiceNameOf(row);
            if (!name) continue;
            await applyToRow(row, { name: name, setProductToRow: hooksRef.setProductToRow });
        }
        const pending = pendingRows();
        if (pending.length) openModal(pending);
        return pending.length;
    }

    function bindRow(row, hooks) {
        if (!row || row.dataset.matchBound === '1') return;
        row.dataset.matchBound = '1';
        if (hooks && hooks.setProductToRow) hooksRef.setProductToRow = hooks.setProductToRow;
        const input = row.querySelector('.product-name');
        if (!input) return;
        input.addEventListener('blur', function () {
            const pid = parseInt(row.querySelector('.p-id')?.value, 10) || 0;
            if (pid || row.dataset.createNew === '1') return;
            const name = input.value.trim();
            if (name.length < 2) return;
            applyToRow(row, { name: name, setProductToRow: hooksRef.setProductToRow });
        });
        input.addEventListener('input', function () {
            if (row.dataset.createNew === '1') row.dataset.createNew = '';
        });
    }

    function onProductChosen(row, product) {
        if (!row || !product || !product.id) return;
        const inv = (row.querySelector('.invoice-name-hidden')?.value || '').trim();
        if (inv) preserveInvoiceName(row, inv);
        row.dataset.catalogName = product.name || '';
        row.dataset.matchPending = '';
        row.dataset.createNew = '';
        row[CAND_KEY] = null;
        hideHint(row);
        const btn = row.querySelector('.match-open-btn');
        if (btn) btn.remove();
        if (inv) {
            linkAlias({
                product_id: product.id,
                invoice_name: inv,
                supplier_id: supplierId(),
                barcode: (row.querySelector('.line-barcode')?.value || '').trim(),
            });
        }
    }

    async function confirmBeforeSave() {
        const rows = pendingRows();
        if (!rows.length) return true;
        openModal(rows);
        return false;
    }

    global.KetoImportMatch = {
        applyToRow: applyToRow,
        scanTable: scanTable,
        bindRow: bindRow,
        onProductChosen: onProductChosen,
        confirmBeforeSave: confirmBeforeSave,
        matchCandidates: matchCandidates,
        linkAlias: linkAlias,
        openModal: function () { openModal(pendingRows()); },
        tryExactFromManageList: tryExactFromManageList,
        isExactCatalogMatch: isExactCatalogMatch,
    };
})(window);
