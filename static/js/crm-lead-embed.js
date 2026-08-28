/**
 * SME CRM — nhúng form lead trên website ngoài.
 * Usage:
 *   <div id="sme-crm-lead" data-form-url="https://domain/lead"></div>
 *   <script src="https://domain/static/js/crm-lead-embed.js" defer></script>
 */
(function () {
  function boot() {
    var el = document.getElementById('sme-crm-lead');
    if (!el) return;
    var url = el.getAttribute('data-form-url') || el.getAttribute('data-url');
    if (!url) {
      el.innerHTML = '<p style="color:#b91c1c;font:14px/1.4 sans-serif">Thiếu data-form-url</p>';
      return;
    }
    var q = window.location.search || '';
    if (q && url.indexOf('?') === -1) url += q;
    else if (q) url += '&' + q.slice(1);

    var iframe = document.createElement('iframe');
    iframe.src = url;
    iframe.title = 'Form liên hệ';
    iframe.setAttribute('loading', 'lazy');
    iframe.style.cssText = 'width:100%;min-height:540px;border:0;border-radius:12px;background:#fff;';
    el.innerHTML = '';
    el.appendChild(iframe);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
