/**
 * Bảng danh sách: sort, resize cột, lọc theo từng cột (nút funnel trên tiêu đề).
 */
(function (global) {
    'use strict';

    function escapeHtml(text) {
        return String(text ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function normalizeText(v) {
        return String(v ?? '').toLowerCase().normalize('NFD').replace(/\p{Diacritic}/gu, '');
    }

    function parseNumber(v) {
        if (v == null || v === '') return null;
        const n = parseFloat(String(v).replace(/[^\d.-]/g, ''));
        return Number.isFinite(n) ? n : null;
    }

    function parseDateValue(v) {
        if (!v) return null;
        if (v instanceof Date && !isNaN(v)) return v;
        const s = String(v).trim();
        if (/^\d{4}-\d{2}-\d{2}/.test(s)) return new Date(s.slice(0, 10));
        const m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
        if (m) return new Date(+m[3], +m[2] - 1, +m[1]);
        const d = new Date(s);
        return isNaN(d) ? null : d;
    }

    class DataListTable {
        constructor(options) {
            this.options = options;
            this.table = typeof options.table === 'string'
                ? document.querySelector(options.table)
                : options.table;
            if (!this.table) throw new Error('DataListTable: table not found');

            this.columns = options.columns || [];
            this.allData = [];
            this.filters = {};
            this.sortKey = options.defaultSort?.key || null;
            this.sortDir = options.defaultSort?.dir || 'desc';
            this.countEl = options.countSelector
                ? document.querySelector(options.countSelector)
                : null;
            this.countTemplate = options.countTemplate || null;
            this.activeFiltersEl = options.activeFiltersSelector
                ? document.querySelector(options.activeFiltersSelector)
                : null;

            this._popover = null;
            this._popoverCol = null;
            this._buildHeader();
            this._bindSort();
            this._bindResize();
            this._bindOutsideClick();
        }

        _buildHeader() {
            const thead = this.table.querySelector('thead');
            if (!thead) return;

            let tr = thead.querySelector('tr');
            if (!tr) {
                tr = document.createElement('tr');
                thead.appendChild(tr);
            }

            this.table.classList.add('enhanced-data-table');

            if (this.columns.length === 0) {
                tr.querySelectorAll('th').forEach((th, idx) => {
                    const key = th.dataset.col || th.dataset.sort || `col_${idx}`;
                    this.columns.push({
                        key,
                        label: th.textContent.trim(),
                        sortable: th.classList.contains('sortable') || th.dataset.sortable === 'true',
                        filter: th.dataset.filter !== 'false' && !th.classList.contains('no-filter'),
                        filterType: th.dataset.filterType || 'text',
                        filterOptions: th.dataset.filterOptions
                            ? JSON.parse(th.dataset.filterOptions)
                            : null,
                        width: th.style.width || null,
                        align: th.classList.contains('text-center') ? 'center'
                            : th.classList.contains('text-end') ? 'end' : 'start',
                        resizable: !th.classList.contains('no-resize'),
                    });
                });
            }

            tr.innerHTML = '';
            this.columns.forEach((col) => {
                const th = document.createElement('th');
                if (col.width) th.style.width = col.width;
                if (col.align === 'center') th.classList.add('text-center');
                if (col.align === 'end') th.classList.add('text-end');
                if (col.sortable) {
                    th.classList.add('sortable');
                    th.dataset.sort = col.key;
                    if (this.sortKey === col.key) {
                        th.classList.add(this.sortDir === 'asc' ? 'sorted-asc' : 'sorted-desc');
                    }
                }

                const canFilter = col.filter !== false && col.key !== 'actions';
                const filterBtn = canFilter
                    ? `<button type="button" class="col-filter-btn" data-filter-col="${escapeHtml(col.key)}" title="Lọc cột"><i class="bi bi-funnel"></i></button>`
                    : '';

                th.innerHTML = `
                    <div class="th-inner">
                        <span class="th-label">${escapeHtml(col.label)}${col.sortable ? ' <span class="sort-icon"></span>' : ''}</span>
                        <span class="th-actions">${filterBtn}</span>
                    </div>
                    ${col.resizable !== false ? '<div class="col-resizer"></div>' : ''}
                `;
                tr.appendChild(th);
            });

            tr.querySelectorAll('.col-filter-btn').forEach((btn) => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    this._openFilterPopover(btn.dataset.filterCol, btn);
                });
            });
        }

        _bindSort() {
            this.table.querySelectorAll('th.sortable .th-label').forEach((label) => {
                label.addEventListener('click', () => {
                    const th = label.closest('th');
                    const key = th.dataset.sort;
                    if (this.sortKey === key) {
                        this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc';
                    } else {
                        this.sortKey = key;
                        this.sortDir = 'desc';
                    }
                    this.table.querySelectorAll('th.sortable').forEach((h) => {
                        h.classList.remove('sorted-asc', 'sorted-desc');
                    });
                    th.classList.add(this.sortDir === 'asc' ? 'sorted-asc' : 'sorted-desc');
                    this._apply();
                });
            });
        }

        _bindResize() {
            this.table.querySelectorAll('th').forEach((col) => {
                const resizer = col.querySelector('.col-resizer');
                if (!resizer) return;
                let startX = 0;
                let startW = 0;
                const onMove = (e) => {
                    col.style.width = `${Math.max(60, startW + (e.clientX - startX))}px`;
                };
                const onUp = () => {
                    document.removeEventListener('mousemove', onMove);
                    document.removeEventListener('mouseup', onUp);
                };
                resizer.addEventListener('mousedown', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    startX = e.clientX;
                    startW = col.offsetWidth;
                    document.addEventListener('mousemove', onMove);
                    document.addEventListener('mouseup', onUp);
                });
            });
        }

        _bindOutsideClick() {
            document.addEventListener('click', (e) => {
                if (!this._popover) return;
                if (this._popover.contains(e.target)) return;
                if (e.target.closest('.col-filter-btn')) return;
                this._closePopover();
            });
        }

        _getColumn(key) {
            return this.columns.find((c) => c.key === key);
        }

        _openFilterPopover(colKey, anchorBtn) {
            this._closePopover();
            const col = this._getColumn(colKey);
            if (!col) return;

            const existing = this.filters[colKey] || {};
            const pop = document.createElement('div');
            pop.className = 'col-filter-popover';
            pop.innerHTML = `
                <div class="popover-title">Lọc: ${escapeHtml(col.label)}</div>
                ${this._filterFieldsHtml(col, existing)}
                <div class="popover-actions">
                    <button type="button" class="btn btn-light btn-sm btn-clear-col">Xóa lọc</button>
                    <button type="button" class="btn btn-primary btn-sm btn-apply-col">Áp dụng</button>
                </div>
            `;

            document.body.appendChild(pop);
            const rect = anchorBtn.getBoundingClientRect();
            pop.style.top = `${rect.bottom + 6}px`;
            pop.style.left = `${Math.min(rect.left, window.innerWidth - pop.offsetWidth - 8)}px`;

            pop.querySelector('.btn-apply-col').addEventListener('click', () => {
                this.filters[colKey] = this._readFilterValues(pop, col);
                if (this._isEmptyFilter(this.filters[colKey])) delete this.filters[colKey];
                this._updateFilterButtons();
                this._apply();
                this._closePopover();
            });

            pop.querySelector('.btn-clear-col').addEventListener('click', () => {
                delete this.filters[colKey];
                this._updateFilterButtons();
                this._apply();
                this._closePopover();
            });

            this._popover = pop;
            this._popoverCol = colKey;
            const input = pop.querySelector('input, select');
            if (input) input.focus();
        }

        _filterFieldsHtml(col, existing) {
            const t = col.filterType || 'text';
            if (t === 'number') {
                return `
                    <div class="mb-2"><label class="form-label small mb-1">Từ</label>
                    <input type="text" class="form-control filter-min" value="${escapeHtml(existing.min ?? '')}" placeholder="0"></div>
                    <div><label class="form-label small mb-1">Đến</label>
                    <input type="text" class="form-control filter-max" value="${escapeHtml(existing.max ?? '')}" placeholder="∞"></div>`;
            }
            if (t === 'date') {
                return `
                    <div class="mb-2"><label class="form-label small mb-1">Từ ngày</label>
                    <input type="text" class="form-control filter-from" value="${escapeHtml(existing.from ?? '')}" placeholder="dd/mm/yyyy"></div>
                    <div><label class="form-label small mb-1">Đến ngày</label>
                    <input type="text" class="form-control filter-to" value="${escapeHtml(existing.to ?? '')}" placeholder="dd/mm/yyyy"></div>`;
            }
            if (t === 'select' && col.filterOptions) {
                const opts = col.filterOptions.map((o) => {
                    const val = typeof o === 'object' ? o.value : o;
                    const lab = typeof o === 'object' ? o.label : o;
                    const sel = existing.value === val ? ' selected' : '';
                    return `<option value="${escapeHtml(val)}"${sel}>${escapeHtml(lab)}</option>`;
                }).join('');
                return `<select class="form-select filter-value"><option value="">— Tất cả —</option>${opts}</select>`;
            }
            return `<input type="text" class="form-control filter-value" value="${escapeHtml(existing.value ?? '')}" placeholder="Chứa...">`;
        }

        _readFilterValues(pop, col) {
            const t = col.filterType || 'text';
            if (t === 'number') {
                return {
                    min: pop.querySelector('.filter-min')?.value.trim() || '',
                    max: pop.querySelector('.filter-max')?.value.trim() || '',
                };
            }
            if (t === 'date') {
                return {
                    from: pop.querySelector('.filter-from')?.value.trim() || '',
                    to: pop.querySelector('.filter-to')?.value.trim() || '',
                };
            }
            return { value: pop.querySelector('.filter-value')?.value.trim() || '' };
        }

        _isEmptyFilter(f) {
            if (!f) return true;
            return !Object.values(f).some((v) => String(v || '').trim());
        }

        _closePopover() {
            if (this._popover) {
                this._popover.remove();
                this._popover = null;
                this._popoverCol = null;
            }
        }

        _updateFilterButtons() {
            this.table.querySelectorAll('.col-filter-btn').forEach((btn) => {
                const key = btn.dataset.filterCol;
                btn.classList.toggle('active', !!this.filters[key]);
            });
            if (this.activeFiltersEl) {
                const n = Object.keys(this.filters).length;
                if (n > 0) {
                    this.activeFiltersEl.textContent = `${n} bộ lọc cột đang bật`;
                    this.activeFiltersEl.classList.remove('d-none');
                } else {
                    this.activeFiltersEl.classList.add('d-none');
                }
            }
        }

        _cellValue(row, col) {
            if (typeof col.getValue === 'function') return col.getValue(row);
            if (typeof col.sortValue === 'function') return col.sortValue(row);
            return row[col.key];
        }

        _sortValue(row, col) {
            if (typeof col.sortValue === 'function') return col.sortValue(row);
            return this._cellValue(row, col);
        }

        _matchFilter(row, col, filter) {
            const raw = this._cellValue(row, col);
            const t = col.filterType || 'text';

            if (t === 'number') {
                const num = parseNumber(raw);
                if (num == null) return false;
                const min = parseNumber(filter.min);
                const max = parseNumber(filter.max);
                if (min != null && num < min) return false;
                if (max != null && num > max) return false;
                return true;
            }

            if (t === 'date') {
                const d = parseDateValue(raw);
                if (!d) return false;
                const from = parseDateValue(filter.from);
                const to = parseDateValue(filter.to);
                if (from && d < from) return false;
                if (to) {
                    const toEnd = new Date(to);
                    toEnd.setHours(23, 59, 59, 999);
                    if (d > toEnd) return false;
                }
                return true;
            }

            if (t === 'select') {
                if (!filter.value) return true;
                return String(raw ?? '') === String(filter.value);
            }

            const needle = normalizeText(filter.value);
            if (!needle) return true;
            return normalizeText(raw).includes(needle);
        }

        _filteredSortedData() {
            let rows = [...this.allData];
            const activeFilters = Object.entries(this.filters);
            if (activeFilters.length) {
                rows = rows.filter((row) => activeFilters.every(([key, f]) => {
                    const col = this._getColumn(key);
                    return col ? this._matchFilter(row, col, f) : true;
                }));
            }
            if (this.sortKey) {
                const col = this._getColumn(this.sortKey);
                if (col) {
                    rows.sort((a, b) => {
                        let va = this._sortValue(a, col);
                        let vb = this._sortValue(b, col);
                        if (va instanceof Date) va = va.getTime();
                        if (vb instanceof Date) vb = vb.getTime();
                        if (va == null) va = '';
                        if (vb == null) vb = '';
                        if (va < vb) return this.sortDir === 'asc' ? -1 : 1;
                        if (va > vb) return this.sortDir === 'asc' ? 1 : -1;
                        return 0;
                    });
                }
            }
            return rows;
        }

        _apply() {
            const rows = this._filteredSortedData();
            const tbody = this.table.querySelector('tbody');
            if (this.options.renderRows) {
                this.options.renderRows(rows, tbody);
            }
            if (this.countEl) {
                if (this.countTemplate) {
                    this.countEl.textContent = this.countTemplate
                        .replace('{n}', rows.length)
                        .replace('{total}', this.allData.length);
                } else {
                    this.countEl.textContent = String(rows.length);
                }
            }
            if (typeof this.options.onFiltered === 'function') {
                this.options.onFiltered(rows, this.allData.length);
            }
        }

        setData(data) {
            this.allData = Array.isArray(data) ? data : [];
            this._apply();
        }

        clearColumnFilters() {
            this.filters = {};
            this._updateFilterButtons();
            this._apply();
        }

        getFilteredData() {
            return this._filteredSortedData();
        }
    }

    global.DataListTable = {
        create(options) {
            return new DataListTable(options);
        },
    };
})(window);
