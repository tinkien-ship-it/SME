/** Quét / gõ mã vạch NSX trên dòng phiếu nhập. */
(function (global) {
    function toastMsg(msg, type) {
        if (typeof toast === 'function') return toast(msg, type === 'error' ? 'error' : 'success');
        if (global.Swal) {
            Swal.fire({ toast: true, icon: type === 'error' ? 'error' : 'success', title: msg, timer: 2200, showConfirmButton: false });
        }
    }

    async function lookup(code) {
        const res = await fetch('/api/products/lookup-scan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ barcode: String(code || '').trim() }),
        });
        return res.json();
    }

    function fillBarcodeInput(row, product, scanned) {
        const el = row.querySelector('.line-barcode');
        if (!el) return;
        if (product && product.matched_wholesale && product.barcode1) {
            el.value = product.barcode1;
        } else if (product && product.barcode) {
            el.value = product.barcode;
        } else if (scanned) {
            el.value = scanned;
        }
    }

    async function applyToRow(row, code, opts) {
        const scanned = String(code || '').trim();
        if (!scanned) return;
        try {
            const data = await lookup(scanned);
            if (!data.success) throw new Error(data.error || 'Không tra cứu được mã');
            if (data.found && data.product && typeof opts.setProductToRow === 'function') {
                opts.setProductToRow(row, data.product, 'exact', opts.invoiceUnit || '');
                data.product.matched_wholesale = data.product.matched_wholesale;
                fillBarcodeInput(row, Object.assign({}, data.product, {
                    matched_wholesale: data.product.matched_wholesale,
                }), scanned);
                toastMsg('Đã khớp sản phẩm theo mã vạch', 'success');
                const qty = row.querySelector('.qty');
                if (qty) qty.focus();
            } else {
                fillBarcodeInput(row, null, scanned);
                if (typeof opts.onUnmatched === 'function') opts.onUnmatched(scanned);
                toastMsg('Mã mới — sẽ gắn khi lưu phiếu (nếu hàng chưa có trong danh mục)', 'success');
                const nameEl = row.querySelector('.product-name');
                if (nameEl && !nameEl.value.trim()) nameEl.focus();
            }
        } catch (err) {
            toastMsg(err.message || 'Lỗi quét mã', 'error');
        }
    }

    function ensureScannerModal() {
        let modal = document.getElementById('importBarcodeScannerModal');
        if (modal) return modal;
        modal = document.createElement('div');
        modal.id = 'importBarcodeScannerModal';
        modal.className = 'modal fade';
        modal.innerHTML = `
            <div class="modal-dialog modal-dialog-centered">
                <div class="modal-content">
                    <div class="modal-header py-2">
                        <h6 class="modal-title mb-0">Quét mã vạch trên hàng</h6>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body"><div id="importBarcodeReader"></div></div>
                </div>
            </div>`;
        document.body.appendChild(modal);
        return modal;
    }

    let html5Qr = null;
    let scanTarget = null;

    function loadHtml5Qrcode() {
        if (global.Html5Qrcode) return Promise.resolve();
        return new Promise((resolve, reject) => {
            const s = document.createElement('script');
            s.src = 'https://unpkg.com/html5-qrcode';
            s.onload = resolve;
            s.onerror = () => reject(new Error('Không tải được thư viện camera'));
            document.head.appendChild(s);
        });
    }

    async function openCamera(row, opts) {
        scanTarget = { row, opts };
        try {
            await loadHtml5Qrcode();
        } catch (e) {
            toastMsg(e.message, 'error');
            return;
        }
        const modalEl = ensureScannerModal();
        const bsModal = bootstrap.Modal.getOrCreateInstance(modalEl);
        bsModal.show();
        modalEl.addEventListener('hidden.bs.modal', stopCamera, { once: true });
        const readerId = 'importBarcodeReader';
        html5Qr = new Html5Qrcode(readerId);
        html5Qr.start(
            { facingMode: 'environment' },
            { fps: 10, qrbox: { width: 260, height: 160 } },
            async (decoded) => {
                stopCamera();
                bsModal.hide();
                if (scanTarget) await applyToRow(scanTarget.row, decoded, scanTarget.opts);
            },
            () => {},
        ).catch((err) => toastMsg(err.message || 'Không mở được camera', 'error'));
    }

    function stopCamera() {
        if (html5Qr) {
            html5Qr.stop().catch(() => {});
            html5Qr.clear().catch(() => {});
            html5Qr = null;
        }
    }

    function bindLine(row, opts) {
        const input = row.querySelector('.line-barcode');
        if (!input || input.dataset.ketoBcBound) return;
        input.dataset.ketoBcBound = '1';
        input.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter') return;
            e.preventDefault();
            applyToRow(row, input.value, opts || {});
        });
        const btn = row.querySelector('.btn-scan-line');
        if (btn) {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                openCamera(row, opts || {});
            });
        }
    }

    function barcodeCellHtml(value) {
        const v = (value || '').replace(/"/g, '&quot;');
        return `<div class="input-group input-group-sm">
            <input type="text" class="line-barcode form-control form-control-sm border-0"
                   placeholder="Quét tem NSX..." value="${v}" autocomplete="off">
            <button type="button" class="btn btn-outline-secondary btn-scan-line px-2" title="Camera quét mã">
                <i class="bi bi-upc-scan"></i>
            </button>
        </div>`;
    }

    global.KetoImportBarcode = {
        lookup, applyToRow, bindLine, barcodeCellHtml, openCamera,
    };
})(window);
