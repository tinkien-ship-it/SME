# -*- coding: utf-8 -*-
"""Patch remaining demo hubs: sale, sosach, BCTC (standalone — do not import hub script)."""
from pathlib import Path

STYLE = r'''{% extends "base.html" %}
{% block title %}__TITLE__{% endblock %}

{% block extra_style %}
<style>
.sme-dashboard-container{display:flex;min-height:calc(100vh - 160px);margin:-1.5rem}
.sme-sidebar{width:280px;background:#fff;border-right:1px solid #e9ecef;padding:1.5rem 0;flex-shrink:0;overflow-y:auto;box-shadow:2px 0 10px rgba(0,0,0,.03)}
.nav-link-sub{display:flex;align-items:center;padding:.75rem 1.5rem;color:#495057;text-decoration:none;font-weight:500;border-left:4px solid transparent}
.nav-link-sub:hover{background:#f8f9fa;color:#0d6efd}
.nav-link-sub.active{background:#e7f1ff;color:#0d6efd;border-left-color:#0d6efd;font-weight:600}
.submenu{padding-left:2.8rem;font-size:.95rem}
.sme-content{flex-grow:1;padding:2rem;background:linear-gradient(135deg,#f8f9fc 0%,#f0f2f5 100%);overflow-y:auto}
.metric-card{border:none;border-radius:16px;background:#fff;box-shadow:0 4px 20px rgba(0,0,0,.06)}
.metric-icon{width:56px;height:56px;border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:1.6rem}
.quick-action-btn{height:42px;font-weight:600;border-radius:10px;display:inline-flex;align-items:center}
</style>
{% endblock %}
'''


def money_js():
    return """
function money(v){return new Intl.NumberFormat('vi-VN').format(Number(v||0))+' đ';}
function periodToFromFilter(val){const m=new Date().getMonth()+1;if(val==='this_quarter')return Math.ceil(m/3)*3;if(val==='this_year')return 12;return m;}
"""


def extract_sidebar(path: Path) -> str:
    text = path.read_text(encoding='utf-8')
    start = text.find('<div class="sme-sidebar')
    end = text.find('<!-- Main Content -->')
    if start < 0 or end < 0:
        raise RuntimeError(f'Cannot find sidebar in {path}')
    return text[start:end].rstrip()


def card(label, id_, color='primary', icon='chart-pie', sub=''):
    return f'''
<div class="col-xl-3 col-md-6"><div class="card metric-card h-100"><div class="card-body">
  <div class="d-flex justify-content-between align-items-start">
    <div><span class="text-muted small fw-semibold text-uppercase">{label}</span>
    <h3 class="fw-bold text-{color} mt-2 mb-0" id="{id_}">0</h3></div>
    <div class="metric-icon bg-{color} bg-opacity-10 text-{color}"><i class="fas fa-{icon}"></i></div>
  </div>
  <div class="mt-3 text-muted small">{sub}</div>
</div></div></div>'''


def write_hub(path: Path, *, title: str, heading: str, subtitle: str, cta_html: str,
              cards_html: str, chart_title: str, chart_id: str, pie_title: str, pie_id: str,
              api: str, render_js: str, links_html: str, sidebar_fix=None):
    sidebar = sidebar_fix(extract_sidebar(path)) if sidebar_fix else extract_sidebar(path)
    html = STYLE.replace('__TITLE__', title)
    html += '{% block content %}\n<div class="sme-dashboard-container">\n'
    html += sidebar + '\n'
    body = """  <div class="sme-content">
    <div class="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-2">
      <div>
        <h3 class="fw-bold text-primary mb-1">__HEADING__</h3>
        <p class="text-muted mb-0">__SUBTITLE__</p>
      </div>
      <div class="d-flex gap-2 align-items-center">
        <select class="form-select shadow-sm" id="dateFilter" style="width:160px;height:42px">
          <option value="this_month" selected>Tháng này</option>
          <option value="this_quarter">Quý này</option>
          <option value="this_year">Năm nay</option>
        </select>
        __CTA__
      </div>
    </div>
    <div class="row g-4 mb-4">__CARDS__</div>
    <div class="row g-4">
      <div class="col-lg-8"><div class="card shadow-sm border-0 h-100">
        <div class="card-header bg-white border-0 pt-3"><h6 class="fw-bold mb-0">__CHART_TITLE__</h6></div>
        <div class="card-body"><div style="position:relative;height:320px"><canvas id="__CHART_ID__"></canvas></div></div>
      </div></div>
      <div class="col-lg-4"><div class="card shadow-sm border-0 h-100">
        <div class="card-header bg-white border-0 pt-3"><h6 class="fw-bold mb-0">__PIE_TITLE__</h6></div>
        <div class="card-body d-flex align-items-center justify-content-center">
          <div style="position:relative;height:280px;width:100%"><canvas id="__PIE_ID__"></canvas></div>
        </div>
      </div></div>
    </div>
    <div class="card shadow-sm border-0 mt-4">
      <div class="card-header bg-white py-3"><h6 class="fw-bold mb-0">Lối tắt</h6></div>
      <div class="card-body d-flex flex-wrap gap-2">__LINKS__</div>
    </div>
  </div>
</div>
{% endblock %}

{% block scripts %}
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
(function(){
__MONEY_JS__
let trendChart, pieChart;
async function loadDash(){
  const year=new Date().getFullYear();
  const periodTo=periodToFromFilter(document.getElementById('dateFilter').value);
  const res=await fetch('__API__?year='+year+'&period_to='+periodTo);
  const payload=await res.json();
  if(!payload.success) throw new Error(payload.error||'Lỗi');
  const d=payload.data||{};
__RENDER_JS__
}
document.addEventListener('DOMContentLoaded',()=>{
  document.getElementById('dateFilter').addEventListener('change',()=>loadDash().catch(e=>alert(e.message)));
  loadDash().catch(e=>alert(e.message));
});
})();
</script>
{% endblock %}
"""
    body = (body
            .replace('__HEADING__', heading)
            .replace('__SUBTITLE__', subtitle)
            .replace('__CTA__', cta_html)
            .replace('__CARDS__', cards_html)
            .replace('__CHART_TITLE__', chart_title)
            .replace('__CHART_ID__', chart_id)
            .replace('__PIE_TITLE__', pie_title)
            .replace('__PIE_ID__', pie_id)
            .replace('__LINKS__', links_html)
            .replace('__MONEY_JS__', money_js())
            .replace('__API__', api)
            .replace('__RENDER_JS__', render_js))
    path.write_text(html + body, encoding='utf-8')
    print('wrote', path.name)


root = Path(r'C:\SME\templates\KeToanSME')

write_hub(
    root / 'dashboard_sale.html',
    title='Dashboard Bán Hàng - SME',
    heading='Tổng quan bán hàng',
    subtitle='Doanh thu / giá vốn / phải thu từ sổ kép SME',
    cta_html='<a href="{{ url_for(\'sale\') }}" class="btn btn-primary quick-action-btn px-4"><i class="fas fa-cart-shopping me-2"></i>Điểm bán hàng</a>',
    cards_html=''.join([
        card('Doanh thu YTD', 'm1', 'primary', 'chart-line'),
        card('Giá vốn (632)', 'm2', 'danger', 'box'),
        card('Lãi gộp', 'm3', 'success', 'coins', '<span id="m3sub"></span>'),
        card('Phải thu KH (131)', 'm4', 'warning', 'hand-holding-dollar', '<span id="m4sub"></span>'),
    ]),
    chart_title='Doanh thu & giá vốn theo tháng',
    chart_id='saleTrend',
    pie_title='Cơ cấu DT / GV / LG',
    pie_id='salePie',
    api='/api/sme/sales-metrics',
    render_js='''
  document.getElementById('m1').textContent=money(d.revenue);
  document.getElementById('m2').textContent=money(d.cogs);
  document.getElementById('m3').textContent=money(d.gross_profit);
  document.getElementById('m3sub').textContent=d.gross_margin_pct==null?'—':('Biên lãi gộp '+d.gross_margin_pct.toFixed(1)+'%');
  document.getElementById('m4').textContent=money(d.receivable);
  document.getElementById('m4sub').textContent='Đã thu YTD '+money(d.collected_ytd)+' · Đơn '+ (d.order_count||0);
  const months=d.monthly||[];
  if(trendChart) trendChart.destroy();
  trendChart=new Chart(document.getElementById('saleTrend').getContext('2d'),{type:'bar',data:{labels:months.map(x=>x.label),datasets:[
    {label:'Doanh thu',data:months.map(x=>x.revenue),backgroundColor:'#0d6efd'},
    {label:'Giá vốn',data:months.map(x=>x.cogs),backgroundColor:'#dc3545'}
  ]},options:{responsive:true,maintainAspectRatio:false,plugins:{tooltip:{callbacks:{label:c=>' '+c.dataset.label+': '+money(c.parsed.y)}}}}});
  if(pieChart) pieChart.destroy();
  pieChart=new Chart(document.getElementById('salePie').getContext('2d'),{type:'doughnut',data:{labels:['Lãi gộp','Giá vốn'],datasets:[{data:[Math.max(0,d.gross_profit||0),Math.max(0,d.cogs||0)],backgroundColor:['#198754','#dc3545']}]},options:{responsive:true,maintainAspectRatio:false,cutout:'65%',plugins:{legend:{position:'bottom'},tooltip:{callbacks:{label:c=>' '+c.label+': '+money(c.parsed)}}}}});
''',
    links_html='''
<a class="btn btn-outline-primary btn-sm" href="{{ url_for('sale') }}">POS</a>
<a class="btn btn-outline-primary btn-sm" href="{{ url_for('order') }}">Đơn hàng</a>
<a class="btn btn-outline-primary btn-sm" href="{{ url_for('SoCongNoPhaiThu') }}">Phải thu</a>
<a class="btn btn-outline-primary btn-sm" href="{{ url_for('SME_general_ledger') }}">Sổ cái</a>
''',
)

write_hub(
    root / 'dashboard_sosachketoan.html',
    title='Sổ Sách Kế Toán - SME',
    heading='Tổng quan sổ sách',
    subtitle='Nhật ký · sổ cái · khóa kỳ · cân đối phát sinh',
    cta_html='<a href="{{ url_for(\'SME_journal\') }}" class="btn btn-primary quick-action-btn px-4"><i class="fas fa-book-open me-2"></i>Nhật ký</a>',
    cards_html=''.join([
        card('Bút toán YTD', 'm1', 'primary', 'book'),
        card('TK có phát sinh', 'm2', 'info', 'sitemap'),
        card('PS Nợ / Có', 'm3', 'secondary', 'scale-balanced', '<span id="m3sub"></span>'),
        card('Kỳ đã khóa', 'm4', 'danger', 'lock', '<span id="m4sub"></span>'),
    ]),
    chart_title='Phát sinh Nợ/Có theo tháng',
    chart_id='booksTrend',
    pie_title='Cân đối YTD',
    pie_id='booksPie',
    api='/api/sme/books-metrics',
    render_js='''
  document.getElementById('m1').textContent=String(d.entry_count||0);
  document.getElementById('m2').textContent=String(d.accounts_touched||0);
  document.getElementById('m3').textContent=money(d.period_debit);
  document.getElementById('m3sub').textContent='Có '+money(d.period_credit)+(d.balanced?' · Cân bằng':' · Lệch');
  document.getElementById('m4').textContent=String(d.locked_periods||0);
  document.getElementById('m4sub').textContent='Kỳ mở còn lại ~ '+(d.open_periods||0);
  const months=d.monthly||[];
  if(trendChart) trendChart.destroy();
  trendChart=new Chart(document.getElementById('booksTrend').getContext('2d'),{type:'bar',data:{labels:months.map(x=>x.label),datasets:[
    {label:'Nợ',data:months.map(x=>x.debit),backgroundColor:'#0d6efd'},
    {label:'Có',data:months.map(x=>x.credit),backgroundColor:'#198754'}
  ]},options:{responsive:true,maintainAspectRatio:false,plugins:{tooltip:{callbacks:{label:c=>' '+c.dataset.label+': '+money(c.parsed.y)}}}}});
  if(pieChart) pieChart.destroy();
  pieChart=new Chart(document.getElementById('booksPie').getContext('2d'),{type:'doughnut',data:{labels:['PS Nợ','PS Có'],datasets:[{data:[d.period_debit||0,d.period_credit||0],backgroundColor:['#0d6efd','#198754']}]},options:{responsive:true,maintainAspectRatio:false,cutout:'65%',plugins:{legend:{position:'bottom'},tooltip:{callbacks:{label:c=>' '+c.label+': '+money(c.parsed)}}}}});
''',
    links_html='''
<a class="btn btn-outline-primary btn-sm" href="{{ url_for('SME_journal') }}">Nhật ký</a>
<a class="btn btn-outline-primary btn-sm" href="{{ url_for('SME_general_ledger') }}">Sổ cái / CĐPS</a>
<a class="btn btn-outline-primary btn-sm" href="{{ url_for('SME_chart_of_accounts') }}">Danh mục TK</a>
<a class="btn btn-outline-primary btn-sm" href="{{ url_for('SME_auto_posting') }}">Tự động kỳ</a>
''',
)


def _bctc_sidebar(sb: str) -> str:
    return (sb
            .replace("url_for('SME_BCTC')", "url_for('SME_BCTC_reports')")
            .replace('url_for("SME_BCTC")', 'url_for("SME_BCTC_reports")'))


write_hub(
    root / 'dashboard_BCTC.html',
    title='Dashboard BCTC - SME',
    heading='Tổng quan báo cáo tài chính',
    subtitle='Chỉ số nhanh từ sổ kép · lập B01–B09 tại trang báo cáo',
    cta_html='<a href="{{ url_for(\'SME_BCTC_reports\') }}" class="btn btn-primary quick-action-btn px-4"><i class="fas fa-file-invoice me-2"></i>Lập B01–B09</a>',
    cards_html=''.join([
        card('Doanh thu YTD', 'm1', 'primary', 'chart-line'),
        card('Lợi nhuận (xấp xỉ)', 'm2', 'success', 'coins'),
        card('Tài sản (xấp xỉ)', 'm3', 'info', 'building'),
        card('Nợ phải trả (xấp xỉ)', 'm4', 'danger', 'file-invoice-dollar', '<span id="m4sub"></span>'),
    ]),
    chart_title='Doanh thu / LN theo tháng',
    chart_id='bctcTrend',
    pie_title='Cơ cấu thuế cuối kỳ',
    pie_id='bctcPie',
    api='/api/sme/bctc-metrics',
    render_js='''
  document.getElementById('m1').textContent=money(d.revenue);
  document.getElementById('m2').textContent=money(d.profit);
  document.getElementById('m3').textContent=money(d.total_assets_approx);
  document.getElementById('m4').textContent=money(d.total_liabilities_approx);
  document.getElementById('m4sub').textContent='VCSH ~ '+money(d.equity_approx)+' · Tiền '+money(d.cash);
  const months=d.monthly||[];
  if(trendChart) trendChart.destroy();
  trendChart=new Chart(document.getElementById('bctcTrend').getContext('2d'),{type:'line',data:{labels:months.map(x=>x.label),datasets:[
    {label:'Doanh thu',data:months.map(x=>x.revenue),borderColor:'#0d6efd',tension:.3,fill:false},
    {label:'Lợi nhuận',data:months.map(x=>x.profit),borderColor:'#198754',tension:.3,fill:false}
  ]},options:{responsive:true,maintainAspectRatio:false,plugins:{tooltip:{callbacks:{label:c=>' '+c.dataset.label+': '+money(c.parsed.y)}}}}});
  const tb=d.tax_breakdown||{};
  if(pieChart) pieChart.destroy();
  pieChart=new Chart(document.getElementById('bctcPie').getContext('2d'),{type:'doughnut',data:{labels:['GTGT','TNDN','TNCN','Khác'],datasets:[{data:[tb.gtgt||0,tb.tndn||0,tb.tncn||0,tb.other||0],backgroundColor:['#0d6efd','#dc3545','#ffc107','#6c757d']}]},options:{responsive:true,maintainAspectRatio:false,cutout:'65%',plugins:{legend:{position:'bottom'},tooltip:{callbacks:{label:c=>' '+c.label+': '+money(c.parsed)}}}}});
''',
    links_html='''
<a class="btn btn-outline-primary btn-sm" href="{{ url_for('SME_BCTC_reports') }}">B01–B09</a>
<a class="btn btn-outline-primary btn-sm" href="{{ url_for('SME_mgmt_report') }}">Báo cáo QT</a>
<a class="btn btn-outline-primary btn-sm" href="{{ url_for('SME_general_ledger') }}">Sổ cái</a>
<a class="btn btn-outline-primary btn-sm" href="{{ url_for('SME_tax_nsnn') }}">Thuế &amp; NSNN</a>
''',
    sidebar_fix=_bctc_sidebar,
)

print('remaining hubs done')
