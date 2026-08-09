/** Quét / gõ mã vạch NSX trên dòng phiếu nhập. */
(function (global) {
    function toastMsg(msg, type) {
        if (typeof toast === 'function') return toast(msg, type === 'error' ? 'error' : 'success');
        if (global.Swal) {
            Swal.fire({ toast: true, icon: type === 'error' ? 'error' : 'success', title: msg, timer: 2600, showConfirmButton: false });
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

    function fillBarcodeInput(row, product, scanned, preferScanned) {
        const el = row.querySelector('.line-barcode');
        if (!el) return;
        if (preferScanned && scanned) {
            el.value = scanned;
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

    async function attachBarcode(productId, scanned) {
        const res = await fetch('/api/products/' + encodeURIComponent(productId) + '/attach-barcode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ barcode: scanned }),
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
        const pid = parseInt(row.querySelector('.p-id')?.value, 10) || 0;
        try {
            const data = await lookup(scanned);
            if (!data.success) throw new Error(data.error || 'Không tra cứu được mã');

            // Sửa phiếu: dòng đã có SP — gắn tem vào đúng SP đó, không đổi sang SP khác.
            if (pid) {
                if (data.found && data.product && Number(data.product.id) !== pid) {
                    toastMsg(
                        'Mã "' + scanned + '" đã gắn sản phẩm khác: ' + (data.product.name || data.product.id),
                        'error'
                    );
                    return;
                }
                fillBarcodeInput(row, null, scanned, true);
                await attachBarcode(pid, scanned);
                toastMsg('Đã gắn mã vạch vào sản phẩm', 'success');
                return;
            }

            fillBarcodeInput(row, data.found ? Object.assign({}, data.product, {
                matched_wholesale: data.product && data.product.matched_wholesale,
            }) : null, scanned, !!opts.preferScanned || !data.found);

            if (data.found && data.product && typeof opts.setProductToRow === 'function') {
                opts.setProductToRow(row, data.product, 'exact', opts.invoiceUnit || '');
                fillBarcodeInput(row, Object.assign({}, data.product, {
                    matched_wholesale: data.product.matched_wholesale,
                }), scanned, !!opts.preferScanned);
                toastMsg('Đã khớp sản phẩm theo mã vạch', 'success');
                const qty = row.querySelector('.qty');
                if (qty) qty.focus();
            } else {
                if (typeof opts.onUnmatched === 'function') opts.onUnmatched(scanned);
                toastMsg('Đã ghi mã — lưu phiếu để gắn vào sản phẩm mới', 'success');
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
                    <div class="modal-body">
                        <div id="importBarcodeReader" style="min-height:240px"></div>
                        <p class="small text-muted mt-2 mb-0">Đưa tem EAN / UPC / Code128 vào khung ngang. Giữ máy cách tem ~10–20 cm.</p>
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
            F.EAN_13, F.EAN_8, F.UPC_A, F.UPC_E,
            F.CODE_128, F.CODE_39, F.ITF, F.CODABAR, F.QR_CODE,
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
        const qrbox = (viewW, viewH) => ({
            width: Math.max(220, Math.floor(Math.min(viewW * 0.92, 400))),
            height: Math.max(90, Math.floor(Math.min(viewH * 0.36, 150))),
        });
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
        const reader = modalEl.querySelector('#importBarcodeReader');
        if (reader) reader.innerHTML = '';
        const bsModal = bootstrap.Modal.getOrCreateInstance(modalEl);
        modalEl.addEventListener('shown.bs.modal', () => startScanner(bsModal), { once: true });
        modalEl.addEventListener('hidden.bs.modal', () => { stopCamera(); }, { once: true });
        bsModal.show();
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
        const v = String(value || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
        return `<div class="input-group input-group-sm">
            <input type="text" class="line-barcode form-control form-control-sm border-0"
                   placeholder="Quét tem NSX..." value="${v}" autocomplete="off" inputmode="numeric">
            <button type="button" class="btn btn-outline-secondary btn-scan-line px-2" title="Camera quét mã">
                <i class="bi bi-upc-scan"></i>
            </button>
        </div>`;
    }

    global.KetoImportBarcode = {
        lookup, applyToRow, bindLine, barcodeCellHtml, openCamera, attachBarcode,
    };
})(window);
