# Hướng dẫn kết nối mạng xã hội → CRM Hub inbound

Mục tiêu: khi khách để lại form / chat / Lead Ads trên Facebook, Zalo, Google, TikTok, WhatsApp, Viber… hệ thống SME **tự tạo lead**, ghi đúng **nguồn**, chia **NV bán hàng**, và hiện trên **Leads / Dashboard**.

> SME **không tự “kéo”** danh sách fanpage. Bạn lấy **URL webhook theo kênh** trên Hub CRM rồi dán vào nền tảng MXH (hoặc Make/n8n) để nền tảng **đẩy** lead về SME.

---

## Bước chung (bắt buộc trước mọi kênh)

1. Mở **CRM → Hub inbound lead** (`/crm/inbound`).
2. Copy **Token** (ô Token / `X-CRM-Token`).
3. Ở bảng **Endpoint theo kênh**, Copy đúng URL kênh cần dùng, ví dụ:
   - Facebook → `…/api/crm/inbound/facebook`
   - Zalo → `…/api/crm/inbound/zalo`
4. Đảm bảo có user role **Nhân Viên Bán Hàng** (`staff`) — lead mới mới được round-robin.
5. Sau khi cấu hình xong: bấm **Lead thử** / **Payload gốc** trên Hub → kiểm tra **Nhật ký inbound** và trang **Leads**.

**Multi-tenant:** URL phải có mã sổ, ví dụ `https://domain.com/{tenant}/api/crm/inbound/facebook`.

**Bảo mật:** Token chỉ đặt trên Make/n8n / cấu hình webhook server — **không** nhúng Token vào JavaScript website công khai. Form Website dùng `/lead` (Token ở server SME).

---

## 1. Website (form SME hoặc site riêng)

| Việc | Cách làm |
|------|----------|
| Form sẵn của SME | Hub → bật **Form công khai** → chia sẻ link `/lead` (hoặc `/{tenant}/lead`) |
| Nhúng site | Copy **Snippet nhúng** trên Hub (iframe / `crm-lead-embed.js`) |
| Ads Google → landing | Dùng URL form kèm UTM mẫu trên Hub (`utm_source=google&…`) |
| Form tự host | Make/n8n: khi submit → `POST` URL kênh `website` + header Token |

CRM nhận biết: `source = Website`, UTM lưu trên lead.

---

## 2. Facebook / Instagram Lead Ads (Meta)

### Cách A — Make.com / n8n (khuyên dùng cho SME)

1. Meta Business: tạo **Lead Form** trên Fanpage / Instagram (bắt buộc có SĐT).
2. Make: scenario **Facebook Lead Ads → Watch Leads** (chọn Page + Form).
3. Thêm module **HTTP → Make a request**:
   - Method: `POST`
   - URL: copy từ Hub dòng **facebook**
   - Header: `X-CRM-Token: <Token Hub>` + `Content-Type: application/json`
   - Body map: `full_name` / `phone` → hoặc để Make gửi nguyên payload; adapter SME hiểu `field_data` Meta.
4. Bật scenario → gửi 1 lead thử trên form Ads.

### Cách B — Webhook trực tiếp Meta App

1. [developers.facebook.com](https://developers.facebook.com) → App → **Webhooks** → product **Page**.
2. **Callback URL** = URL Hub `…/api/crm/inbound/facebook` (HTTPS công khai).
3. **Verify Token** = **cùng Token** trên Hub CRM (SME trả `hub.challenge` khi Meta gọi GET).
4. Subscribe field **leadgen** (và quyền `leads_retrieval` nếu App tự lấy chi tiết lead).
5. Nếu webhook chỉ gửi `leadgen_id` mà thiếu SĐT: dùng Make (Cách A) hoặc Graph API lấy lead fields rồi POST về SME.

CRM nhận biết: URL kênh `facebook` → `source = Facebook`, `external_id = leadgen_id` (dedup nếu Ads gửi lại).

---

## 3. Zalo Official Account (OA)

1. Zalo OA / Zalo Cloud / tool chatbot đang dùng phải **bắt được SĐT** (form, nút “Để lại SĐT”, kịch bản bot).
2. Cấu hình webhook OA hoặc Make:
   - Trigger: tin nhắn / form có phone
   - HTTP POST → URL Hub **zalo** + header Token
3. Map tối thiểu: `phone`, `name` (hoặc `sender.name`), `notes` = nội dung chat ngắn, `external_id` = user/msg id Zalo.

CRM nhận biết: kênh `zalo` → `source = Zalo`.

> Zalo thường **không** cho SME “đọc hết hội thoại” nếu không qua OA API / đối tác. Luồng chuẩn là **đẩy lead khi có SĐT**.

---

## 4. Google Ads (Landing + Lead Form)

### Landing về form SME (đơn giản nhất)

1. Copy link form `/lead` trên Hub.
2. Trong Google Ads: Final URL = link đó + UTM (`utm_source=google&utm_medium=cpc&utm_campaign=…` — mẫu có sẵn trên Hub).
3. Lead vào CRM với `source = Website` hoặc gắn campaign; pie nguồn vẫn theo dõi UTM.

### Lead Form Extension / Google Lead Form

1. Make: trigger Google Lead Form / Sheets khi có dòng mới.
2. POST URL Hub **google** + Token.
3. Adapter nhận `user_column_data` (FULL_NAME, PHONE_NUMBER…) hoặc JSON flat `contact_name` / `phone`.

CRM nhận biết: kênh `google` → `source = Google`.

---

## 5. TikTok Lead Generation

1. TikTok Ads Manager: bật form Lead Generation (tên + SĐT).
2. Make/n8n: trigger TikTok Lead → HTTP POST URL Hub **tiktok** + Token.
3. Adapter hiểu batch `data[].leads[]` (payload gốc TikTok) hoặc JSON flat.

CRM nhận biết: kênh `tiktok` → `source = TikTok`, `external_id = lead_id`.

---

## 6. WhatsApp Business

1. Dùng WhatsApp Cloud API / ManyChat / tool flow bắt buộc khách gửi **tên + SĐT**.
2. Make hoặc webhook Cloud API:
   - POST URL Hub **whatsapp** + Token
   - Adapter đọc dạng Meta Cloud: `entry[].changes[].value.contacts/messages`
3. Verify webhook (nếu Meta yêu cầu GET): dùng cùng Token Hub như Facebook (endpoint whatsapp).

CRM nhận biết: kênh `whatsapp` → `source = WhatsApp`.

---

## 7. Viber

1. Chỉ kết nối khi đội sale đã dùng Viber Business / bot.
2. Bot/Make khi có SĐT → POST URL Hub **viber** + Token (`sender`, `message`, `phone`).

CRM nhận biết: kênh `viber` → `source = Viber`.

---

## 8. Hotline / giới thiệu / triển lãm

- Không có “link MXH”: trên Hub dùng form **Nhập Hotline / nguồn khác**, hoặc tổng đài CTI POST URL **hotline**.
- Nguồn: Hotline · Giới thiệu · Triển lãm · Khác.

---

## Làm sao CRM “biết” tài khoản / kênh tương tác?

| Cơ chế | Ý nghĩa |
|--------|---------|
| **URL theo kênh** | `…/inbound/facebook` khác `…/inbound/zalo` → adapter + `source` đúng kênh |
| **Token** | Chỉ webhook của bạn (có Token) mới tạo được lead |
| **external_id** | ID lead Ads / msg id — chống trùng khi nền tảng gửi lại |
| **UTM** | Biết chiến dịch Ads (đặc biệt Google → `/lead`) |
| **owner** | Round-robin NV Bán hàng; thông báo CRM + activity trên lead |

SME **không thay** hộp thư Messenger/Zalo đầy đủ. Sau khi có lead, NV chăm trên **Leads → Chuyển KH → 360°** (ghi gọi/Zalo/email).

---

## Kiểm tra đã kết nối đúng

1. Hub → **Lead thử** / **Payload gốc** theo kênh → status `ok` trong nhật ký.
2. **Leads** → lọc **Nguồn** = Facebook / Zalo / …
3. **Tổng quan** → ô **Inbound hôm nay** + thông báo NV phụ trách.
4. Gửi 1 lead thật từ Ads/OA → cùng luồng.

Sự cố thường gặp: sai Token, dùng URL thiếu `/{tenant}`, form Ads thiếu SĐT, Make chưa bật scenario, firewall chặn IP Make/Meta.
