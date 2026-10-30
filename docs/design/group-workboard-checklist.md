# Bảng việc nhóm FPT Chat — checklist theo dõi

Thiết kế: [`group-workboard.md`](group-workboard.md) (v3, Codex + Fable ACCEPT). Tiến độ kỹ thuật chi tiết:
[`CHECKLIST.md` §8](CHECKLIST.md). Extension: `D:\Claude-cli\fpt-chat-quick-reply` (ngoài git; `HANDOFF.md`,
`PROTOCOL.md`). Nhóm thử: **New Group** `6883495a299a0f54343608e3`.

Ký hiệu: ✅ xong · 🟡 đang làm / xong một phần · ⬜ chưa làm · ⛔ bị chặn · 👤 việc của BẠN.

---

## A. Việc của bạn (bot không tự làm được)

### A1. Bật những gì đã code xong (chưa chạy thật ở đâu)
- [ ] 👤 Cài lại extension trong Chrome (`chrome://extensions` → Load unpacked / Reload) → v1.7.0.
- [ ] 👤 Đặt `FCHAT_BOARD_GROUPS=6883495a299a0f54343608e3` (dashboard → Cài đặt, hoặc
      `python agentkit.py toolkit_settings set FCHAT_BOARD_GROUPS 6883495a299a0f54343608e3`).
- [ ] 👤 Restart tab `group_watch` trên ubuntu-c (đổi `.env` không tự nạp lại; commit mới thì tự nạp).
- [ ] 👤 (tuỳ chọn) Thêm watchdog: lệnh supervise của tab `group_watch` thêm
      `--heartbeat group_watch-6883495a299a0f54343608e3 --stale-after 180`.
- [ ] 👤 Thử trong nhóm: gửi `@bot ping`, `@bot tôi là ai`, một link MR → xem thẻ R-n ra sau ≤ 3′ hoặc kèm kết quả.
- [ ] 👤 Sau 1–2 ngày thấy ổn: bật báo lệnh bị lỡ `FCHAT_CATCHUP_REPLY=1` (hiện chỉ ghi log).
- [ ] 👤 Bật bản đồ môi trường: `FCHAT_ENV_REPOS=etask` (thêm repo khác khi đã khai `envmap` — mục A4). Prod ẩn;
      muốn hiện: `FCHAT_ENV_SHOW_PROD=1`. Thử `@bot env etask`, `@bot MR 1395 etask ở đâu?`.

### A2. Danh bạ + quyền (`work/team.json`, hiện CHƯA có file)
- [ ] 👤 Tạo hồ sơ thành viên: `python agentkit.py team bootstrap --stdin` (từ danh sách participant) hoặc
      `team set <key> --name … --fchat-id … --gitlab … --atask-id …`.
- [ ] 👤 Xác nhận liên kết từng người: `team verify <key> --by <tên bạn>`.
- [ ] 👤 Quyết: ai có `deploy:dev|sandbox|demo`, ai có `approve:<env>` → `team perms <key> --add deploy:dev`.
      (Mặc định ai cũng `review` + `task`; muốn bỏ: `--remove task`.)

### A4. Khai môi trường cho từng repo (registry `work/projects.json`, mục `environments.<env>.envmap`)
Bot cần biết workload k8s, container chính và tiền tố git tag của MỖI env. etask đã khai xong (dev, demo, sandbox).
Repo khác: thêm `{"workload": "deployment/<tên>" hoặc "dc/<tên>", "container": "<tên>", "git_tag_prefix": "<tiền tố>."}`
— hoặc nhờ tôi dò (pod + GitLab tags) như đã làm với etask.
- [ ] 👤 Chọn repo cần theo dõi (ngoài etask).
- [ ] 👤 Kiểm quyền k8s trên **ubuntu-c** (máy chạy bot): `python agentkit.py k8s access --project etask --env dev|sandbox|demo`.

### A3. Quyết định còn mở (cần trước pha tương ứng)
- [ ] 👤 **S1** — cho phép gửi 1 khung WS với `senderId` SAI vào *Saved Messages của bạn* để xem server có ép
      định danh không. ⛔ Chặn Pha 3 (task) và Pha 4 (deploy).
- [ ] 👤 **S4** — bạn tự sửa / reply / thả reaction / ghim 1 tin trong Saved Messages trong lúc bot nghe WS (chỉ đọc) → biết định dạng để sửa thẻ tại chỗ.
- [x] Bản đồ môi trường: code dùng `FCHAT_ENV_REPOS` + prod tắt mặc định (bạn chỉ cần khai — A1/A4).
- [ ] 👤 Project/list aTask mặc định cho lệnh tạo task.
- [ ] 👤 Giờ gửi bản tổng hợp môi trường (đề xuất 08:30).
- [ ] 👤 Quyền k8s của máy chạy bot (ubuntu-c) theo env — để biết env nào xác minh được ✅.

---

## B. Tiến độ theo pha

| Pha | Nội dung | Trạng thái | Commit / ghi chú |
|---|---|---|---|
| 0b | Outbox (phân biệt tin bot / chủ nhóm), inbox bền, catch-up không chạy lệnh cũ, heartbeat | ✅ | `d0b06cd` — Codex ACCEPT |
| 1a | Thẻ R-n thay tin "🔎 Review…", không ack ngay, chờ bot 3′, quá hạn 30′, head chưa đổi ⇒ trỏ thẻ cũ | ✅ | `3364820` — Codex ACCEPT (7 vòng) |
| 1b | `review lại R-n`, `@bot tôi là ai`, `@bot ping`, `@bot help`, quyền theo `team.json` | ✅ | `aaf8eb9` — Codex ACCEPT (2 vòng) |
| 2a | Đọc môi trường: `@bot env <repo>`, `@bot MR <số> <repo> ở đâu?` (+ link tự kiểm, mức xác minh) | ✅ | `c42c9d1` — Codex ACCEPT (3 vòng); etask chạy thật OK |
| 2b | Tin `E-<repo>-<env>` tự động khi env đổi phiên bản + bản tổng hợp; chạy shadow 2 ngày | ⬜ | sau 2a; cần giờ tổng hợp |
| 3 | Task aTask: nháp T-n → `xác nhận T-n` → tạo; `xong T-n` (hỏi lại) | ⛔ | cần S1, S7, project/list mặc định |
| 4 | Deploy dev/sandbox: D-n → người KHÁC duyệt → Jenkins (ghim commit) → đợi rollout | ⛔ | cần S1, S8, quyền deploy/approve |
| 5 | Extension bật các lệnh mới; sửa thẻ tại chỗ nếu S4 đạt | 🟡 | v1.7.0: nhãn Bot/Bạn, thẻ E, nút Review lại R-n |

### Chi tiết còn lại từng pha
**1b**
- [x] Danh bạ + quyền mặc định (`core/identity.py`), policy review (`core/policy.py`)
- [x] Lệnh neo đầu dòng (`workflows/group_board/commands.py`) + nối vào `group_watch`
- [x] `team.py perms / verify`
- [x] Codex ACCEPT → commit
- [ ] Cảnh báo Telegram cho bạn khi heartbeat dừng (hiện supervise chỉ khởi động lại + ghi log)

**2a — bản đồ môi trường (đọc)**
- [x] Spike S5: GitLab tags API, `merge_base`, `/commits/:sha/merge_requests` với token hiện tại
- [~] Spike S6: k8s (digest pod, RS/RC) ✅ ở 4 env; phía Jenkins (build ↔ digest ↔ SHA) chưa làm
- [x] `k8s.py workload`: Deployment + DeploymentConfig + digest pod + giờ tạo RS/RC
- [x] `integrations/gitlab`: tag, ancestor, MR port cùng tiêu đề, sha merge/squash/ff
- [ ] `integrations/jenkins`: build info + SHA thực + digest ⇒ mới trả được ✅ đã xác minh
- [x] `envmap`: snapshot + mức 🟡/❔/⚠ (✅ chờ Jenkins) + "MR có ở env X?" 4 mức
- [x] Lệnh `@bot env`, `@bot MR … ở đâu` (+ link tag / compare)
- [~] So tay: etask 3 env khớp kiến thức đã biết; còn 2 repo nữa (chờ A4)
- [ ] Cờ "có revert — kiểm tay"; báo "có tag mới hơn chưa lên env" (vd dev.0.0.1274 có tag, pod vẫn 1273)

**2b — báo tự động**
- [ ] `env_watch.py` (daemon, supervise, backoff 429, cache)
- [ ] Tin E-<repo>-<env> khi đổi phiên bản (MR thêm/mất theo mã R)
- [ ] Bản tổng hợp dev hằng ngày
- [ ] Shadow 2 ngày (chỉ log) → không báo sai → bật

**3 — task aTask** (sau S1)
- [ ] Spike S7 (tạo task thay người khác?)
- [ ] Lệnh `@bot tạo task: … | giao: @X | hạn: …` → thẻ T nháp; `xác nhận / huỷ / xong T-n`
- [ ] Idempotency (mã thẻ) + đối soát khi timeout; 1 task thật bạn xác nhận

**4 — deploy** (sau S1, S8)
- [ ] Spike S8 (job nhận SHA/tag?) — không thì chặn, trừ dev opt-in `allow_unpinned`
- [ ] Thẻ D + payload chốt (repo, env, job, nhánh, commit, hash cấu hình)
- [ ] Duyệt 4 mắt (người khác, đã xác minh), một lần, có hạn; bạn duyệt qua Telegram
- [ ] Khoá repo:env, đợi build + rollout, so SHA build thực
- [ ] Test: duyệt đồng thời, HEAD đổi, rollback, DC demo

**5 — extension**
- [x] v1.6.0: thẻ chỉ từ bot, nhãn 🤖 Bot / Bạn, chế độ chủ nhóm, thẻ E, banner bot im lặng
- [x] v1.7.0: nút "Review lại R-n", bật `@bot tôi là ai / ping / help`
- [x] v1.7.1: bật `@bot env` / `@bot MR … ở đâu` trong bảng lệnh
- [ ] Bật Task / Deploy / Env khi bot hỗ trợ (`BOT_SUPPORTS`)
- [ ] Đưa thư mục extension vào git (hiện không có lịch sử thay đổi)

---

## C. Đã biết nhưng chưa xử lý
- MR có 2 người yêu cầu ⇒ thẻ kết quả gửi 2 lần (mỗi tin tag 1 người).
- Chưa chạy daemon thật với code mới ⇒ chưa có thẻ nào trong nhóm; mọi kiểm chứng là test + bơm extension qua Orca.
- `test_store_engine.test_ctx_run_kills_descendants_too` đỏ từ trước (không liên quan).
- Extension: chưa kiểm bản cài trong Chrome của bạn và nút "Gửi" bằng click thật.
