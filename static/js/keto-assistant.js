(function () {
    'use strict';

    const cfg = window.KETO_SUPPORT || {};
    if (!cfg.enabled) return;

    const panel = document.getElementById('ketoAssistPanel');
    const launcher = document.getElementById('ketoAiFab');
    const closeBtn = document.getElementById('ketoAssistClose');
    const messagesEl = document.getElementById('ketoAssistMessages');
    const chipsEl = document.getElementById('ketoAssistChips');
    const inputEl = document.getElementById('ketoAssistInput');
    const sendBtn = document.getElementById('ketoAssistSend');
    const tabAi = document.getElementById('ketoTabAi');
    const tabZalo = document.getElementById('ketoTabZalo');
    const aiPane = document.getElementById('ketoPaneAi');
    const zaloPane = document.getElementById('ketoPaneZalo');

    if (!panel || !launcher) return;

    const page = cfg.page || '';
    let busy = false;

    if (cfg.premiumActive && launcher) {
        launcher.classList.add('premium');
    }

    function escHtml(s) {
        return String(s || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function mdLite(text) {
        return escHtml(text).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    }

    function collectContext() {
        const activeForm = document.querySelector('form:focus-within')
            || document.querySelector('.modal.show form')
            || document.querySelector('main form, .accounting-hub-content form, form');
        return {
            page: page,
            path: window.location.pathname,
            page_title: document.title,
            form_id: activeForm ? (activeForm.id || activeForm.getAttribute('name') || '') : '',
            screen_hint: document.body.dataset.assistScreen || '',
            regime: cfg.regime || '',
            role: cfg.role || '',
        };
    }

    function appendMsg(role, text) {
        const div = document.createElement('div');
        div.className = 'keto-assist-msg ' + role;
        div.innerHTML = mdLite(text);
        messagesEl.appendChild(div);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function setTyping(on) {
        let el = document.getElementById('ketoAssistTyping');
        if (on) {
            if (!el) {
                el = document.createElement('div');
                el.id = 'ketoAssistTyping';
                el.className = 'keto-assist-typing';
                el.innerHTML = '<i class="fas fa-circle-notch fa-spin me-1"></i>Đang trả lời...';
                messagesEl.appendChild(el);
            }
        } else if (el) {
            el.remove();
        }
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function renderChips(items) {
        if (!chipsEl) return;
        chipsEl.innerHTML = (items || []).map(q =>
            `<button type="button" class="keto-assist-chip" data-q="${escHtml(q)}">${escHtml(q)}</button>`
        ).join('');
        chipsEl.querySelectorAll('.keto-assist-chip').forEach(btn => {
            btn.addEventListener('click', () => sendMessage(btn.dataset.q));
        });
    }

    async function loadSuggestions() {
        try {
            const res = await fetch('/api/assistant/suggestions?page=' + encodeURIComponent(page));
            const json = await res.json();
            if (json.success) renderChips(json.suggestions);
        } catch (_) { /* bỏ qua */ }
    }

    async function sendMessage(text) {
        const msg = (text || inputEl.value || '').trim();
        if (!msg || busy) return;
        busy = true;
        sendBtn.disabled = true;
        appendMsg('user', msg);
        inputEl.value = '';
        setTyping(true);
        try {
            const res = await fetch('/api/assistant/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(Object.assign({ message: msg }, collectContext())),
            });
            const json = await res.json();
            setTyping(false);
            if (!json.success) throw new Error(json.error || 'Lỗi trợ lý');
            const reply = json.reply || {};
            appendMsg('bot', reply.text || 'Không có phản hồi.');
            if (reply.needs_escalation) {
                appendMsg('bot', 'Bạn có thể nhắn Zalo ' + (cfg.zaloPhone || '0908870287') + ' để nhân viên hỗ trợ trực tiếp.');
            }
            if (reply.help_url && reply.source === 'fallback') {
                appendMsg('bot', 'Xem thêm: Hướng Dẫn Sử Dụng trong menu Kế Toán HKD.');
            }
        } catch (e) {
            setTyping(false);
            appendMsg('bot', 'Không kết nối được trợ lý. Nhắn Zalo ' + (cfg.zaloPhone || '0908870287') + ' để được hỗ trợ.');
        } finally {
            busy = false;
            sendBtn.disabled = false;
            inputEl.focus();
        }
    }

    function openPanel(tab) {
        panel.hidden = false;
        panel.classList.add('open');
        switchTab(tab || 'ai');
        if (messagesEl.children.length === 0) {
            let greet = 'Xin chào! Tôi là **Trợ lý KETO POS** — hướng dẫn bán hàng, kho, kế toán HKD và hóa đơn điện tử.';
            if (cfg.regime) greet += '\n\nLoại hình: **' + cfg.regime + '**.';
            if (cfg.premiumActive) {
                greet += '\n\nChế độ **AI Pro** (OpenAI) — trả lời linh hoạt hơn.';
            } else {
                greet += '\n\nChế độ **miễn phí**: FAQ + tài liệu hướng dẫn + gợi ý theo màn hình.';
                if (cfg.openaiAvailable) {
                    greet += '\n\nQuản trị viên có thể bật **AI Pro** trong Master Settings → Trợ lý AI.';
                }
            }
            greet += '\n\nChọn câu hỏi gợi ý hoặc gõ câu hỏi của bạn.';
            appendMsg('bot', greet);
            loadSuggestions();
        }
    }

    function closePanel() {
        panel.classList.remove('open');
        panel.hidden = true;
    }

    function switchTab(name) {
        const isAi = name === 'ai';
        tabAi.classList.toggle('active', isAi);
        tabZalo.classList.toggle('active', !isAi);
        aiPane.style.display = isAi ? '' : 'none';
        zaloPane.style.display = isAi ? 'none' : '';
    }

    launcher.addEventListener('click', () => {
        if (panel.classList.contains('open')) closePanel();
        else openPanel('ai');
    });
    if (closeBtn) closeBtn.addEventListener('click', closePanel);
    tabAi.addEventListener('click', () => switchTab('ai'));
    tabZalo.addEventListener('click', () => switchTab('zalo'));
    sendBtn.addEventListener('click', () => sendMessage());
    inputEl.addEventListener('keydown', e => {
        if (e.key === 'Enter') sendMessage();
    });

    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') closePanel();
    });
})();
