---
name: security-audit
description: >
  Quy chuẩn kiểm tra bảo mật + máy quét offline cho project sản phẩm (Spring Boot / JHipster, front-end
  JS/TS): phân quyền endpoint (permitAll, không có luật, BOLA/IDOR), secret trong code/config/lịch sử git,
  code sinh sẵn/code thử còn sót, cấu hình prod nguy hiểm, injection, deserialize, crypto, XSS. Có baseline
  để chỉ chặn lỗi mới. Trigger phrases: "kiểm tra bảo mật", "security audit", "quét bảo mật", "security scan",
  "check lỗ hổng", "lỗi bảo mật", "soát BOLA", "IDOR", "secret trong code", "code tự sinh còn sót".
---

# Security Audit

Chuẩn: **`cookbook/standard.md`** (mã lỗi · vì sao · cách sửa · quy trình soát). Đọc nó trước khi soát.

## Tool (`tools/security_scan.py`) — stdlib, offline, không tốn token
```
python security_scan.py scan --repo <dir> [--history] [--format text|md|json] [--out F] [--fail-on high|medium|low] [--include-info]
python security_scan.py endpoints --repo <dir> [--public|--unprotected|--ids] [--format json]
python security_scan.py baseline --repo <dir> [--note "..."]      # [WRITE] ghi <repo>/.agent/security-baseline.json
python security_scan.py rules
```
Exit `scan`: 0 sạch (không lỗi MỚI ≥ fail-on) · 1 có · 2 lỗi chạy (KHÔNG coi là sạch).

## Quy tắc khi agent soát
1. Chạy `scan` (và `scan --history` lần đầu). Không in giá trị secret ra ngoài báo cáo đã che.
2. Mỗi 🔴: xác minh trong code rồi đề xuất sửa cụ thể (file:dòng). Secret đã commit: báo người dùng **đổi/thu hồi** —
   agent không tự đổi secret, không tự viết lại lịch sử git.
3. `endpoints --public` và `--ids`: đọc service để xác nhận kiểm quyền đối tượng; ghi rõ "đã kiểm / chưa rõ".
4. Không tự chạy `baseline` khi chưa được người dùng đồng ý (nó chấp nhận lỗi hiện tại).
5. Kết quả là **ứng viên**: nói rõ mục nào đã xác minh, mục nào là nghi ngờ.
