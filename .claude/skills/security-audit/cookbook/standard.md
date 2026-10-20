# Quy chuẩn kiểm tra bảo mật — project Spring Boot / JHipster (và front-end đi kèm)

Mỗi mục có **mã** (cùng mã với `security_scan.py`), **vì sao nguy hiểm**, **cách kiểm** (máy quét / người)
và **cách sửa**. Mức: 🔴 high (khai thác được / lộ secret — sửa trước khi merge) · 🟡 medium (điểm yếu,
cần sửa hoặc ghi lý do) · 🔵 low (vệ sinh code) · ⚪ info (danh sách cho người / agent soát, không chặn).

> Máy quét chỉ **tìm ứng viên** (regex + phân tích Spring). Một mục báo không chắc đã là lỗi, một mục
> không báo không chắc đã an toàn — nhất là **phân quyền theo đối tượng (BOLA)**, phải có người đọc code.

## Cách dùng

```
T=.claude/skills/security-audit/tools
python $T/security_scan.py scan --repo work/etask                    # báo cáo text, exit 1 nếu có lỗi HIGH MỚI
python $T/security_scan.py scan --repo work/etask --format md --out temp/etask-security.md
python $T/security_scan.py scan --repo work/etask --history           # + secret từng commit (300 commit gần nhất)
python $T/security_scan.py endpoints --repo work/etask --public       # endpoint công khai (permitAll)
python $T/security_scan.py endpoints --repo work/etask --ids          # endpoint nhận id, chỉ cần đăng nhập → soát BOLA
python $T/security_scan.py baseline --repo work/etask --note "hiện trạng 2026-10-20"   # chấp nhận lỗi cũ
python $T/security_scan.py rules                                     # danh sách mã
```
- **Baseline** (`<repo>/.agent/security-baseline.json`): lưu dấu vân tay các lỗi đã biết → các lần sau chỉ
  chặn lỗi **mới**. Dấu vân tay = mã + file + nội dung dòng + thứ tự xuất hiện của dòng giống hệt (dời dòng không mất; thêm một
  dòng y hệt = lỗi mới). Nên commit file này.
- **Bỏ qua có lý do** ngay tại dòng: `// security-audit: ignore INJ-001 tên bảng lấy từ enum cố định`.
- Không gửi gì ra ngoài; secret luôn bị che trong báo cáo (giữ 3 ký tự đầu) — ở **mọi** mã lỗi, không chỉ mã secret.
- Endpoint `@RequestMapping` không ghi method được xét **từng method** (GET/POST/PUT/PATCH/DELETE), lấy kết quả xấu nhất.

## Quy trình soát một project
1. `scan` → sửa mọi 🔴. Secret đã commit: **đổi/thu hồi trước**, xoá code sau (đã lộ là lộ).
2. `scan --history` một lần: secret trong lịch sử cũng là secret đã lộ.
3. `endpoints --public` → mỗi endpoint công khai phải có lý do + cơ chế xác thực khác (PAT, chữ ký webhook…).
4. `endpoints --ids` → với mỗi endpoint nhận id: đọc service, xác nhận có kiểm quyền trên **đúng đối tượng**.
5. Xử lý 🟡 hoặc ghi `security-audit: ignore` kèm lý do; chạy `baseline` khi đã chốt hiện trạng.
6. Từ đó: `scan` trước mỗi MR (exit 1 = có lỗi mới mức ≥ `--fail-on`; exit 3 = **quét chưa đủ** — file quá lớn/
   không đọc được hoặc `git log` lỗi: KHÔNG được coi là sạch; exit 2 = lỗi).

---

## 1. Phân quyền (AUTHZ) — lỗi phổ biến nhất
| Mã | Mức | Vấn đề | Cách sửa |
|---|---|---|---|
| AUTHZ-002 | 🔴 | Endpoint **không khớp luật nào** của `HttpSecurity` → Spring **cho qua** | Thêm luật cho đường dẫn; luật cuối `anyRequest().authenticated()` (hoặc `denyAll()`) |
| AUTHZ-004 | 🟡 | Endpoint **ghi/xoá** (POST/PUT/PATCH/DELETE) nằm trong `permitAll` | Chỉ webhook có chữ ký / PAT được công khai; còn lại bỏ khỏi `permitAll` |
| AUTHZ-001 | 🟡 | Endpoint đọc công khai | Xác nhận có chủ đích; có xác thực riêng (PAT, token chia sẻ) + rate-limit |
| AUTHZ-003 | ⚪ | Endpoint nhận **id đối tượng** chỉ cần đăng nhập (BOLA/IDOR) | Ở service: người gọi phải có quyền trên đúng đối tượng đó (thuộc project/tenant/được chia sẻ). Viết test "user B đọc task của project A → 403" |

**Kiểm BOLA bằng tay** (máy không làm thay được): với `GET /api/tasks/{id}`, đọc từ controller → service →
phải thấy lệnh kiểm `project/member/owner` trước khi trả dữ liệu; chú ý cả endpoint **list/search** (lọc theo
quyền), **export/download/attachment** (thường quên), và **id trong body** của PUT.
`@PreAuthorize("isAuthenticated()")` không phải kiểm quyền đối tượng.

## 2. Secret (SEC)
| Mã | Mức | Vấn đề | Cách sửa |
|---|---|---|---|
| SEC-001 | 🔴 | Private key trong repo | Thu hồi key, xoá khỏi repo + lịch sử, nạp từ vault/biến môi trường |
| SEC-002 | 🔴 | Token dạng đã biết (GitLab `glpat-`, GitHub, AWS `AKIA`, Slack, Google, OpenAI, **JWT `eyJ…`**) — kể cả trong Postman collection / tài liệu | Thu hồi; tài liệu dùng `{{token}}` |
| SEC-003 | 🔴 | Khoá cấu hình `*password*/*secret*/*token*/*api-key*…` có giá trị thật (YAML/properties/`.env`/JSON; **kể cả giá trị mặc định** trong `${VAR:mặc-định}`) | `${ENV_VAR}` không mặc định, Spring Cloud Config, Jasypt `ENC(...)`; đổi giá trị đã commit |
| SEC-004 | 🟡 | Secret gán cứng trong code Java/TS | Đọc từ cấu hình |
| SEC-005 | 🔴 | Hash mật khẩu mặc định JHipster (`admin/admin`, `user/user`) | Đổi mật khẩu / xoá user mẫu khỏi changelog chạy ở prod |
| HIST-* | 🔴 | Secret xuất hiện trong **lịch sử git** (`--history`) | Đổi/thu hồi; xoá lịch sử (`git filter-repo`) nếu repo được chia sẻ |

## 3. Code sinh sẵn / code thử còn sót (GEN) — "code tự sinh không xoá"
| Mã | Mức | Vấn đề | Cách sửa |
|---|---|---|---|
| GEN-001 | 🟡 | Controller `*Test/Demo/Debug/Sample/Mock*Resource` trong `src/main` | Xoá hoặc chuyển vào `src/test` / profile dev |
| GEN-005 | 🟡 | Mapping `/test`, `/debug`, `/demo`, `/tmp`… trong code chạy thật | Xoá; hoặc chỉ ADMIN + profile dev |
| GEN-004 | 🟡 | `liquibase.contexts` có **faker** ở cấu hình prod (nạp dữ liệu giả) | Bỏ `faker` khỏi context prod |
| GEN-003 | 🟡 | TODO/FIXME về auth/permission/bảo mật còn sót | Làm nốt hoặc ghi ticket |
| GEN-002 | 🔵 | `printStackTrace()` / `System.out` | Dùng logger; không trả stacktrace cho client |

Soát thêm bằng tay sau khi sinh code (JHipster, AI): entity/endpoint mẫu không dùng, `UserResource` mặc định
mở cho mọi user, gateway route thử, file `*.http`/Postman chứa token thật, `application-*.yml` thừa.

## 4. Cấu hình (CFG) — chỉ tính file chạy ở prod (`application.yml`, `application-prod*`, `bootstrap*`)
| Mã | Mức | Vấn đề | Cách sửa |
|---|---|---|---|
| CFG-001 | 🔴 | `management.endpoints.web.exposure.include: '*'` | Chỉ `health,info,prometheus`; `/management/**` ADMIN |
| CFG-002 | 🔴 | H2 console bật | Tắt ngoài dev |
| CFG-003 | 🟡 | Swagger / api-docs bật ở prod | Tắt hoặc chỉ ADMIN |
| CFG-008 / CFG-005 | 🟡 | CORS `*` (YAML / Java) | Liệt kê origin; không `*` cùng credentials |
| CFG-007 | 🟡 | Tắt kiểm chứng chỉ TLS (`verify-ssl: false`…) | Truststore có CA nội bộ |
| CFG-009 | 🟡 | Cờ insecure bật (`insecure-skip-tls-verify: true`…) | Tắt cờ; truststore có CA nội bộ |
| CFG-004 | 🔵 | `show-sql: true` ở prod | Tắt |
| CFG-006 | 🔵 | Log `DEBUG/TRACE` ở prod | INFO/WARN |

## 5. Injection & API nguy hiểm (Java)
| Mã | Mức | Vấn đề | Cách sửa |
|---|---|---|---|
| INJ-004 | 🔴 | SQL/JPQL nối với giá trị giống input (search, keyword, filter, **sort/orderBy**, param…) | Bind tham số; sort theo **whitelist cột** |
| INJ-001 | 🟡 | SQL/JPQL nối chuỗi với biến (tên bảng/cột động…) | Bind; tên động lấy từ enum/whitelist |
| INJ-002 | 🟡 | `Runtime.exec` / `ProcessBuilder` | Whitelist lệnh, mảng tham số |
| INJ-003 | 🟡 | SpEL / script từ chuỗi | `SimpleEvaluationContext`, không eval input |
| PATH-001 | 🟡 | Đường dẫn file ghép từ input (path traversal) | `normalize()` + kiểm `startsWith(base)` |
| SSRF-001 | 🔵 | Gọi URL lấy từ biến | Whitelist host |
| XXE-001 | 🟡 | XML parser chưa tắt DTD/entity ngoài | `FEATURE_SECURE_PROCESSING`, `disallow-doctype-decl` |
| DESER-001 | 🔴 | `ObjectInputStream`, Jackson **default typing** / `@JsonTypeInfo(use = CLASS)` | DTO cố định; nếu buộc phải: `PolymorphicTypeValidator` whitelist |
| CRYPTO-001 | 🟡 | MD5/SHA-1, DES/RC4/ECB | SHA-256+/bcrypt, AES-GCM |
| CRYPTO-002 | 🔵 | `java.util.Random` | `SecureRandom` cho token/mật khẩu |
| TLS-001 | 🔴 | Trust-all / bỏ kiểm hostname trong code | Truststore đúng |
| AUTH-001 | 🟡 | `@CrossOrigin` mở mọi nguồn | CORS tập trung |
| AUTH-002 | ⚪ | CSRF tắt | Bình thường với JWT stateless; bật nếu dùng cookie session |
| LOG-001 | 🟡 | Log password/token/cookie | Không log; mask |

## 6. Front-end (JS/TS/Vue/Angular)
| Mã | Mức | Vấn đề | Cách sửa |
|---|---|---|---|
| XSS-001 | 🟡 | `innerHTML`, `dangerouslySetInnerHTML`, `v-html`, `bypassSecurityTrust*` | Hiển thị text; sanitize (DOMPurify) |
| JS-001 | 🟡 | `eval` / `new Function` | Không eval chuỗi |
| JS-002 | 🔵 | Token trong `localStorage` | Cookie HttpOnly hoặc chống XSS chặt |

## Ngoài phạm vi máy quét (soát bằng tay / đợt sau)
- **Thư viện có CVE** (bản offline chưa kiểm — đợt sau có thể đối chiếu danh sách tải sẵn).
- Logic nghiệp vụ: đổi trạng thái vượt quyền, giới hạn số lần (rate-limit OTP/login), race condition.
- Upload: kiểm loại file theo nội dung, giới hạn kích thước, không phục vụ file upload cùng domain.
- Rò rỉ dữ liệu trong response (trả cả entity có hash mật khẩu, email người khác…).
