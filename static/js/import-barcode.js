/** Quét / gõ mã vạch NSX — lẻ (tem hàng) và sỉ (tem thùng). */
(function (global) {
    function toastMsg(msg, type) {
        if (typeof toast === 'function') return toast(msg, type === 'error' ? 'error' : 'success');
        if (global.Swal) {
            Swal.fire({ toast: true, icon: type === 'error' ? 'error' : 'success', title: msg, timer: 2600, showConfirmButton: false });
        }
    }

    function isWholesale(opts) {
        return (opts || {}).barcodeRole === 'wholesale';
    }

    function barcodeInput(row, opts) {
        return row.querySelector(isWholesale(opts) ? '.line-barcode1' : '.line-barcode');
    }

    async function lookup(code) {
        const res = await fetch('/api/products/lookup-scan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ barcode: String(code || '').trim() }),
        });
        return res.json();
    }

    function fillBarcodeInput(row, product, scanned, opts) {
        opts = opts || {};
        const wholesale = isWholesale(opts);
        const el = barcodeInput(row, opts);
        if (!el) return;
        if (opts.preferScanned && scanned) {
            el.value = scanned;
            return;
        }
        if (wholesale) {
            el.value = (product && (product.barcode1 || scanned)) || scanned || '';
            return;
        }
        if (product && product.matched_wholesale && product.barcode1) {
            el.value = product.barcode1;
        } else if (product && product.barcode) {
            el.value = product.barcode;
        } else if (scanned) {
            el.value = scanned;
        }
    }

    async function attachBarcode(productId, payload) {
        const body = typeof payload === 'string' ? { barcode: payload } : (payload || {});
        const res = await fetch('/api/products/' + encodeURIComponent(productId) + '/attach-barcode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.success) {
            throw new Error(data.error || 'Không lưu được mã vạch');
        }
        return data;
    }

    async function applyToRow(row, code, opts) {
        opts = opts || {};
        const scanned = String(code || '').trim();
        if (!scanned) return;
        const wholesale = isWholesale(opts);
        const pid = parseInt(row.querySelector('.p-id')?.value, 10) || 0;
        try {
            const data = await lookup(scanned);
            if (!data.success) throw new Error(data.error || 'Không tra cứu được mã');

            if (pid) {
                if (data.found && data.product && Number(data.product.id) !== pid) {
                    toastMsg(
                        'Mã "' + scanned + '" đã gắn sản phẩm khác: ' + (data.product.name || data.product.id),
                        'error'
                    );
                    return;
                }
                fillBarcodeInput(row, null, scanned, Object.assign({}, opts, { preferScanned: true }));
                const saved = await attachBarcode(pid, wholesale ? { barcode1: scanned } : { barcode: scanned });
                const stored = wholesale
                    ? ((saved && saved.barcode1) || scanned)
                    : ((saved && saved.barcode) || scanned);
                fillBarcodeInput(row, null, stored, Object.assign({}, opts, { preferScanned: true }));
                toastMsg(
                    wholesale
                        ? ('Đã gắn mã vạch sỉ (thùng): ' + stored)
                        : (stored && stored !== scanned
                            ? ('Đã gắn mã ' + stored + ' (rút từ QR)')
                            : 'Đã gắn mã vạch / QR vào sản phẩm'),
                    'success'
                );
                return;
            }

            fillBarcodeInput(row, data.found ? data.product : null, scanned, Object.assign({}, opts, {
                preferScanned: !!opts.preferScanned || !data.found,
            }));

            if (data.found && data.product && typeof opts.setProductToRow === 'function') {
                opts.setProductToRow(row, data.product, 'exact', opts.invoiceUnit || '');
                fillBarcodeInput(row, data.product, scanned, Object.assign({}, opts, {
                    preferScanned: !!opts.preferScanned || wholesale,
                }));
                toastMsg(
                    wholesale ? 'Đã khớp sản phẩm theo mã vạch sỉ (thùng)' : 'Đã khớp sản phẩm theo mã vạch',
                    'success'
                );
                const qty = row.querySelector('.qty');
                if (qty) qty.focus();
            } else {
                if (typeof opts.onUnmatched === 'function') opts.onUnmatched(scanned);
                toastMsg(
                    wholesale
                        ? 'Đã ghi mã sỉ — lưu phiếu để gắn tem thùng'
                        : 'Đã ghi mã — lưu phiếu để gắn vào sản phẩm mới',
                    'success'
                );
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
                        <h6 class="modal-title mb-0" id="importBarcodeScannerTitle">Quét mã vạch trên hàng</h6>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">
                        <div id="importBarcodeReader" style="min-height:240px"></div>
                        <p class="small text-muted mt-2 mb-0">Đưa tem QR hoặc mã vạch vào khung vuông. Giữ máy cách tem ~10–20 cm.</p>
                    </div>
                </div>
            </div>`;
        document.body.appendChild(modal);
        return modal;
    }

    let html5Qr = null;
    let scanTarget = null;
    let startingCamera = false;

    function loadHtml5Qrcode() {
        if (global.Html5Qrcode) return Promise.resolve();
        return new Promise((resolve, reject) => {
            const s = document.createElement('script');
            s.src = 'https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js';
            s.onload = resolve;
            s.onerror = () => reject(new Error('Không tải được thư viện camera'));
            document.head.appendChild(s);
        });
    }

    function scannerFormats() {
        const F = global.Html5QrcodeSupportedFormats;
        if (!F) return null;
        return [
            F.QR_CODE, F.DATA_MATRIX,
            F.EAN_13, F.EAN_8, F.UPC_A, F.UPC_E,
            F.CODE_128, F.CODE_39, F.ITF, F.CODABAR,
        ];
    }

    async function stopCamera() {
        if (!html5Qr) return;
        const inst = html5Qr;
        html5Qr = null;
        try { await inst.stop(); } catch (e) { /* ignore */ }
        try { await inst.clear(); } catch (e) { /* ignore */ }
    }

    async function startScanner(bsModal) {
        if (startingCamera) return;
        startingCamera = true;
        const qrbox = (viewW, viewH) => {
            const size = Math.max(200, Math.floor(Math.min(viewW, viewH) * 0.72));
            return { width: size, height: size };
        };
        const onDecoded = async (decoded) => {
            const target = scanTarget;
            await stopCamera();
            bsModal.hide();
            if (target) await applyToRow(target.row, decoded, target.opts);
        };
        async function tryStart(withFormats) {
            await stopCamera();
            const host = document.getElementById('importBarcodeReader');
            if (host) host.innerHTML = '';
            const ctorOpts = {
                verbose: false,
                experimentalFeatures: { useBarCodeDetectorIfSupported: true },
            };
            const formats = withFormats ? scannerFormats() : null;
            if (formats) ctorOpts.formatsToSupport = formats;
            html5Qr = new Html5Qrcode('importBarcodeReader', ctorOpts);
            await html5Qr.start(
                { facingMode: 'environment' },
                { fps: 12, qrbox: qrbox, aspectRatio: 1.777778, disableFlip: false },
                onDecoded,
                () => {},
            );
        }
        try {
            try {
                await tryStart(true);
            } catch (firstErr) {
                await tryStart(false);
            }
        } catch (err) {
            toastMsg(err.message || 'Không mở được camera. Cần HTTPS hoặc cho phép quyền camera.', 'error');
        } finally {
            startingCamera = false;
        }
    }

    async function openCamera(row, opts) {
        scanTarget = { row, opts: opts || {} };
        try {
            await loadHtml5Qrcode();
        } catch (e) {
            toastMsg(e.message, 'error');
            return;
        }
        if (!global.bootstrap || !bootstrap.Modal) {
            toastMsg('Thiếu Bootstrap Modal — không mở được camera', 'error');
            return;
        }
        const modalEl = ensureScannerModal();
        const title = modalEl.querySelector('#importBarcodeScannerTitle');
        if (title) {
            title.textContent = isWholesale(opts)
                ? 'Quét mã vạch sỉ (tem thùng)'
                : 'Quét mã vạch lẻ trên hàng';
        }
        const reader = modalEl.querySelector('#importBarcodeReader');
        if (reader) reader.innerHTML = '';
        const bsModal = bootstrap.Modal.getOrCreateInstance(modalEl);
        modalEl.addEventListener('shown.bs.modal', () => startScanner(bsModal), { once: true });
        modalEl.addEventListener('hidden.bs.modal', () => { stopCamera(); }, { once: true });
        bsModal.show();
    }

    function bindOne(row, selector, btnSelector, role, opts) {
        const input = row.querySelector(selector);
        if (!input || input.dataset.ketoBcBound) return;
        input.dataset.ketoBcBound = '1';
        const lineOpts = () => Object.assign({}, opts || {}, { barcodeRole: role });
        input.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter') return;
            e.preventDefault();
            applyToRow(row, input.value, lineOpts());
        });
        const btn = row.querySelector(btnSelector);
        if (btn) {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                openCamera(row, lineOpts());
            });
        }
    }

    function bindLine(row, opts) {
        bindOne(row, '.line-barcode', '.btn-scan-line', 'retail', opts);
        bindOne(row, '.line-barcode1', '.btn-scan-line1', 'wholesale', opts);
    }

    function barcodeCellHtml(value, kind) {
        const wholesale = kind === 'wholesale' || kind === 'barcode1';
        const v = String(value || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
        const cls = wholesale ? 'line-barcode1' : 'line-barcode';
        const btn = wholesale ? 'btn-scan-line1' : 'btn-scan-line';
        const ph = wholesale ? 'Tem thùng / sỉ...' : 'Tem lẻ NSX...';
        return `<div class="input-group input-group-sm">
            <input type="text" class="${cls} form-control form-control-sm border-0"
                   placeholder="${ph}" value="${v}" autocomplete="off">
            <button type="button" class="btn btn-outline-secondary ${btn} px-2" title="${wholesale ? 'Camera quét tem thùng' : 'Camera quét mã lẻ'}">
                <i class="bi bi-upc-scan"></i>
            </button>
        </div>`;
    }

    global.KetoImportBarcode = {
        lookup, applyToRow, bindLine, barcodeCellHtml, openCamera, attachBarcode,
    };
})(window);
