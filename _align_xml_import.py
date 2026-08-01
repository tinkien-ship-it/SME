# -*- coding: utf-8 -*-
"""Align import_sme.html XML flow with original import.html (git HEAD)."""
import subprocess
from pathlib import Path

orig = subprocess.check_output(
    ["git", "show", "HEAD:templates/import.html"],
    cwd=r"C:\SME",
).decode("utf-8")

# Original markup
orig_markup = '''                    <div class="input-group input-group-sm">
                        <label class="input-group-text btn btn-primary fw-bold text-white" for="xmlFile">Chọn file</label>
                        <input type="file" id="xmlFile" class="form-control" accept=".xml" style="display: none;">
                        <span id="fileNameDisplay" class="form-control bg-white text-muted" style="line-height: 24px;">Chưa có file được chọn</span>
                        <button class="btn btn-primary fw-bold" type="button" id="btnUploadXML">TỰ ĐỘNG LẬP</button>
                    </div>'''

# Original handleXMLUpload from git
i = orig.find("async function handleXMLUpload()")
j = orig.find("async function updateSupplierFromXML")
k = orig.find("async function loadInvoiceFromServer")
handle_fn = orig[i:j]
update_fn = orig[j:k]
# loadInvoice from git until getConsolidatedItems
m = orig.find("function getConsolidatedItems")
load_inv = orig[k:m]

sme = Path(r"C:\SME\templates\KeToanSME\import_sme.html").read_text(encoding="utf-8")

# 1) Fix markup
old_markup_start = sme.find('<div class="input-group input-group-sm')
old_markup_end = sme.find("</div>", sme.find("btnUploadXML", old_markup_start)) + len("</div>")
# better: find the whole input-group block
import re
sme2, n = re.subn(
    r'<div class="input-group input-group-sm(?: xml-file-picker)?">[\s\S]*?</div>\s*</div>\s*</div>\s*</div>\s*</div>',
    orig_markup + "\n                </div>\n            </div>\n        </div>\n    </div>",
    sme,
    count=1,
)
if n != 1:
    # fallback simpler replace of inner group only
    sme2, n = re.subn(
        r'(<div class="col-md-5">\s*)<div class="input-group input-group-sm[^"]*">[\s\S]*?</div>(\s*</div>)',
        r"\1" + orig_markup + r"\2",
        sme,
        count=1,
    )
    if n != 1:
        raise SystemExit(f"markup replace failed n={n}")
sme = sme2

# 2) Replace from KHỞI TẠO through loadInvoiceFromServer (before getConsolidatedItems)
start = sme.find("// ====================== KHỞI TẠO ======================")
if start < 0:
    start = sme.find("function initImportPage()")
end = sme.find("function getConsolidatedItems()")
if start < 0 or end < 0:
    raise SystemExit(f"js markers missing {start} {end}")

# Keep loadPurchaseOrder if it exists before KHỞI TẠO - it should stay before
# Currently loadPurchaseOrder is AFTER init in sme - need to check
# In current sme: init... then lots of client parse... then updateSupplier, loadInvoice, getConsolidated
# loadPurchaseOrder is before KHỞI TẠO in current file? Check...
# From earlier read: loadPurchaseOrder is AFTER initImportPage call area - actually between
# Looking at earlier: loadPurchaseOrder starts at line 677 after bindXmlFilePicker was removed to after init
# Wait - in current file structure after my patches:
# initImportPage, safeToast, bind..., client parse..., handleXMLUpload, updateSupplier, loadInvoice, getConsolidated
# AND loadPurchaseOrder was moved - grep said it's at 677 area which got overwritten?

# Check if loadPurchaseOrder still exists
has_po = "async function loadPurchaseOrder" in sme[:start] or "async function loadPurchaseOrder" in sme[end:]
po_fn = ""
po_i = sme.find("async function loadPurchaseOrder")
if po_i >= 0 and po_i < end:
    # extract until next major section (KHỞI TẠO or XML or getConsolidated)
    po_end = sme.find("// ======================", po_i + 10)
    if po_end < 0 or po_end > end:
        po_end = sme.find("function getConsolidatedItems()", po_i)
    # if loadPurchaseOrder is inside the block we're replacing, save it
    if start <= po_i < end:
        # find end of loadPurchaseOrder function - next async function or // ===
        nxt = sme.find("\nasync function ", po_i + 5)
        nxt2 = sme.find("\nfunction ", po_i + 5)
        candidates = [c for c in (nxt, nxt2, end) if c and c > po_i]
        po_end = min(candidates) if candidates else end
        po_fn = sme[po_i:po_end].rstrip() + "\n\n"
        print("saved loadPurchaseOrder", len(po_fn))

new_js = '''// ====================== KHỞI TẠO ======================
document.addEventListener('DOMContentLoaded', () => {
    flatpickr('#importDate', { dateFormat: 'd/m/Y', defaultDate: 'today', locale: 'vn' });
    flatpickr('#bill_date', { dateFormat: 'd/m/Y', locale: 'vn', allowInput: true });

    fetch('/api/import/next_sequence', { method: 'POST' })
        .then(res => res.json())
        .then(d => {
            if (d.success) {
                document.getElementById('import_no').value = d.next_no;
                document.getElementById('import_no_val').textContent = d.next_no;
            }
        });

    loadQuySoDu();

    const btnUploadXML = document.getElementById('btnUploadXML');
    if (btnUploadXML) btnUploadXML.addEventListener('click', handleXMLUpload);

    const xmlFileInput = document.getElementById('xmlFile');
    if (xmlFileInput) {
        xmlFileInput.addEventListener('change', function() {
            const display = document.getElementById('fileNameDisplay');
            const file = this.files?.[0];
            if (!file) return;
            if (!file.name.toLowerCase().endsWith('.xml')) {
                this.value = '';
                display.textContent = 'Chưa có file được chọn';
                return toast('Chỉ hỗ trợ file XML của hóa đơn điện tử!', 'error');
            }
            display.textContent = file.name;
            handleXMLUpload();
        });
    }

    const urlParams = new URLSearchParams(window.location.search);
    const invoiceId = urlParams.get('invoice_id');
    const poId = urlParams.get('po_id');
    if (invoiceId) loadInvoiceFromServer(invoiceId);
    else if (poId) loadPurchaseOrder(poId);
    else addRow();

    togglePaymentMethod();
});

''' + (po_fn if po_fn else "") + handle_fn + update_fn + load_inv

# If loadPurchaseOrder wasn't inside block, keep whatever is before start (may already have it)
# Remove duplicate loadPurchaseOrder before start if we're inserting it again
before = sme[:start]
if po_fn and "async function loadPurchaseOrder" in before:
    # remove old copy before start
    po_b = before.rfind("async function loadPurchaseOrder")
    if po_b >= 0:
        # also remove comment before it
        cut = before.rfind("// ======================", 0, po_b)
        if cut >= 0 and "XML" in before[cut:po_b]:
            before = before[:cut]
        else:
            # remove from po_b back to previous blank line section
            before = before[:po_b]

sme = before + new_js + sme[end:]

# Remove trailing initImportPage call if any
sme = sme.replace(
    """
// Khởi chạy trang (gắn listener chọn XML + dòng hàng)
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initImportPage);
} else {
    initImportPage();
}
""",
    "\n",
)

Path(r"C:\SME\templates\KeToanSME\import_sme.html").write_text(sme, encoding="utf-8")
print("import_sme.html aligned with import.html XML logic")

# Also restore import.html markup + handlers to git HEAD originals for consistency
imp = Path(r"C:\SME\templates\import.html").read_text(encoding="utf-8")
imp2, n2 = re.subn(
    r'(<div class="col-md-5">\s*)<div class="input-group input-group-sm">[\s\S]*?</div>(\s*</div>)',
    r"\1" + orig_markup + r"\2",
    imp,
    count=1,
)
if n2 == 1:
    # restore DOMContentLoaded xml bind + handleXMLUpload from git
    # Replace current handle through updateSupplier with originals
    hi = imp2.find("async function handleXMLUpload()")
    ui = imp2.find("async function updateSupplierFromXML")
    li = imp2.find("async function loadInvoiceFromServer")
    if hi > 0 and ui > hi and li > ui:
        # keep loadInvoice as in current or use git? use git for handle+update only
        imp2 = imp2[:hi] + handle_fn + update_fn + imp2[li:]
    # restore bind in DOMContentLoaded - replace btnChoose block with original
    old_bind = """    const btnChooseXML = document.getElementById('btnChooseXML');
    const btnUploadXML = document.getElementById('btnUploadXML');
    if (btnUploadXML) btnUploadXML.addEventListener('click', handleXMLUpload);

    const xmlFileInput = document.getElementById('xmlFile');
    if (xmlFileInput && xmlFileInput.dataset.bound !== '1') {
        xmlFileInput.dataset.bound = '1';
        xmlFileInput.addEventListener('change', function() {
            const display = document.getElementById('fileNameDisplay');
            const file = this.files && this.files[0];
            if (!file) return;
            if (!/\\.xml$/i.test(file.name)) {
                this.value = '';
                if (display) display.textContent = 'Chưa có file được chọn';
                return toast('Chỉ hỗ trợ file XML của hóa đơn điện tử!', 'error');
            }
            if (display) display.textContent = file.name;
            handleXMLUpload();
        });
    }
"""
    # also try current bind variants
    bind_orig = """    const btnUploadXML = document.getElementById('btnUploadXML');
    if (btnUploadXML) btnUploadXML.addEventListener('click', handleXMLUpload);

    const xmlFileInput = document.getElementById('xmlFile');
    if (xmlFileInput) {
        xmlFileInput.addEventListener('change', function() {
            const display = document.getElementById('fileNameDisplay');
            const file = this.files?.[0];
            if (!file) return;
            if (!file.name.toLowerCase().endsWith('.xml')) {
                this.value = '';
                display.textContent = 'Chưa có file được chọn';
                return toast('Chỉ hỗ trợ file XML của hóa đơn điện tử!', 'error');
            }
            display.textContent = file.name;
            handleXMLUpload();
        });
    }
"""
    # find from btnUploadXML or btnChooseXML to extraCostEl
    bi = imp2.find("const btnChooseXML")
    if bi < 0:
        bi = imp2.find("const btnUploadXML = document.getElementById('btnUploadXML')")
    ei = imp2.find("const extraCostEl", bi)
    if bi > 0 and ei > bi:
        imp2 = imp2[:bi] + bind_orig + "\n" + imp2[ei:]
    Path(r"C:\SME\templates\import.html").write_text(imp2, encoding="utf-8")
    print("import.html restored to original XML logic")
else:
    print("import.html markup not changed n=", n2)
