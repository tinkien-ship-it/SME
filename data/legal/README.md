# Tài liệu pháp lý — Kế toán Doanh nghiệp (SME)

Nguồn do bạn cung cấp từ máy local. Bản gốc giữ nguyên; bản text UTF-8 dùng để AI/lập trình tham chiếu khi triển khai module SME (tách biệt hoàn toàn với HKD).

## Cấu trúc thư mục

| Đường dẫn | Nội dung |
|---|---|
| `tt58_2026/` | Bản gốc `.docx` Thông tư 58/2026/TT-BTC |
| `tt99_2025/` | Bản gốc `.doc` các phần Công báo Thông tư 99/2025/TT-BTC |
| `text/` | Bản text UTF-8 đã trích bằng Microsoft Word (để tìm kiếm / RAG / thiết kế) |

## Thông tư 58/2026/TT-BTC — Doanh nghiệp siêu nhỏ

- File text: `text/TT58_2026_TT-BTC.txt` (~58k ký tự, đọc tốt)
- Ban hành: 25/05/2026 · Hiệu lực: **01/07/2026** · Thay: TT132/2018
- Phạm vi: chứng từ, ghi sổ, BCTC DN siêu nhỏ (thuế theo pháp luật thuế)
- Đối tượng: DN siêu nhỏ; **HKD/cá nhân kinh doanh được chọn áp dụng** nếu có nhu cầu
- Ghi sổ theo **4 phương pháp nộp thuế** (Điều 5–8), ví dụ Điều 5 → mẫu **S1-DNSN**
- BCTC theo Điều 10 (bắt buộc chủ yếu khi TNDN theo thu nhập tính thuế)

## Thông tư 99/2025/TT-BTC — Chế độ kế toán doanh nghiệp

Các file Công báo ghép theo số trang (1575+1576 là file ảnh lớn ~27MB — text trích được ít, chủ yếu bảng/ảnh):

| File text | Phần gốc | Nội dung nhận diện |
|---|---|---|
| `TT99_2025_1563_1564_…txt` | 1563+1564 | **Thân Thông tư** — Chương I…, chứng từ / TK / sổ / BCTC |
| `TT99_2025_1565_1566_…txt` | 1565+1566 | **Phụ lục II — Hệ thống tài khoản** (+ tiếp chứng từ) |
| `TT99_2025_1567_1568_…txt` | 1567+1568 | Tiếp hướng dẫn tài khoản / nghiệp vụ |
| `TT99_2025_1569_1570_…txt` | 1569+1570 | Tiếp hướng dẫn tài khoản / nghiệp vụ |
| `TT99_2025_1571_1572_…txt` | 1571+1572 | Tiếp hướng dẫn tài khoản / nghiệp vụ |
| `TT99_2025_1573_1574_…txt` | 1573+1574 | Tiếp hướng dẫn tài khoản / nghiệp vụ |
| `TT99_2025_1575_1576_…txt` | 1575+1576 | **Phụ lục III — Sổ kế toán** (nhiều ảnh → text mỏng) |
| `TT99_2025_1577_1578_…txt` | 1577+1578 | Tiếp mẫu sổ kế toán |
| `TT99_2025_1579_1580_…txt` | 1579+1580 | **Báo cáo tài chính** (Phụ lục IV) |
| `TT99_2025_1581_1582_…txt` | 1581+1582 | Tiếp BCTC (vd. lưu chuyển tiền tệ phương pháp trực tiếp) |

Hiệu lực thân thông tư: **01/01/2026**, áp dụng năm tài chính bắt đầu từ/sau ngày đó.

## Cách dùng khi code SME

1. Không sửa / không gọi `Services/hkd_*` từ module SME.
2. Đọc file trong `text/` khi cần số hiệu tài khoản, nguyên tắc hạch toán, cấu trúc mẫu sổ/BCTC.
3. File `.doc` gốc giữ để đối chiếu khi text thiếu (đặc biệt Phụ lục III dạng ảnh).
4. Có thể bổ sung sau: Excel hệ thống tài khoản tách riêng nếu bạn xuất từ Phụ lục II — giúp seed `chart_of_accounts` nhanh và chính xác hơn.

## Lưu ý git

Các file `.doc` gốc khá nặng (một file ~27MB). Nên **commit bản `text/`** và cân nhắc không đẩy nhị phân lớn lên remote, hoặc dùng Git LFS nếu cần giữ bản gốc trong repo.
