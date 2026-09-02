/* POS offline-first — IndexedDB catalog + outbox sync (HKD & SME). */
(function (global) {
    'use strict';

    const DB_NAME = 'keto_pos_offline_v1';
    const DB_VER = 1;
    const OUTBOX_KEY = 'keto_pos_outbox_backup';
    const SYNCED_RETENTION_DAYS = 7;
    const CATALOG_TTL_MS = 5 * 60 * 1000;

    let dbPromise = null;
    let online = typeof navigator !== 'undefined' ? navigator.onLine : true;
    let syncing = false;
    let tenantKey = 'default';
    let scaleConfig = { barcode_prefix: '2' };

    function uuid() {
        if (global.crypto && crypto.randomUUID) return crypto.randomUUID();
        return 'pos-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10);
    }

    function resolveTenantKey(opts) {
        opts = opts || {};
        if (opts.tenantKey) return String(opts.tenantKey);
        const meta = document.querySelector('meta[name="keto-tenant-id"]');
        if (meta && meta.content) return String(meta.content).trim();
        // Không lấy segment path (/sale) làm tenant — sẽ lệch khi đổi URL.
        return 'default';
    }

    async function probeServerReachable() {
        try {
            const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
            const timer = ctrl ? setTimeout(function () { ctrl.abort(); }, 4000) : null;
            const res = await fetch('/api/pos/catalog?probe=1', {
                method: 'GET',
                credentials: 'same-origin',
                cache: 'no-store',
                signal: ctrl ? ctrl.signal : undefined,
            });
            if (timer) clearTimeout(timer);
            // 401/403 vẫn nghĩa là có mạng (session hết hạn)
            return res.status > 0;
        } catch (e) {
            return false;
        }
    }

    function normalizeCheckoutPayload(payload, opts) {
        const p = Object.assign({}, payload || {});
        const forceNew = !opts || opts.forceNew !== false;
        if (forceNew) {
            // Đơn offline mới: bỏ order_id/sale_id cũ (draft/QR) để không gọi nhầm update.
            delete p.order_id;
            delete p.sale_id;
        }
        if (!p.client_uuid) p.client_uuid = uuid();
        if (!p.status) p.status = 'completed';
        return p;
    }

    function openDb() {
        if (dbPromise) return dbPromise;
        dbPromise = new Promise(function (resolve, reject) {
            if (!global.indexedDB) {
                reject(new Error('IndexedDB không khả dụng'));
                return;
            }
            const req = indexedDB.open(DB_NAME, DB_VER);
            req.onupgradeneeded = function (ev) {
                const db = ev.target.result;
                if (!db.objectStoreNames.contains('catalog')) {
                    const cat = db.createObjectStore('catalog', { keyPath: 'id' });
                    cat.createIndex('name', 'name', { unique: false });
                    cat.createIndex('barcode', 'barcode', { unique: false });
                    cat.createIndex('barcode1', 'barcode1', { unique: false });
                    cat.createIndex('product_code', 'product_code', { unique: false });
                    cat.createIndex('weight_plu', 'weight_plu', { unique: false });
                }
                if (!db.objectStoreNames.contains('menu')) {
                    db.createObjectStore('menu', { keyPath: 'id' });
                }
                if (!db.objectStoreNames.contains('outbox')) {
                    const ob = db.createObjectStore('outbox', { keyPath: 'client_uuid' });
                    ob.createIndex('status', 'status', { unique: false });
                    ob.createIndex('created_at', 'created_at', { unique: false });
                }
                if (!db.objectStoreNames.contains('meta')) {
                    db.createObjectStore('meta', { keyPath: 'key' });
                }
            };
            req.onsuccess = function () { resolve(req.result); };
            req.onerror = function () { reject(req.error); };
        });
        return dbPromise;
    }

    function idbTx(store, mode) {
        return openDb().then(function (db) {
            return db.transaction(store, mode).objectStore(store);
        });
    }

    function idbPut(store, value) {
        return idbTx(store, 'readwrite').then(function (os) {
            return new Promise(function (resolve, reject) {
                const r = os.put(value);
                r.onsuccess = function () { resolve(value); };
                r.onerror = function () { reject(r.error); };
            });
        });
    }

    function idbGet(store, key) {
        return idbTx(store, 'readonly').then(function (os) {
            return new Promise(function (resolve, reject) {
                const r = os.get(key);
                r.onsuccess = function () { resolve(r.result); };
                r.onerror = function () { reject(r.error); };
            });
        });
    }

    function idbGetAll(store) {
        return idbTx(store, 'readonly').then(function (os) {
            return new Promise(function (resolve, reject) {
                const r = os.getAll();
                r.onsuccess = function () { resolve(r.result || []); };
                r.onerror = function () { reject(r.error); };
            });
        });
    }

    function idbDelete(store, key) {
        return idbTx(store, 'readwrite').then(function (os) {
            return new Promise(function (resolve, reject) {
                const r = os.delete(key);
                r.onsuccess = function () { resolve(); };
                r.onerror = function () { reject(r.error); };
            });
        });
    }

    function fold(s) {
        const mapFrom = 'àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ'
            + 'ÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ';
        const mapTo = 'aaaaaaaaaaaaaaaaaeeeeeeeeeeeiiiiiooooooooooooooooouuuuuuuuuuuyyyyyd'
            + 'aaaaaaaaaaaaaaaaaeeeeeeeeeeeiiiiiooooooooooooooooouuuuuuuuuuuyyyyyd';
        let t = String(s || '');
        t = t.split('').map(function (ch) {
            const i = mapFrom.indexOf(ch);
            return i >= 0 ? mapTo[i] : ch;
        }).join('').toLowerCase();
        return t.replace(/[^a-z0-9]+/g, ' ').trim();
    }

    function digitsOnly(code) {
        return String(code || '').replace(/\D/g, '');
    }

    function parseWeightBarcode(barcode, prefix) {
        const code = digitsOnly(barcode);
        if (code.length !== 13 || code[0] !== '2') return null;
        const pfx = String(prefix || '2').trim();
        if (pfx && code[0] !== pfx[0]) return null;
        const plu = code.slice(1, 6);
        const weightRaw = code.slice(6, 11);
        const grams = parseInt(weightRaw, 10);
        if (!grams || grams <= 0) return null;
        return {
            barcode: code,
            plu: plu,
            weight_kg: Math.round(grams / 1000 * 10000) / 10000,
            weight_grams: grams,
        };
    }

    async function lookupWeightProductLocal(plu) {
        const key = String(plu || '').trim();
        if (!key) return null;
        const all = await idbGetAll('catalog');
        const stripped = key.replace(/^0+/, '') || key;
        for (let i = 0; i < all.length; i++) {
            const p = all[i];
            const wplu = String(p.weight_plu || '').trim();
            if (wplu && (wplu === key || wplu === stripped || wplu.replace(/^0+/, '') === stripped)) {
                return p;
            }
        }
        for (let j = 0; j < all.length; j++) {
            const p2 = all[j];
            if (parseInt(p2.sell_by_weight, 10) !== 1) continue;
            const bc = String(p2.barcode || '');
            const pc = String(p2.product_code || '');
            if (bc.includes(key) || pc.includes(key)) return p2;
        }
        return null;
    }

    function buildWeightScanResult(product, parsed) {
        const weightKg = parsed.weight_kg;
        const price = parseFloat(product.base_price) || 0;
        const stock = parseFloat(product.quantity) || 0;
        return {
            success: true,
            source: 'scale_barcode',
            parsed: parsed,
            offline: true,
            data: {
                id: product.id,
                name: product.name,
                price: price,
                unit: product.unit || 'kg',
                useUnit1: false,
                maxQty: stock > 0 ? stock : 9999,
                qty: weightKg,
                sellByWeight: true,
                product_type: (product.product_type || 'goods').toLowerCase(),
            },
        };
    }

    async function resolveWeightScanOffline(barcode) {
        const parsed = parseWeightBarcode(barcode, scaleConfig.barcode_prefix || '2');
        if (!parsed) return null;
        const product = await lookupWeightProductLocal(parsed.plu);
        if (!product) {
            return {
                success: false,
                error: 'Không tìm thấy PLU ' + parsed.plu + ' trong cache offline',
                parsed: parsed,
                offline: true,
            };
        }
        return buildWeightScanResult(product, parsed);
    }

    function isOnline() {
        return online;
    }

    function setOnlineState(next) {
        const was = online;
        online = !!next;
        if (was !== online) {
            document.dispatchEvent(new CustomEvent('pos-offline-status', { detail: { online: online } }));
            if (online) {
                // Xác nhận thật sự tới được server rồi mới sync (navigator.onLine hay báo sai).
                probeServerReachable().then(function (ok) {
                    if (ok) processOutbox().catch(function () {});
                    else setOnlineState(false);
                });
            }
        }
        updateBadge();
    }

    function backupOutboxLocal(items) {
        try {
            localStorage.setItem(OUTBOX_KEY + ':' + tenantKey, JSON.stringify(items));
        } catch (e) { /* ignore quota */ }
    }

    async function loadOutboxFromBackup() {
        try {
            const raw = localStorage.getItem(OUTBOX_KEY + ':' + tenantKey);
            if (!raw) return;
            const items = JSON.parse(raw);
            if (!Array.isArray(items)) return;
            for (const item of items) {
                if (item && item.client_uuid) await idbPut('outbox', item);
            }
        } catch (e) { /* ignore */ }
    }

    async function purgeOldSyncedOutbox() {
        const cutoff = Date.now() - SYNCED_RETENTION_DAYS * 86400000;
        const all = await idbGetAll('outbox');
        for (let i = 0; i < all.length; i++) {
            const item = all[i];
            if (item.status !== 'synced') continue;
            const ts = Date.parse(item.synced_at || item.created_at || '');
            if (ts && ts < cutoff) {
                await idbDelete('outbox', item.client_uuid);
            }
        }
    }

    async function syncCatalog(opts) {
        opts = opts || {};
        if (!opts.force && isOnline()) {
            const meta = await getCatalogMeta();
            if (meta && meta.count > 0 && meta.updated_at) {
                const ts = Date.parse(meta.updated_at);
                if (!Number.isNaN(ts) && (Date.now() - ts) < CATALOG_TTL_MS) {
                    return { success: true, count: meta.count, cached: true };
                }
            }
        }
        if (!isOnline()) return { success: false, offline: true };
        const includeMenu = !!opts.includeMenu;
        try {
            const res = await fetch('/api/pos/catalog?include_menu=' + (includeMenu ? '1' : '0'), {
                credentials: 'same-origin',
            });
            const data = await res.json();
            if (!data.success) throw new Error(data.error || 'Không tải danh mục');
            for (const p of (data.products || [])) {
                await idbPut('catalog', p);
            }
            if (includeMenu) {
                for (const m of (data.menu || [])) {
                    await idbPut('menu', m);
                }
            }
            if (data.scale_config) {
                scaleConfig = data.scale_config;
                await idbPut('meta', { key: 'scale_config', tenant: tenantKey, config: scaleConfig });
            }
            await idbPut('meta', {
                key: 'catalog',
                updated_at: data.updated_at,
                count: data.count,
                tenant: tenantKey,
            });
            document.dispatchEvent(new CustomEvent('pos-catalog-synced', {
                detail: { count: data.count, updated_at: data.updated_at },
            }));
            return { success: true, count: data.count };
        } catch (e) {
            return { success: false, error: String(e.message || e) };
        }
    }

    async function getCatalogMeta() {
        const meta = await idbGet('meta', 'catalog');
        return meta || null;
    }

    async function loadCachedScaleConfig() {
        const row = await idbGet('meta', 'scale_config');
        if (row && row.config) scaleConfig = row.config;
    }

    async function searchProducts(q) {
        const needle = String(q || '').trim();
        if (!needle) return [];
        const all = await idbGetAll('catalog');
        if (!all.length) return [];
        const qf = fold(needle);
        const qLower = needle.toLowerCase();
        const scored = all.map(function (p) {
            const name = String(p.name || '');
            const code = String(p.product_code || '');
            const bc = String(p.barcode || '');
            const bc1 = String(p.barcode1 || '');
            let score = 0;
            if (name.toLowerCase() === qLower) score = 1000;
            else if (bc === needle || bc1 === needle || code.toLowerCase() === qLower) score = 950;
            else if (fold(name) === qf) score = 900;
            else if (name.toLowerCase().includes(qLower)) score = 700;
            else if (code.toLowerCase().includes(qLower)) score = 650;
            else if (bc.includes(needle) || bc1.includes(needle)) score = 600;
            else {
                const words = qf.split(/\s+/).filter(Boolean);
                words.forEach(function (w) {
                    if (fold(name).includes(w)) score += 120;
                });
            }
            return { p: p, score: score };
        }).filter(function (x) { return x.score > 0; });
        scored.sort(function (a, b) { return b.score - a.score; });
        return scored.slice(0, 50).map(function (x) { return x.p; });
    }

    async function lookupBarcodeLocal(barcode) {
        const code = String(barcode || '').trim();
        if (!code) return null;
        const all = await idbGetAll('catalog');
        for (let i = 0; i < all.length; i++) {
            const p = all[i];
            if (String(p.barcode || '') === code || String(p.barcode1 || '') === code) {
                return p;
            }
            if (String(p.product_code || '').toLowerCase() === code.toLowerCase()) {
                return p;
            }
        }
        return null;
    }

    function productToScanResult(p, barcode, isUnit1) {
        const productType = (p.product_type || 'goods').toLowerCase();
        const isService = productType === 'service';
        const stock = parseFloat(p.quantity) || 0;
        const ratio = parseFloat(p.unit_ratio) || 1;
        let maxQty = isService ? 999999 : Math.floor(stock);
        if (isUnit1 && !isService) {
            maxQty = ratio ? Math.floor(stock / ratio) : Math.floor(stock);
        }
        const useUnit1 = !!isUnit1;
        const price = useUnit1 ? (parseFloat(p.sale_price) || parseFloat(p.base_price) || 0)
            : (parseFloat(p.base_price) || 0);
        return {
            success: true,
            data: {
                id: p.id,
                name: p.name,
                unit: useUnit1 ? (p.unit1 || p.unit) : p.unit,
                price: price,
                useUnit1: useUnit1,
                ratio: ratio,
                sellByWeight: parseInt(p.sell_by_weight, 10) === 1,
                product_type: productType,
                maxQty: maxQty,
                barcode: p.barcode,
                barcode1: p.barcode1,
                product_code: p.product_code,
            },
            offline: true,
        };
    }

    async function scanBarcode(barcode) {
        const weightResult = await resolveWeightScanOffline(barcode);
        if (weightResult) return weightResult;

        const local = await lookupBarcodeLocal(barcode);
        if (local) {
            const isUnit1 = String(local.barcode1 || '') === String(barcode);
            return productToScanResult(local, barcode, isUnit1);
        }
        if (!isOnline()) {
            return { success: false, error: 'Không tìm thấy mã trong cache offline', offline: true };
        }
        const res = await fetch('/api/scan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ barcode: barcode }),
            credentials: 'same-origin',
        });
        return res.json();
    }

    async function enqueueOutbox(entry) {
        entry.status = entry.status || 'pending';
        entry.created_at = entry.created_at || new Date().toISOString();
        entry.attempts = entry.attempts || 0;
        entry.tenant = tenantKey;
        await idbPut('outbox', entry);
        const all = await idbGetAll('outbox');
        backupOutboxLocal(all.filter(function (x) {
            return x.tenant === tenantKey && (x.status === 'pending' || x.status === 'error');
        }));
        updateBadge();
        return entry;
    }

    async function getPendingOutbox() {
        const all = await idbGetAll('outbox');
        return all.filter(function (x) {
            const t = x.tenant || 'default';
            // Bản cũ gán tenant = 'sale' (lấy từ pathname) — vẫn đồng bộ.
            const tenantOk = t === tenantKey || t === 'sale' || t === 'default' || !x.tenant;
            return tenantOk && (x.status === 'pending' || x.status === 'error');
        }).sort(function (a, b) {
            return String(a.created_at).localeCompare(String(b.created_at));
        });
    }

    async function getPendingCount() {
        const pending = await getPendingOutbox();
        return pending.length;
    }

    async function removeOutboxItem(clientUuid) {
        await idbDelete('outbox', clientUuid);
        const all = await idbGetAll('outbox');
        backupOutboxLocal(all.filter(function (x) {
            return x.tenant === tenantKey && (x.status === 'pending' || x.status === 'error');
        }));
        updateBadge();
    }

    async function retryOutboxItem(clientUuid) {
        const item = await idbGet('outbox', clientUuid);
        if (!item) return { ok: false, error: 'Không tìm thấy' };
        item.status = 'pending';
        item.last_error = null;
        await idbPut('outbox', item);
        if (!isOnline()) return { ok: false, error: 'Đang offline' };
        return replayOutboxItem(item);
    }

    async function submitSale(opts) {
        opts = opts || {};
        const isUpdate = !!(opts.kind === 'pos_update' || (opts.url && String(opts.url).indexOf('/api/sale/update_item/') >= 0));
        const payload = normalizeCheckoutPayload(opts.payload || {}, { forceNew: !isUpdate });
        // Đơn mới luôn POST checkout — tránh PUT update_item khi đang offline.
        const url = isUpdate ? (opts.url || '/api/cart/checkout') : '/api/cart/checkout';
        const method = isUpdate ? (opts.method || 'PUT') : 'POST';

        const reachable = isOnline() && await probeServerReachable();
        if (reachable) {
            try {
                const res = await fetch(url, {
                    method: method,
                    headers: Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {}),
                    body: JSON.stringify(payload),
                    credentials: 'same-origin',
                    cache: 'no-store',
                });
                const data = await res.json().catch(function () { return {}; });
                if (res.ok && data.success) {
                    return Object.assign({ offline: false, queued: false }, data);
                }
                // Lỗi nghiệp vụ (4xx) — không xếp hàng đợi
                if (res.status >= 400 && res.status < 500) {
                    return Object.assign({
                        success: false,
                        offline: false,
                        queued: false,
                        error: data.error || data.message || ('HTTP ' + res.status),
                    }, data);
                }
            } catch (e) {
                /* fall through to queue */
            }
        }

        const entry = {
            client_uuid: payload.client_uuid,
            kind: isUpdate ? 'pos_update' : 'pos_checkout',
            url: url,
            method: method,
            payload: payload,
            status: 'pending',
            created_at: new Date().toISOString(),
            attempts: 0,
            label: opts.label || ('POS ' + (payload.total || '') + 'đ'),
        };
        await enqueueOutbox(entry);
        // Nếu mạng vừa về, thử sync ngay
        if (isOnline()) {
            setTimeout(function () { processOutbox().catch(function () {}); }, 300);
        }
        return {
            success: true,
            offline: true,
            queued: true,
            client_uuid: payload.client_uuid,
            message: 'Đã lưu offline — sẽ đồng bộ khi có mạng',
        };
    }

    async function handleSyncedSideEffects(item, data) {
        const payload = item.payload || {};
        const effects = { invoice: null, pending_qr: null };
        if (payload.auto_issue_invoice && data.sale_id) {
            try {
                const invRes = await fetch('/api/invoice/issue/' + data.sale_id, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ loai_hdon: payload.invoice_loai_hdon != null ? payload.invoice_loai_hdon : 1 }),
                    credentials: 'same-origin',
                    cache: 'no-store',
                });
                effects.invoice = await invRes.json().catch(function () { return null; });
            } catch (e) {
                effects.invoice = { success: false, error: String(e.message || e) };
            }
        }
        if (payload.status === 'pending' && data.sale_id) {
            effects.pending_qr = data.sale_id;
        }
        return effects;
    }

    function isRetryableSyncFailure(res, data, errMsg) {
        if (!res) return true;
        if (res.status === 0 || res.status >= 500) return true;
        if (res.status === 503) return true;
        const msg = String(errMsg || data.error || data.message || '').toLowerCase();
        if (msg.indexOf('locked') >= 0 || msg.indexOf('mạng') >= 0 || msg.indexOf('timeout') >= 0) return true;
        return false;
    }

    async function replayOutboxItem(item) {
        item.attempts = (item.attempts || 0) + 1;
        // Chuẩn hóa payload cũ đã xếp hàng (có thể còn order_id draft).
        if (item.kind !== 'pos_update') {
            item.url = '/api/cart/checkout';
            item.method = 'POST';
            item.payload = normalizeCheckoutPayload(item.payload || {}, { forceNew: true });
            item.payload.client_uuid = item.client_uuid || item.payload.client_uuid;
        }
        let res;
        try {
            res = await fetch(item.url, {
                method: item.method || 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(item.payload),
                credentials: 'same-origin',
                cache: 'no-store',
            });
        } catch (e) {
            item.status = 'pending';
            item.last_error = 'Mất mạng: ' + String(e.message || e);
            await idbPut('outbox', item);
            return { ok: false, retryable: true, error: item.last_error, item: item };
        }

        const rawText = await res.text().catch(function () { return ''; });
        let data = {};
        try { data = rawText ? JSON.parse(rawText) : {}; } catch (e) { data = {}; }

        if (res.ok && data.success) {
            item.status = 'synced';
            item.sale_id = data.sale_id;
            item.synced_at = new Date().toISOString();
            item.last_error = null;
            item.side_effects = await handleSyncedSideEffects(item, data);
            await idbPut('outbox', item);
            return { ok: true, data: data, item: item };
        }

        // Redirect login / HTML → phiên hết hạn
        if (res.status === 401 || res.status === 403 ||
            (rawText && rawText.indexOf('<html') >= 0) ||
            (res.redirected && res.url && res.url.indexOf('/login') >= 0)) {
            item.status = 'error';
            item.last_error = 'Phiên đăng nhập hết hạn — đăng nhập lại rồi bấm Đồng bộ';
            await idbPut('outbox', item);
            return { ok: false, retryable: false, error: item.last_error, data: data, item: item };
        }

        const errMsg = data.error || data.message || ('HTTP ' + res.status);
        if (isRetryableSyncFailure(res, data, errMsg)) {
            item.status = 'pending';
            item.last_error = errMsg;
            await idbPut('outbox', item);
            return { ok: false, retryable: true, error: errMsg, data: data, item: item };
        }

        item.status = 'error';
        item.last_error = errMsg;
        await idbPut('outbox', item);
        return { ok: false, retryable: false, error: item.last_error, data: data, item: item };
    }

    async function processOutbox() {
        if (syncing) return { processed: 0 };
        const reachable = await probeServerReachable();
        if (!reachable) {
            online = false;
            updateBadge();
            return { processed: 0, offline: true };
        }
        online = true;
        syncing = true;
        let processed = 0;
        const errors = [];
        const syncedItems = [];
        try {
            const pending = await getPendingOutbox();
            for (const item of pending) {
                try {
                    const r = await replayOutboxItem(item);
                    if (r.ok) {
                        processed++;
                        syncedItems.push(r.item);
                    } else {
                        errors.push({ client_uuid: item.client_uuid, error: r.error, label: item.label });
                        if (r.retryable) break; // dừng, lần sau thử tiếp
                    }
                } catch (e) {
                    item.status = 'pending';
                    item.last_error = String(e.message || e);
                    await idbPut('outbox', item);
                    errors.push({ client_uuid: item.client_uuid, error: item.last_error, label: item.label });
                    break;
                }
            }
            const all = await idbGetAll('outbox');
            backupOutboxLocal(all.filter(function (x) {
                return (!x.tenant || x.tenant === tenantKey) && (x.status === 'pending' || x.status === 'error');
            }));
            await purgeOldSyncedOutbox();
        } finally {
            syncing = false;
            updateBadge();
        }
        if (processed > 0 || errors.length > 0) {
            document.dispatchEvent(new CustomEvent('pos-offline-synced', {
                detail: { processed: processed, errors: errors, items: syncedItems },
            }));
        }
        return { processed: processed, errors: errors, items: syncedItems };
    }

    function ensureBadge() {
        let el = document.getElementById('posOfflineBadge');
        if (el) return el;
        el = document.createElement('div');
        el.id = 'posOfflineBadge';
        el.style.cssText = 'position:fixed;bottom:16px;right:16px;z-index:19999;max-width:360px;';
        document.body.appendChild(el);
        return el;
    }

    function formatOutboxLabel(item) {
        const p = item.payload || {};
        const total = p.total != null ? p.total : '';
        return item.label || ('Đơn ' + (total ? total + 'đ' : item.client_uuid.slice(0, 8)));
    }

    function showOutboxPanel() {
        getPendingOutbox().then(function (items) {
            let existing = document.getElementById('posOutboxPanel');
            if (existing) existing.remove();

            const wrap = document.createElement('div');
            wrap.id = 'posOutboxPanel';
            wrap.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:20000;display:flex;align-items:center;justify-content:center;padding:16px;';

            let rows = '';
            if (!items.length) {
                rows = '<p class="text-muted mb-0">Không có đơn chờ đồng bộ.</p>';
            } else {
                rows = items.map(function (item) {
                    const err = item.last_error ? '<div class="text-danger small">' + item.last_error + '</div>' : '';
                    const st = item.status === 'error'
                        ? '<span class="badge bg-danger">Lỗi</span>'
                        : '<span class="badge bg-warning text-dark">Chờ</span>';
                    return '<div class="border rounded p-2 mb-2" data-uuid="' + item.client_uuid + '">' +
                        '<div class="d-flex justify-content-between align-items-start gap-2">' +
                        '<div><strong>' + formatOutboxLabel(item) + '</strong><br>' +
                        '<small class="text-muted">' + (item.created_at || '') + '</small>' + err + '</div>' +
                        '<div>' + st + '</div></div>' +
                        '<div class="mt-2 d-flex gap-1 flex-wrap">' +
                        '<button type="button" class="btn btn-sm btn-primary pos-outbox-retry" data-uuid="' + item.client_uuid + '">Thử lại</button>' +
                        '<button type="button" class="btn btn-sm btn-outline-danger pos-outbox-del" data-uuid="' + item.client_uuid + '">Xóa</button>' +
                        '</div></div>';
                }).join('');
            }

            wrap.innerHTML = '<div class="card shadow-lg" style="max-width:520px;width:100%;max-height:80vh;overflow:auto;">' +
                '<div class="card-header d-flex justify-content-between align-items-center">' +
                '<strong>Đơn chờ đồng bộ</strong>' +
                '<button type="button" class="btn-close" id="posOutboxClose"></button></div>' +
                '<div class="card-body">' + rows +
                '<div class="d-flex gap-2 mt-2">' +
                '<button type="button" class="btn btn-success btn-sm" id="posOutboxSyncAll"' +
                (items.length ? '' : ' disabled') + '>Đồng bộ tất cả</button>' +
                '<button type="button" class="btn btn-outline-secondary btn-sm" id="posOutboxRefresh">Làm mới</button>' +
                '</div></div></div>';

            document.body.appendChild(wrap);

            wrap.addEventListener('click', function (ev) {
                if (ev.target === wrap) wrap.remove();
            });
            document.getElementById('posOutboxClose').onclick = function () { wrap.remove(); };
            document.getElementById('posOutboxRefresh').onclick = function () {
                wrap.remove();
                showOutboxPanel();
            };
            const syncAll = document.getElementById('posOutboxSyncAll');
            if (syncAll) {
                syncAll.onclick = function () {
                    syncAll.disabled = true;
                    processOutbox().finally(function () {
                        wrap.remove();
                        showOutboxPanel();
                    });
                };
            }
            wrap.querySelectorAll('.pos-outbox-retry').forEach(function (btn) {
                btn.onclick = function () {
                    const uid = btn.getAttribute('data-uuid');
                    btn.disabled = true;
                    retryOutboxItem(uid).finally(function () {
                        wrap.remove();
                        showOutboxPanel();
                    });
                };
            });
            wrap.querySelectorAll('.pos-outbox-del').forEach(function (btn) {
                btn.onclick = function () {
                    const uid = btn.getAttribute('data-uuid');
                    if (!confirm('Xóa đơn khỏi hàng đợi offline? Dữ liệu sẽ không được đồng bộ.')) return;
                    removeOutboxItem(uid).then(function () {
                        wrap.remove();
                        showOutboxPanel();
                    });
                };
            });
        });
    }

    async function updateBadge() {
        const el = ensureBadge();
        const pending = await getPendingCount();
        const meta = await getCatalogMeta();
        const offline = !isOnline();
        if (!offline && pending === 0) {
            el.innerHTML = '';
            el.style.display = 'none';
            return;
        }
        el.style.display = 'block';
        const parts = [];
        if (offline) {
            parts.push('<span class="badge bg-warning text-dark me-1"><i class="fas fa-wifi-slash me-1"></i>Offline</span>');
        }
        if (meta && meta.count) {
            parts.push('<span class="badge bg-secondary me-1" title="Danh mục cache">' + meta.count + ' SP</span>');
        }
        if (pending > 0) {
            parts.push('<button type="button" class="btn btn-sm btn-danger me-1" id="posOfflinePendingBtn">' +
                pending + ' đơn chờ</button>');
        }
        parts.push('<button type="button" class="btn btn-sm btn-outline-primary" id="posOfflineSyncBtn">Đồng bộ</button>');
        el.innerHTML = '<div class="card shadow-sm p-2"><div class="d-flex flex-wrap align-items-center gap-1">' + parts.join('') + '</div></div>';

        const pendingBtn = document.getElementById('posOfflinePendingBtn');
        if (pendingBtn) pendingBtn.onclick = showOutboxPanel;

        const btn = document.getElementById('posOfflineSyncBtn');
        if (btn) {
            btn.onclick = function () {
                btn.disabled = true;
                processOutbox().finally(function () { btn.disabled = false; });
            };
        }
    }

    function registerServiceWorker() {
        if (!('serviceWorker' in navigator)) return;
        navigator.serviceWorker.register('/sw-pos.js', { scope: '/' }).catch(function () {});
    }

    async function init(opts) {
        opts = opts || {};
        tenantKey = resolveTenantKey(opts);
        registerServiceWorker();
        await openDb().catch(function () {});
        await loadCachedScaleConfig();
        await loadOutboxFromBackup();
        await purgeOldSyncedOutbox();
        window.addEventListener('online', function () { setOnlineState(true); });
        window.addEventListener('offline', function () { setOnlineState(false); });
        setOnlineState(navigator.onLine);
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible' && isOnline()) {
                processOutbox().catch(function () {});
            }
        });
        if (isOnline()) {
            await syncCatalog({ includeMenu: !!opts.includeMenu });
            processOutbox().catch(function () {});
        }
        updateBadge();
        if (opts.autoSyncIntervalMs > 0) {
            setInterval(function () {
                if (isOnline()) {
                    syncCatalog({ includeMenu: !!opts.includeMenu }).catch(function () {});
                    processOutbox().catch(function () {});
                }
            }, opts.autoSyncIntervalMs);
        }
        return { online: isOnline(), tenantKey: tenantKey };
    }

    async function cacheFbSale(tableId, saleData) {
        if (!tableId) return;
        await idbPut('meta', {
            key: 'fb_sale_' + tableId,
            table_id: tableId,
            sale_id: saleData.sale_id,
            items: saleData.items || [],
            total_amount: saleData.total_amount || 0,
            cached_at: new Date().toISOString(),
            tenant: tenantKey,
        });
    }

    async function getCachedFbSale(tableId) {
        if (!tableId) return null;
        const row = await idbGet('meta', 'fb_sale_' + tableId);
        if (row && row.tenant && row.tenant !== tenantKey) return null;
        return row;
    }

    global.PosOffline = {
        init: init,
        isOnline: isOnline,
        syncCatalog: syncCatalog,
        getCatalogMeta: getCatalogMeta,
        searchProducts: searchProducts,
        scanBarcode: scanBarcode,
        submitSale: submitSale,
        processOutbox: processOutbox,
        getPendingCount: getPendingCount,
        getPendingOutbox: getPendingOutbox,
        removeOutboxItem: removeOutboxItem,
        retryOutboxItem: retryOutboxItem,
        showOutboxPanel: showOutboxPanel,
        updateBadge: updateBadge,
        cacheFbSale: cacheFbSale,
        getCachedFbSale: getCachedFbSale,
        uuid: uuid,
    };
})(window);
