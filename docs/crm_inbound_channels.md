# Hub inbound — kênh 2–8 (endpoint theo kênh)

## Endpoint theo kênh

| Kênh | URL | Adapter |
|------|-----|---------|
| Website form | `POST /api/crm/public-lead` (không Token client) | website |
| Website / Make | `POST /api/crm/inbound/website` | website |
| Facebook | `POST /api/crm/inbound/facebook` | field_data / leadgen / Make flat |
| Facebook verify | `GET ...?hub.mode=subscribe&hub.verify_token=TOKEN&hub.challenge=...` | trả challenge |
| Zalo | `POST /api/crm/inbound/zalo` | OA sender/message + phone |
| Google | `POST /api/crm/inbound/google` | Lead Form user_column_data |
| TikTok | `POST /api/crm/inbound/tiktok` | data[].leads[] |
| WhatsApp | `POST /api/crm/inbound/whatsapp` | Cloud API entry/changes |
| Viber | `POST /api/crm/inbound/viber` | sender + message |
| Hotline | `POST /api/crm/inbound/hotline` | call_id / notes |
| Chung | `POST /api/crm/inbound-lead` | normalize source |

Header bắt buộc (trừ public-lead + GET verify): `X-CRM-Token: <token CRM Hub>`

Multi-tenant: thêm prefix `/{tenant_id}` trước `/api/...`.

## Sau mỗi lead thành công

1. Tạo lead CRM + round-robin owner (hoặc **dedup** nếu trùng `external_id`)  
2. Ghi activity trên lead + **thông báo** NV phụ trách  
3. Ghi `crm_inbound_logs`  
4. Tự tick kênh tương ứng trên Hub  
5. Hiện trên Dashboard (Inbound hôm nay) + Leads (lọc theo nguồn)

## Hotline thủ công

Trên Hub: form **Nhập Hotline / nguồn khác** — không cần Make (Hotline / Giới thiệu / Triển lãm).

## Make / n8n

Trên **CRM → Hub inbound**: bảng URL từng kênh + Copy curl / JSON.  
Trigger Ads → HTTP POST đúng URL kênh + Token.

**Hướng dẫn vận hành MXH (Facebook, Zalo, Google…):** [crm_inbound_mxh_guide.md](crm_inbound_mxh_guide.md)

## Nhúng website

```html
<div id="sme-crm-lead" data-form-url="https://DOMAIN/lead"></div>
<script src="https://DOMAIN/static/js/crm-lead-embed.js" defer></script>
```

Hoặc iframe tới `/lead`.

## Kiểm thử

- Hub: nút **Lead thử** / **Payload gốc** (FB field_data, TikTok batch)  
- `python scripts/test_crm_inbound_channels.py`
