# -*- coding: utf-8 -*-
from pathlib import Path

PURCH_SCRIPTS = """{% block scripts %}
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
(function () {
    function formatCurrency(amount) {
        return new Intl.NumberFormat('vi-VN').format(Number(amount || 0)) + ' đ';
    }
    function periodToFromFilter(val) {
        const m = new Date().getMonth() + 1;
        if (val === 'this_quarter') return Math.ceil(m / 3) * 3;
        if (val === 'this_year') return 12;
        return m;
    }
    let trendChart, pieChart;

    async function loadDash() {
        const year = new Date().getFullYear();
        const periodTo = periodToFromFilter(document.getElementById('dateFilter').value);
        const res = await fetch('/api/sme/purchasing-metrics?year=' + year + '&period_to=' + periodTo);
        const payload = await res.json();
        if (!payload.success) throw new Error(payload.error || 'Loi tai');
        const d = payload.data || {};

        document.getElementById('total-purchase').textContent = formatCurrency(d.total_purchase);
        document.getElementById('total-paid').textContent = formatCurrency(d.total_paid);
        document.getElementById('total-payable').textContent = formatCurrency(d.total_payable);
        document.getElementById('pending-orders').textContent = d.pending_orders || 0;
        document.getElementById('purchase-growth').textContent =
            d.growth_pct == null ? '—' : ((d.growth_pct >= 0 ? '+' : '') + d.growth_pct.toFixed(1) + '%');
        document.getElementById('paid-rate').textContent =
            d.paid_rate_pct == null ? '—' : (Math.round(d.paid_rate_pct) + '%');
        document.getElementById('urgent-payable-text').textContent =
            'ĐĐH chờ nhập ~ ' + formatCurrency(d.pending_order_value);

        const tableBody = document.getElementById('due-debts-table-body');
        const opens = d.open_orders || [];
        if (!opens.length) {
            tableBody.innerHTML = '<tr class="text-center text-muted"><td colspan="5" class="py-4">Không có đơn chờ nhập</td></tr>';
        } else {
            const stLabel = {draft:'Nháp',confirmed:'Xác nhận',partial:'Một phần'};
            tableBody.innerHTML = opens.map(o =>
                '<tr>' +
                '<td class="ps-3 fw-medium">' + (o.supplier_name || '') + '</td>' +
                '<td><code><a href="/SME_purchase_order_create?id=' + o.id + '">' + (o.po_no || '') + '</a></code></td>' +
                '<td>' + ((o.expected_date || o.po_date || '').slice(0,10)) + '</td>' +
                '<td class="text-end fw-bold">' + formatCurrency(o.total_amount) + '</td>' +
                '<td class="text-center"><span class="badge bg-warning bg-opacity-10 text-warning">' + (stLabel[o.status]||o.status) + '</span>' +
                ' <a class="btn btn-sm btn-outline-success py-0 ms-1" href="/SME_import?po_id=' + o.id + '">Nhập</a></td>' +
                '</tr>'
            ).join('');
        }

        const months = d.monthly || [];
        if (trendChart) trendChart.destroy();
        trendChart = new Chart(document.getElementById('purchaseTrendChart').getContext('2d'), {
            type: 'line',
            data: {
                labels: months.map(x => x.label),
                datasets: [{
                    label: 'Giá trị mua (152/153/156)',
                    data: months.map(x => x.purchase),
                    borderColor: '#0d6efd', borderWidth: 3, tension: 0.35, fill: true,
                    backgroundColor: 'rgba(13,110,253,0.12)', pointBackgroundColor: '#0d6efd'
                }]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => ' ' + formatCurrency(c.parsed.y) } } },
                scales: { y: { ticks: { callback: v => v >= 1e6 ? (v / 1e6) + ' Tr' : v } }, x: { grid: { display: false } } }
            }
        });

        const suppliers = d.suppliers || [];
        if (pieChart) pieChart.destroy();
        pieChart = new Chart(document.getElementById('supplierPieChart').getContext('2d'), {
            type: 'doughnut',
            data: {
                labels: suppliers.length ? suppliers.map(s => s.name) : ['Chưa có ĐĐH'],
                datasets: [{
                    data: suppliers.length ? suppliers.map(s => s.amount) : [1],
                    backgroundColor: ['#0d6efd', '#198754', '#ffc107', '#6c757d', '#dc3545', '#0dcaf0'],
                    borderWidth: 2
                }]
            },
            options: {
                responsive: true, maintainAspectRatio: false, cutout: '70%',
                plugins: {
                    legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 11 } } },
                    tooltip: { callbacks: { label: c => ' ' + c.label + ': ' + formatCurrency(c.parsed) } }
                }
            }
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.getElementById('dateFilter').addEventListener('change', () => loadDash().catch(e => alert(e.message)));
        loadDash().catch(e => {
            document.getElementById('due-debts-table-body').innerHTML =
                '<tr><td colspan="5" class="text-danger text-center py-4">' + e.message + '</td></tr>';
        });
    });
})();
</script>
{% endblock %}
"""

DEBT_TAIL = """
        <div class="d-flex justify-content-between align-items-center mb-4">
            <div>
                <h3 class="fw-bold text-primary mb-1">Tổng Quan Công Nợ</h3>
                <p class="text-muted mb-0">Số dư sổ kép TK 131 / 331 / 141 · từ nhật ký bút toán</p>
            </div>
            <div class="d-flex gap-2 align-items-center">
                <select class="form-select shadow-sm" id="dateFilter" style="width: 160px; height: 42px;">
                    <option value="this_month" selected>Tháng này</option>
                    <option value="this_quarter">Quý này</option>
                    <option value="this_year">Năm nay</option>
                </select>
                <a href="{{ url_for('SME_SoCongNoPhaiTra') }}" class="btn btn-primary quick-action-btn px-4">
                    <i class="fas fa-book me-2"></i> Sổ phải trả
                </a>
            </div>
        </div>

        <div class="row g-4 mb-4">
            <div class="col-xl-3 col-md-6"><div class="card metric-card h-100"><div class="card-body">
                <span class="text-muted small fw-semibold text-uppercase">Phải thu KH (131)</span>
                <h3 class="fw-bold text-primary mt-2 mb-0" id="total-ar">0 đ</h3>
                <div class="mt-3 text-muted small">Thu trong kỳ: <span id="ar-collected">0</span></div>
            </div></div></div>
            <div class="col-xl-3 col-md-6"><div class="card metric-card h-100"><div class="card-body">
                <span class="text-muted small fw-semibold text-uppercase">Phải trả NCC (331)</span>
                <h3 class="fw-bold text-danger mt-2 mb-0" id="total-ap">0 đ</h3>
                <div class="mt-3 text-muted small">Đã trả trong kỳ: <span id="ap-paid">0</span></div>
            </div></div></div>
            <div class="col-xl-3 col-md-6"><div class="card metric-card h-100"><div class="card-body">
                <span class="text-muted small fw-semibold text-uppercase">Tạm ứng NV (141)</span>
                <h3 class="fw-bold text-warning mt-2 mb-0" id="total-emp">0 đ</h3>
                <div class="mt-3"><a class="small" href="{{ url_for('SME_PhaiThuCongNhanVien') }}">Chi tiết →</a></div>
            </div></div></div>
            <div class="col-xl-3 col-md-6"><div class="card metric-card h-100"><div class="card-body">
                <span class="text-muted small fw-semibold text-uppercase">Tiền (111+112)</span>
                <h3 class="fw-bold text-success mt-2 mb-0" id="total-cash">0 đ</h3>
                <div class="mt-3 text-muted small">VLĐ ròng: <span id="net-wc">0</span></div>
            </div></div></div>
        </div>

        <div class="row g-4">
            <div class="col-lg-8"><div class="card shadow-sm border-0 h-100">
                <div class="card-header bg-white border-0 pt-3"><h6 class="fw-bold mb-0">Phát sinh phải thu / phải trả theo tháng</h6></div>
                <div class="card-body"><div style="position:relative;height:320px"><canvas id="debtTrendChart"></canvas></div></div>
            </div></div>
            <div class="col-lg-4"><div class="card shadow-sm border-0 h-100">
                <div class="card-header bg-white border-0 pt-3"><h6 class="fw-bold mb-0">Cơ cấu số dư</h6></div>
                <div class="card-body d-flex align-items-center justify-content-center">
                    <div style="position:relative;height:280px;width:100%"><canvas id="debtPieChart"></canvas></div>
                </div>
            </div></div>
        </div>

        <div class="card shadow-sm border-0 mt-4">
            <div class="card-header bg-white py-3"><h6 class="fw-bold mb-0">Lối tắt</h6></div>
            <div class="card-body d-flex flex-wrap gap-2">
                <a class="btn btn-outline-primary btn-sm" href="{{ url_for('SME_SoCongNoPhaiTra') }}">Công nợ phải trả</a>
                <a class="btn btn-outline-primary btn-sm" href="{{ url_for('SME_PhaiThuCongNhanVien') }}">Phải thu nhân viên</a>
                <a class="btn btn-outline-primary btn-sm" href="{{ url_for('SME_general_ledger') }}">Sổ cái</a>
                <a class="btn btn-outline-primary btn-sm" href="{{ url_for('SME_tax_nsnn') }}">Thuế &amp; NSNN</a>
            </div>
        </div>
    </div>
</div>
{% endblock %}

{% block scripts %}
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
(function () {
    function money(v){ return new Intl.NumberFormat('vi-VN').format(Number(v||0)) + ' đ'; }
    function periodToFromFilter(val) {
        const m = new Date().getMonth() + 1;
        if (val === 'this_quarter') return Math.ceil(m / 3) * 3;
        if (val === 'this_year') return 12;
        return m;
    }
    let trendChart, pieChart;
    async function loadDash() {
        const year = new Date().getFullYear();
        const periodTo = periodToFromFilter(document.getElementById('dateFilter').value);
        const res = await fetch('/api/sme/debt-metrics?year=' + year + '&period_to=' + periodTo);
        const payload = await res.json();
        if (!payload.success) throw new Error(payload.error || 'Loi');
        const d = payload.data || {};
        document.getElementById('total-ar').textContent = money(d.receivable);
        document.getElementById('total-ap').textContent = money(d.payable);
        document.getElementById('total-emp').textContent = money(d.employee_advance);
        document.getElementById('total-cash').textContent = money(d.cash);
        document.getElementById('ar-collected').textContent = money(d.ar_collected_ytd);
        document.getElementById('ap-paid').textContent = money(d.ap_paid_ytd);
        document.getElementById('net-wc').textContent = money(d.net_working_capital);
        const months = d.monthly || [];
        if (trendChart) trendChart.destroy();
        trendChart = new Chart(document.getElementById('debtTrendChart').getContext('2d'), {
            type: 'bar',
            data: {
                labels: months.map(x => x.label),
                datasets: [
                    { label: 'Tăng phải thu', data: months.map(x => x.receivable_increase), backgroundColor: '#0d6efd' },
                    { label: 'Tăng phải trả', data: months.map(x => x.payable_increase), backgroundColor: '#dc3545' }
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { tooltip: { callbacks: { label: c => ' ' + c.dataset.label + ': ' + money(c.parsed.y) } } },
                scales: { y: { ticks: { callback: v => v >= 1e6 ? (v/1e6)+' Tr' : v } } }
            }
        });
        if (pieChart) pieChart.destroy();
        pieChart = new Chart(document.getElementById('debtPieChart').getContext('2d'), {
            type: 'doughnut',
            data: {
                labels: ['Phải thu 131', 'Phải trả 331', 'Tạm ứng 141'],
                datasets: [{ data: [d.receivable||0, d.payable||0, d.employee_advance||0], backgroundColor: ['#0d6efd','#dc3545','#ffc107'] }]
            },
            options: {
                responsive: true, maintainAspectRatio: false, cutout: '65%',
                plugins: { legend: { position: 'bottom' }, tooltip: { callbacks: { label: c => ' ' + c.label + ': ' + money(c.parsed) } } }
            }
        });
    }
    document.addEventListener('DOMContentLoaded', () => {
        document.getElementById('dateFilter').addEventListener('change', () => loadDash().catch(e => alert(e.message)));
        loadDash().catch(e => alert(e.message));
    });
})();
</script>
{% endblock %}
"""

p = Path(r'C:\SME\templates\KeToanSME\dashboard_purchasing.html')
text = p.read_text(encoding='utf-8')
idx = text.find('{% block scripts %}')
assert idx >= 0
text = text[:idx] + PURCH_SCRIPTS
text = text.replace('Năm 2026', 'Năm nay', 1)
text = text.replace('Công Nợ Đến Hạn & Quá Hạn', 'Đơn đặt hàng chờ nhập kho', 1)
text = text.replace('<th>Số Hóa Đơn</th>', '<th>Số ĐĐH</th>', 1)
text = text.replace('<th>Ngày Đến Hạn</th>', '<th>Ngày DK</th>', 1)
p.write_text(text, encoding='utf-8')
print('purchasing ok')

p2 = Path(r'C:\SME\templates\KeToanSME\dashboard_debt.html')
text2 = p2.read_text(encoding='utf-8')
# keep head through sidebar end of sme-content start header
marker = '<div class="sme-content">'
i = text2.find(marker)
assert i >= 0
# find after opening sme-content, keep sidebar before
head = text2[: i + len(marker)]
# drop old main content/scripts, append new
out = head + DEBT_TAIL
# fix title
out = out.replace('Dashboard Mua Hàng - SME Accounting', 'Dashboard Công Nợ - SME Accounting', 1)
p2.write_text(out, encoding='utf-8')
print('debt ok')
