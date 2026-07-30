# -*- coding: utf-8 -*-
"""Helpers to rewrite SME hub dashboards with live metrics. Import-safe (no side effects)."""
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


def write_hub(path: Path, *, title: str, heading: str, subtitle: str, cta_html: str,
              cards_html: str, chart_title: str, chart_id: str, pie_title: str, pie_id: str,
              api: str, render_js: str, links_html: str, sidebar_fix=None):
    sidebar = extract_sidebar(path)
    if sidebar_fix:
        sidebar = sidebar_fix(sidebar)
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
