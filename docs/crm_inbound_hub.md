# Hub inbound lead — vận hành đa kênh → SME CRM

## Mục tiêu

Một webhook SME nhận lead từ:

Website · Facebook Ads · Zalo · Google · TikTok · WhatsApp · Viber · Hotline · khác

## Trong SME (đã triển khai)

| Thành phần | Đường dẫn |
|------------|-----------|
| Hub vận hành theo kênh | `/crm/inbound` |
| Webhook theo kênh (FB/Zalo/…) | `POST /api/crm/inbound/<channel>` + `X-CRM-Token` — chi tiết [crm_inbound_channels.md](crm_inbound_channels.md) |
| Webhook chung (Make flat) | `POST /api/crm/inbound-lead` + header `X-CRM-Token` |
| Form Website công khai (không lộ Token) | `GET /lead` → `POST /api/crm/public-lead` |
| Multi-tenant | `/{tenant_id}/lead`, `/{tenant_id}/api/crm/...` |

## Quy trình theo kênh

1. **Website** — Bật form tại Hub → chia sẻ `/lead` hoặc gắn Ads kèm UTM → bấm «Lead thử».
2. **Facebook Lead Ads** — Make: New Lead → HTTP POST Endpoint + Token; `source=Facebook`.
3. **Zalo OA** — Bot/Make khi có SĐT → POST `source=Zalo`.
4. **Google Ads** — Landing = `/lead?utm_source=google&...` hoặc Lead Form → Make `source=Google`.
5. **TikTok Lead Ads** — Make tương tự FB, `source=TikTok`.
6. **WhatsApp** — Flow thu SĐT → Make POST `source=WhatsApp`.
7. **Viber** — Chỉ khi kênh đang dùng → `source=Viber`.
8. **Hotline / khác** — POST `source=Hotline` / `Giới thiệu` / `Triển lãm`.

Mỗi kênh trên Hub có: checklist, curl mẫu, JSON Make, nút **Lead thử**, checkbox đánh dấu xong.

**Hướng dẫn người dùng (lấy URL kênh + gắn MXH):** xem [crm_inbound_mxh_guide.md](crm_inbound_mxh_guide.md) — cũng hiển thị trên trang Hub inbound.

## Payload chuẩn

```json
{
  "contact_name": "Nguyễn A",
  "phone": "0901234567",
  "email": "a@email.com",
  "company_name": "Cty ABC",
  "source": "Facebook",
  "utm_source": "fb",
  "utm_medium": "paid",
  "utm_campaign": "spring",
  "external_id": "fb_lead_123",
  "notes": "Quan tâm báo giá"
}
```

Alias được chuẩn hóa tự động (`fb`→Facebook, `zalo_oa`→Zalo, `whatsapp`→WhatsApp…).

## Bảo mật

- Token chỉ dùng trên **server Make/n8n** hoặc webhook có auth — không nhúng JS website.
- Form `/lead` gọi `/api/crm/public-lead` (Token ở server SME).
- Có honeypot chống bot trên form công khai.

## Điều kiện chia lead

Settings → Users → role **Nhân Viên Bán Hàng** (`staff`). Round-robin theo thứ tự trên Hub / Cấu hình CRM.
