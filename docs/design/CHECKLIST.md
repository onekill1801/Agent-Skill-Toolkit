# Checklist triển khai — Agent-Skill-Toolkit

> Theo dõi tiến độ kế hoạch trong [`agent-ecosystem.md`](agent-ecosystem.md). Cập nhật **mỗi lần** xong một phần.
> Cập nhật lần cuối: 2026-10-13. Test: 410 xanh, 1 skip (Windows).

## Quy trình mỗi phần

```
code → test (Windows, cả suite — chỉ commit khi xanh) → commit → Codex review (bản chụp sạch, không .env)
     → sửa theo điểm đồng thuận → Codex xác nhận → ✅ → phần tiếp theo
```

Ký hiệu: ✅ xong + Codex đồng thuận · 🔁 đã code, đang review/sửa · 🟡 làm một phần · ⬜ chưa làm ·
⛔ bị chặn (chờ thông tin/thao tác của người dùng — ghi ở cột Ghi chú)

---

## 0. Nền an toàn (Phụ lục A)

| # | Việc | Trạng thái | Commit | Codex | Ghi chú |
|---|---|---|---|---|---|
| 0.1 | `orca_adapter` chỉ gọi `orca.exe` (chống chèn lệnh qua `.cmd`) | ✅ | `20cb944` | review gốc (vòng 1 nhánh) | đã xác nhận lỗ hổng bằng thử nghiệm |
| 0.2 | Duyệt review: board/gate không duyệt; duyệt gắn hash nội dung | ✅ | `20cb944` | — | giới hạn tạm: file cùng user OS → cần 6.x |
| 0.3 | Chống đăng trùng: lock, marker, không đăng lại inline | ✅ | `20cb944` | — | |
| 0.4 | Hook: PowerShell/Grep/agentkit/secret/ghi trạng thái | ✅ | `20cb944` | — | nhược điểm: chặn cả lệnh shell chỉ đọc `review_queue` |
| 0.5 | k8s ẩn mọi Secret; dbeaver chặn ghi PROD | ✅ | `20cb944` | — | lớp chắc nhất vẫn là DB user chỉ đọc |
| 0.6 | `cg.py` chạy được Windows (node + script, tắt telemetry) | ✅ | `20cb944` | — | |
| 0.7 | `agent_runner`: không chạy launcher `.cmd` qua `cmd.exe` (`core/winexec.py`) | ✅ | `317a381`, `4a847a0`, `199a6f5` | GO (vòng 3) | đã thử thật: payload tới PowerShell nguyên văn; codex chạy qua node; fallback `cmd /d /c` chỉ cho token thường |
| 0.8 | MED: clone race `review_ws`, lock bị phá giữa chừng | ⬜ | | | người dùng tạm hoãn |
| 0.9 | MED: `podfwd` (mồ côi, PID, `--insecure`), `vpn` argv, allowlist theo tên, `save_seed` mỗi lần | ⬜ | | | người dùng tạm hoãn |
| 0.10 | Thu hồi / cấp lại token trong `.env` | ⛔ | | | **người dùng tự làm** (đã hoãn) |

## Track R — tái cấu trúc repo (§B.4)

| # | Việc | Trạng thái | Commit | Codex | Ghi chú |
|---|---|---|---|---|---|
| R.M1 | Kiểm kê: đồ thị import + cạnh runtime, API công khai, baseline test | ✅ | `b889d86` → `7548c6d` | GO (vòng 4, cùng R.M6) | `_runtime/inventory.py` → `docs/design/INVENTORY.md` (module, đồ thị vùng, ngoại lệ, cạnh runtime, mọi tool + CLI kể cả lệnh con đăng ký trong vòng lặp, test) |
| R.M2 | `_runtime` + bootstrap; kiểm cài plugin | 🟡 | `8520548` | GO (cùng F.4a) | bootstrap + package chạy từ mọi cwd/agentkit; **chưa thử cài marketplace / Orca / Codex** |
| R.M3 | Tách primitive chung vào `core` (env, UTF-8, lock), giữ wrapper | ✅ | `b889d86` → `f8c2c15` → `4c7befb` | GO (vòng 2) | `core/text.py` (clean/literal/quoted), `core/env.py` (luật .env giống dev-automation + `TOOLKIT_ENV_FILE`), `core/console.py` (UTF-8), `core/locks.py` (khoá của HĐH, tự nhả khi tiến trình chết — sửa luôn race của khoá cũ); `review_common.file_lock` dùng nó. `config.py` các skill giữ nguyên (wrapper; một file có thay đổi chưa commit của bạn) |
| R.M4 | Chuyển `mr-review` sang `workflows/mr_review` + `integrations/gitlab` | 🟡 | `51e0f5f` → `3582009` → `c8ba4f4` | GO (lát 1, vòng 3) | lát 1 xong: `integrations/gitlab.py` (client theo project, timeout, lỗi mạng = dict, phản hồi rỗng = lỗi, ghi không chắc = `uncertain`); mọi lời gọi GitLab của mr-review đi qua nó; đăng inline có marker ẩn + đối chiếu mọi trang trước khi đăng lại. Còn: chuyển logic review_flow/queue/ws/post vào `workflows/mr_review` (giữ vỏ CLI) |
| R.M6 | Mở rộng dần + test layering (bánh cóc) | ✅ | `b889d86` → `7548c6d` → `a4b2ecd` → `cbb8275` | GO (vòng 4) | `tests/test_layering.py`: luật §B.2 bằng AST (kể cả import tương đối + alias), loader động / `sys.path` / script path / module chạy tiến trình đều phải khai báo; ngoại lệ theo cặp module, có hợp đồng, chỉ giảm so với `main` (bật khi nhánh merge); 6 → 4 ngoại lệ |

## Track F — tính năng (§14)

| # | Việc | Trạng thái | Commit | Codex | Ghi chú |
|---|---|---|---|---|---|
| F.4a | Store bền + engine checkpoint + quota pause/resume + handoff | ✅ | `8520548` → `980412a` → `4a847a0` → `199a6f5` | GO (vòng 3) | lease token, heartbeat, input key, kill cây tiến trình; mẫu limit vẫn tự viết — cần câu thông báo thật |
| F.4b | Gắn store/engine vào luồng review MR (`review_flow.py`) | ✅ | `65f923d` → `01fdbae` → `b336e7f` | GO (vòng 3) | quota → dừng; MR đổi head → review lại; chờ duyệt → blocked + reconcile; phát hiện giả mạo → quarantine (chỉ người gỡ). Cô lập OS thật = F.6b |
| F.4c | Chạy thử review 1 MR thật (shadow) | ⛔ | | | **cần project + số MR** |
| F.6a | Luật tự đăng comment GitLab (`review_rules.py`) | ✅ | `20cb944` | — | mặc định `shadow`; cần `REVIEW_AUTO_POST_PROJECTS` |
| F.6b | Executor tách biệt + duyệt Telegram gắn hash | ⛔ | | | **cần user Windows chuẩn + token GitLab chỉ đọc** |
| F.1 | Làm rõ → AC, gom câu hỏi 1 lần, triage 4 làn + tự nâng làn | ✅ | `12b4e42` → `bf35c5d` → `b1c43ab` | GO (vòng 3) | làn quyết định tier/mode/debate; đề xuất chỉ khi là câu hỏi; nội dung task được làm sạch + «trích» + vô hiệu markup |
| F.2 | Thang kiểm local + vòng tự sửa (`workflows/verify/ladder.py`, `auto-dev/tools/verify.py`) | ✅ | `20ececc` … `09f6b76` (8 commit) | GO (vòng 8) | worktree sạch + đúng nhánh trước mỗi bậc; pending fix bền; mọi bậc chạy lệnh khai `protects`; thay đổi test/cấu hình/spec/launcher → người duyệt; chạy thử trên toolkit: done. Repo Java chờ người dùng chọn |
| F.3 | AC → test thật trong repo + bảng AC → bằng chứng | ✅ | `d516752` → `28cae3d` → `e5b5bbd` → `fa4ceb5` | GO (vòng 4) | AC chỉ tính khi testcase JUnit của LẦN CHẠY NÀY pass, từ file test đổi trên nhánh (khớp đủ package, không mơ hồ); thiếu test cho AC → người xử lý (không tự sửa); báo cáo chỉ tin kết quả cùng spec + cùng lần chạy suite; `verify.py start --ac/--ac-file`, `report` |
| F.5 | Cổng vào: `mr_watch` / `atask_watch` / `group_watch` chỉ xếp hàng vào store | ✅ | `32257f7` → `fd921b3` → `0905ba4` | GO (vòng 3) | `intake/gate.py`: nguồn → độ tin cậy + loại việc, công tắc `work/policy.json` (sai định dạng = tắt), allowlist theo user id, link MR đúng host+port, không userinfo; `review_queue add/add-text` cũng qua cổng; người trong group chỉ được agent CHỈ ĐỌC, build chỉ chủ tài khoản; `mr_watch` mặc định xếp vào store; `atask_watch --mode store` (mặc định direct tới F.7). **Cần bạn đổi `FCHAT_REVIEW_ALLOWED` sang user id** |
| F.7 | Điều phối nhiều việc + bảng tiến độ (Orca + aTask/`status.py`) | ✅ | `3c87212` → `80b230b` → `ad62576` → `7c661e7` | GO (vòng 4) | `coordinator.py work --parallel N` chạy mọi loại việc (review_mr, verify, atask_intake); giới hạn song song lưu trong DB (`capacity`, mặc định 2) áp cho mọi vòng work; phụ thuộc + lỗi lan theo chuỗi; khoá repo; lease hết hạn không hồi sinh; GitLab project theo luồng; `board` + `status.py` mục tasks; đề xuất aTask → `accept` (atomic). Chưa đồng bộ aTask "Agent Hub" (cần bạn quyết) |
| F.8 | Thang kiểm mở rộng: deploy dev, health pod, smoke UI, e2e, bench | ⛔ | | | **cần `environments.dev` trong `projects.json`** |
| F.9 | Làn Đề xuất (ADR + debate) + quét chủ động hằng tuần | ✅ | `0fab6f5` → `d44b752` → `772465e` → `567b6d0` | GO (vòng 4) | `proposals.py scan` (không token: thư viện cũ theo bảng EOL tĩnh [Unverified] — chỉ từ khai báo build thật, bản không đọc được báo "không xác định"; điểm nóng git theo cả chuỗi đổi tên) → "Đề xuất tuần"; `--enqueue` = ứng viên CHỜ [Làm] (`go`); debate proposer/critic **chỉ đọc**, checkpoint từng lượt → ADR (sha256) → `decide accept\|reject` (accept chỉ khi ADR khớp code + câu hỏi hiện tại, cũ → `refresh`; reject nhớ vĩnh viễn, atomic). Agent không tự go/decide được. Chưa: spike, gửi Telegram (cần duyệt), CVE online |
| F.10a | Làn Lớn — khảo sát + bánh cóc kiến trúc | ✅ | `78910ea` → `db8d8f1` → `eba0e36` → `033e07b` → `6fc2a12` | GO (vòng 5) | `refactor.py survey` (module, phụ thuộc, vòng, test theo module, điểm nóng — chỉ đọc); `ratchet init/check/tighten` + bậc `kind: ratchet`: luật + baseline đọc từ BASE ref, nhánh làm yếu luật / phình baseline bị chặn, so khớp theo câu lệnh logic (Java/Kotlin/JS-TS/Python), chọn file theo đường dẫn hoặc `package`, file không quét được / vi phạm bị chuyển ra ngoài → người xem, quét có giới hạn thời gian. Giới hạn ghi rõ: chuyển file + viết lại chính câu vi phạm |
| F.10b | Làn Lớn — chương trình nhiều lát (DAG, worktree theo lát, mốc duyệt, đo tiến độ) | ✅ | `a7b950f` → `482c347` → `6699b6a` → `e365d84` → `d36a862` | GO (vòng 5) | `refactor.py program new` (cả DAG tạo trong 1 transaction, base ghim SHA, spec phải có ratchet + full suite, spec file được ghim) → `program approve` (chỉ người, kiểm trong handler) → mỗi lát: clone RIÊNG + nhánh `refactor/<run>-<id>`, kiểm nhánh trước/sau agent, implement → verify → publish (chỉ commit đã xanh theo spec đã duyệt, không force); lát 1 phụ thuộc chồng lên commit đã publish, nhiều phụ thuộc chờ bạn merge; clone của bạn so trước/sau mỗi lượt agent. Tạo MR / merge: bạn. Cô lập OS thật: F.6b |
| F.10c | Làn Lớn — lưới an toàn: chụp hành vi/contract + số đo nền | ✅ | `9abc9d6` → `a281699` → `855ff4b` | GO (vòng 3) | bậc `kind: snapshot` (đầu ra lệnh = ảnh chụp trên BASE, khác → người quyết, diff được che secret) + `kind: metrics` (số đo hữu hạn, trong ngưỡng nền trên BASE theo chiều tốt + tolerance); file `output` phải do lần chạy này ghi, trong repo, không qua link; `refactor.py snapshot\|metrics record` ghi file nền (chỉ trong repo) để bạn duyệt + commit; `program new` cảnh báo khi thiếu |
| F.10d | Làn Lớn — lát cơ học bằng tool, lập lại kế hoạch, tiến độ bánh cóc | ✅ | `eb6a41c` → `283f333` | GO (vòng 2) | lát có `run: [argv]` (OpenRewrite / codemod) chạy tool thay agent — vẫn kiểm clone/nhánh kể cả khi tool lỗi, vẫn qua thang kiểm + publish; `program extend --slices` thêm lát sau mỗi đợt, mốc duyệt mới, lát mới chồng lên commit đã publish; `program status` cho từng lát: verify/publish, commit đo, ratchet trên đúng nhánh đó, chồng lên lát nào |
| F.13 | Tài khoản agent: 5 tài khoản Orca + bảng ai làm gì / còn bao nhiêu | ✅ | `b7999b0` → `75c17ca` | GO (vòng 3) | `work/agents.json` trỏ `CLAUDE_CONFIG_DIR` vào tài khoản Orca; mỗi lượt agent ghi tài khoản (usage `backend:model@account`) + slot ghi việc đang làm; `accounts.py` gộp agents.json + `orca account list` (live / cache / reset? ) + slot + lượt hôm nay, chỉ đọc |
| F.14 | Giao diện web theo dõi (mở trong trình duyệt Orca) | ✅ | `f9d8b6c` → `2a374b2` | GO (vòng 5) | `dashboard.py serve` (127.0.0.1, GET, kiểm Host, không nút hành động): ô số, "Cần bạn xử lý" kèm lệnh copy, toàn bộ việc store, tài khoản + dung lượng, review MR, daemon/run/queue/forward/feedback; 1 worker nền dựng snapshot; `Store.reader` (sqlite mode=ro); mọi chuỗi + khoá qua redact; file đọc lỗi → cảnh báo "có thể THIẾU". Kèm sửa: test `review_flow` rò yêu cầu duyệt giả vào `temp/tg_approvals` (rc_config theo `TOOLKIT_STATE_DIR`) |
| F.15 | Dung lượng từng tài khoản đọc thẳng từ Anthropic | ✅ | `e3deb05` → `9fae379` | GO (vòng 4) | `integrations/claude_usage`: access token có sẵn của từng tài khoản → `oauth/usage` (https, không redirect, chỉ trả số; token hết hạn → báo, KHÔNG gia hạn); `core/tls`: Python msys không có CA → nạp kho chứng chỉ Windows trong bộ nhớ, không bao giờ tắt kiểm chứng chỉ; `accounts.py`: song song, cache 5 phút theo danh tính tài khoản, 1 request/tài khoản, request treo bị bỏ không ghi đè kết quả mới; `--orca-only` |
| F.16 | Review MR đăng theo mẫu "Code Review Summary" cũ + không bao giờ tag nhầm | ✅ | `e502f29` → `e502977` | GO (vòng 15) | Note theo `dev-automation/prompts/code_review_prompt.md`: Overview, 🔴 Critical / 🟡 Major / 🟢 Minor có Why/Suggestion, Standards Compliance, Positive Observations, Files Reviewed (từ ws.json); review.json thêm `compliance`/`positives`/`date` (tuỳ chọn). Chống tag: chạy 1 lần trên toàn văn bản sẽ đăng — mọi `@` ngoài code fence tin cậy được chèn U+2060, `&`-reference và `%40` bị vô hiệu; fence chỉ tin khi ở cột 0, info string là đúng 1 ngôn ngữ thường, văn bản không có CR/`>>>`/`$$`/dòng `[`/HTML/fence thụt/fence dài; luật tự đăng từ chối mọi `@`/reference/`%40`/HTML-có-thuộc-tính còn sót. Date chỉ lấy từ review.json (hash duyệt ổn định) |
| F.17 | Cài đặt trên Dashboard + chú thích lệnh từng chức năng | ✅ | `29c72b0` → `c834309` | GO (vòng 5) | `workflows/settings`: danh mục công tắc KHÔNG bí mật (review tự đăng, TChat, Telegram, song song, trần lượt/ngày) — kiểm kiểu/khoảng, chặn trang cũ (kiểm dưới khoá), ghi `temp/settings_audit.jsonl`, báo tab cần restart; `core/env.set_values` sửa .env tại chỗ (dòng đầu tiên, giữ comment/CRLF), atomic + thử lại trên Windows, từ chối giá trị/ký tự phân dòng mà parser nào đọc lệch, tự kiểm parse lại. Dashboard: POST `/api/settings` cần key mỗi lần chạy (in ở tab, qua `#fragment`), cùng origin, JSON; luôn đọc body trước khi trả lời; thẻ Khởi động + 'Lệnh & cách dùng' mọi thẻ; khoá bí mật chỉ hiện đã đặt/chưa. CLI `toolkit_settings.py`; hook hỏi với `set` và `/api/settings`. Kèm: từ khoá TChat tách theo dấu phẩy (giữ cụm 'lên dev') |
| F.18 | Review MR gọn mặc định; comment inline thành tuỳ chọn | ✅ | `cb5683c` → `fa3d5d9` | GO (vòng 4) | `REVIEW_NOTE_STYLE=compact` (mặc định): kết luận + tóm tắt + mỗi lỗi 1–2 dòng (đuôi đường dẫn ngắn nhất mà vẫn phân biệt, câu dài cắt ở ranh câu ngoài code span/link, `**` lẻ được đóng; nit chỉ tiêu đề), bỏ bảng; `full` giữ mẫu cũ. `REVIEW_INLINE_COMMENTS=0` mặc định (inline_done chỉ tính với ứng viên hiện tại). Cả hai trong thẻ Cài đặt; SKILL.md yêu cầu agent viết ngắn. !1202: ~8,5k → ~3,4k ký tự |
| F.19 | Comment review theo đúng mẫu team (MR !1153) | ✅ | `fbec9de` → `7bbfa26` | GO (vòng 2) | Mặc định `REVIEW_NOTE_STYLE=full` = mẫu team: header MR/Reviewer/Date/Commit (source → target)/Verdict + `verdict_note`, `> Phạm vi:` (`scope` hoặc suy từ tests), Critical/Major có `Why:`/`Suggestion:` (ngắt dòng cứng), Minor/Nitpicks một đoạn, Compliance (✅/🟡/❌), Positive, Files Reviewed (bỏ src root + tiền tố package chung, giữ 1 cấp; trùng thì đường dẫn đầy đủ); không footer. Date fallback = `prepared_at` của workspace (hash ổn định) |
| F.20 | Báo lại group sau khi đăng review: không còn mất im lặng | ✅ | `38de4ab` → `38a16fb` | GO (vòng 5) | Trước: kết quả `notify_group` bị bỏ qua (etask!1223 đã đăng nhưng group không được báo). Nay `notices{người: pending/sending/sent/failed/unconfirmed/uncertain}` ghi CÙNG lần lưu 'posted', lưu trước/sau từng lần gửi qua khoá `queue` và chỉ khi job vẫn là review đó; `failed` (chỉ khi lỗi có cấu trúc trước khi gửi) được `sweep` thử lại theo từng người (5 lần, giãn cách); `unconfirmed`/`uncertain`/timeout không tự gửi lại (tránh trùng) — dashboard hiện ở 'Cần bạn xử lý', `review_post.py notify <job>` gửi tay (hook hỏi). Tiến trình con không nhận token TChat cũ của tiến trình cha; lỗi che token trước khi cắt; mọi lần lưu của luồng đăng gộp người yêu cầu mới (add_job) |
| F.21 | Daemon tự chạy lại khi lỗi / khi có code mới (`supervise.py`) | ✅ | `5ca96b5` → `8cc1177` | GO (vòng 9) | Bọc mỗi tab daemon: thoát/lỗi/không khởi động được → chạy lại sau 2→60 s (reset sau 5 phút ổn); commit mới (git HEAD) / `.env` / `agents.json` đổi → dừng và chạy lại ngay (sửa chưa commit thì không). Dừng = CẢ cây tiến trình: Windows Job Object (tạo suspended → gán job → resume; KILL_ON_JOB_CLOSE kể cả khi supervisor chết), POSIX subreaper + dấu env, quét-kill lặp tới khi sạch, chỉ kill tiến trình chứng minh được là của mình; KHÔNG chạy bản mới khi bản cũ còn sót (cleanup_blocked); Ctrl+C lúc khởi động/đang dừng được ghi nhận rồi thoát. Thẻ Khởi động + hướng dẫn dùng supervise; log `daemon_health.jsonl`. Kèm: de-flake test timeout |
| F.22 | Chia đều lượt agent giữa các tài khoản theo dung lượng còn lại | ✅ | `6a685ed` → `1d3d2cc` | GO (vòng 3) | Trước: thử theo thứ tự agents.json → mọi review chạy trên dungpd15. Nay mỗi nhóm (backend, model) sắp: còn ≥ `ACCOUNT_MIN_LEFT_PCT` (mặc định 10%, min 5h/tuần) nhiều nhất trước → không có số (token hết hạn…) → dưới ngưỡng cuối; hoà thì ít lượt hôm nay hơn; nhóm model giữ thứ tự (opus trước sonnet). Số đọc từ Anthropic, cache file chỉ số (`temp/account_usage.json`, ghi atomic file tạm riêng, dấu nhận diện tài khoản, thời điểm đọc từng tài khoản, lỗi tạm thời hết hạn 60 s, giá trị lạ bị loại); `ACCOUNT_BALANCE=0` về thứ tự cũ; lỗi gì cũng giữ thứ tự cũ |
| F.23 | Tự nhận diện bối cảnh máy ngoài git (`machine_context.py`, in kèm `doctor.py`) | ✅ | `0401530` → `2e947f8` | GO (vòng 11) | Chỉ kiểm tồn tại/không rỗng, không đọc giá trị bí mật hay nội dung file khoá, không gọi mạng: `.env` thật sự được nạp (`config._ENV_PATH`) + nhóm khoá theo tính năng (Jenkins cần USER, VPN cần USER); `work/projects.json` (clone_dir là repo git, entry sai cấu trúc = lỗi); `work/agents.json` (CLI theo PATH của tài khoản, thư mục + file đăng nhập `.credentials.json`/`auth.json`, env chuẩn hoá giống `agent_runner._account_env`); `~/.ssh`, kubeconfig (`KUBECONFIG`/`~/.kube/config`/Lens/ref registry), kubectl theo từng khối `k8s` của registry đúng thứ tự `k8s.find_kubectl` (giá trị nguyên văn, bản Lens mới nhất/ghim, không fallback); DBeaver, `gh` (hosts.yml độc quyền theo `GH_CONFIG_DIR` hoặc `GH_TOKEN`/`GITHUB_TOKEN`), `FCHAT_PRIVATE_KEY_PATH`, VPN client → bảng ready/partial/missing + gợi ý copy/cài. JSON sai/ probe lỗi không làm doctor crash |
| F.24 | Quy chuẩn bảo mật + máy quét offline cho repo sản phẩm (`security-audit`, `security_scan.py`) | ✅ | `903030c` → `17e90aa` | GO (vòng 12) | Quy chuẩn `security-audit/cookbook/standard.md` (mã AUTHZ/SEC/GEN/CFG/INJ/XXE/DESER/CRYPTO/TLS/LOG/XSS/JS) + máy quét chỉ đọc, không gọi mạng: kiểm kê endpoint Spring (`@RestController` × luật `HttpSecurity`, bỏ luật bị comment, mapping không method xét TỪNG method lấy kết quả xấu nhất) → endpoint không khớp luật / công khai / ghi công khai / nhận id cần kiểm quyền sở hữu; secret trong code, YAML/properties/`.env`/JSON (kể cả giá trị mặc định `${VAR:x}`; YAML list + block scalar) và lịch sử git (`--history`); code tự sinh/test sót; cấu hình prod nguy hiểm (`-prod*`); SQL/JPQL ghép chuỗi, XXE (miễn chỉ khi đúng biến factory được cấu hình bảo vệ ngay sau nó), deserialize, crypto yếu, TLS. Secret bị che ở MỌI mã lỗi (dòng có từ khoá secret bị cắt từ dấu nháy đầu); baseline = dấu vân tay (mã+file+dòng+thứ tự xuất hiện) → chỉ chặn lỗi MỚI; `security-audit: ignore` kèm lý do; exit 0/1/2 + **3 = quét chưa đủ** (file quá lớn/không đọc được, git lỗi — không bao giờ coi là sạch). etask: 5445 file / 771 endpoint trong ~25 s |
| F.11 | Điều tiết theo mức dùng (80% / 95%), model theo bước, ngân sách cứng | ✅ | `b26077a` → `0a23507` → `a989e1d` → `b8159f7` → `d59e5fe` | GO (vòng 5) | mức dùng do bạn/script khai (`coordinator.py usage PCT [--resets-at]`, hết hạn 6 h — toolkit không tự đọc được % subscription); ≥80%: dừng làn Đề xuất + quét tuần, opus→sonnet, vai nhẹ→haiku; ≥95%: chỉ review_mr + làn Nhỏ; 100%: không nhận việc, từ chối lượt agent tới reset. Ngân sách giữ chỗ **theo từng lượt agent** (atomic, tính trường hợp xấu nhất): task (lượt/giây) → chặn; trần lượt/ngày → dừng tới 0h; lượt API chạy trong tiến trình con bị giết đúng hạn |
| F.12 | `code_context.py` (CodeGraph mặc định, grep dự phòng) cho grounding / fix_loop | ✅ | `166bcf7` → `7b917c4` → `84fed60` → `bc785de` | GO (vòng 2) | `callers/impact/locate/affected/trace/status`; CodeGraph chỉ khi index mới (cũ → sync 1 lần, lỗi/sai định dạng → grep), luôn ghi `source` + lý do; grep không theo symlink, không đọc secret; tên đầy đủ chỉ khớp đúng định nghĩa, grep chỉ trả *ứng viên*; `trace` stack Java (cả inner class)/Python/Node (Windows)/Go → file trong repo, bỏ frame thư viện; fix_loop nhận `<code_locations>`; grounding scout xếp file định nghĩa symbol lên đầu |

## Thông tin đang chờ người dùng

| Cần | Dùng cho |
|---|---|
| Project + số MR để chạy thử | F.4c |
| Repo thử cho thang kiểm (repo Java, vd etask) | F.2 |
| Repo được tự đăng (`REVIEW_AUTO_POST_PROJECTS`), giữ `shadow` bao lâu | F.6a |
| Allowlist TChat (user id) | F.5 |
| Agent mặc định (đề xuất: code = Claude, review chéo = Codex) | F.1, F.2 |
| Câu thông báo limit thật của Claude CLI / Codex | F.4a |
| `environments.dev` (job Jenkins, context/namespace k8s, URL dev) | F.8 |
| User Windows chuẩn + token GitLab chỉ đọc | F.6b |

## Nhật ký review Codex

| Ngày | Phần | Vòng | Kết luận |
|---|---|---|---|
| 2026-10-11 | Kế hoạch hệ sinh thái | 2 | đồng thuận (Phụ lục B) |
| 2026-10-12 | Kiến trúc repo | 2 | đồng thuận (Phụ lục C) |
| 2026-10-13 | F.4a store/engine/quota (`8520548`) + 0.7 winexec (`317a381`) | 1 | **NO-GO**: 1 HIGH (thiếu claim token) + 6 MED (heartbeat, quota runner→engine, LeaseLost lọt, checkpoint không gắn SHA, quota bắt nhầm, reset bị cắt 6 h) — đồng ý cả 7, sửa ở `980412a` |
| 2026-10-13 | F.4a + 0.7 (`980412a`) | 2 | **NO-GO**: xác nhận sửa 4/7; HIGH mới (claim đọc sau commit nhận nhầm token), MED key_fn, migration race, winexec quoting, quota còn bắt nhầm — sửa ở `4a847a0` |
| 2026-10-13 | F.4b (`65f923d`) + xác nhận `199a6f5` | 1 | **NO-GO**: 2 HIGH (agent thừa hưởng token/quyền ghi; reaper Orca lấy lại job đang pause) + 3 MED (reconcile thiếu ca stale/head mới, Telegram gửi trùng khi crash, timeout trong không kill cây). `199a6f5` xác nhận FIXED cả 4 |
| 2026-10-13 | F.4b (`01fdbae`) | 2 | **NO-GO**: chấp nhận hoãn cô lập OS sang F.6b; HIGH mới (lời duyệt giả có hash khớp được reconcile tự mở chặn), MED (khoá gửi duyệt chỉ ở flow) — sửa ở `b336e7f` |
| 2026-10-13 | F.2 (`20ececc`) | 1 | **NO-GO**: 4 HIGH (nhánh chỉ kiểm lúc enqueue; `git add -A` gom thay đổi của người; nới test được; lỗi git = diff rỗng) + 4 MED — sửa ở `64613bd` |
| 2026-10-13 | F.2 (`64613bd`) | 2 | **NO-GO**: FIXED 4/8; HIGH còn (commit nhánh khác, danh sách bảo vệ thiếu, pending fix chỉ trong bộ nhớ) + MED — sửa ở `bcf4bef` |
| 2026-10-13 | F.2 (`bcf4bef`) | 3 | **NO-GO**: chấp nhận cách phát hiện commit nhánh khác; HIGH: script do interpreter chạy chưa bảo vệ, protection fail-open khi git lỗi; MED covered_by |
| 2026-10-13 | F.2 (`6aaeb21`, `bbf35af`, `bf842ad`, `1f69504`) | 4–7 | **NO-GO** từng vòng vì đoán script từ argv luôn lọt → đổi thiết kế: mọi bậc chạy lệnh phải khai `protects`, launcher luôn được bảo vệ, chuẩn hoá path, không phân biệt hoa thường, từ chối path ngoài repo |
| 2026-10-13 | F.2 (`09f6b76`) | 8 | **GO** |
| 2026-10-13 | F.1 (`12b4e42`) | 1 | **NO-GO**: HIGH làn lớn vẫn giữ mode auto/skip_debate; MED đề xuất bắt nhầm câu mệnh lệnh, thiếu làm rõ lại, giả mạo mục trong tin nhắn, giả định che đề xuất |
| 2026-10-13 | F.1 (`bf35c5d`) | 2 | **NO-GO**: FIXED 3/5; còn thiếu epic/defect, markup trong «…» vẫn hoạt động |
| 2026-10-13 | F.1 (`b1c43ab`) | 3 | **GO** |
| 2026-10-13 | F.4b (`b336e7f`) | 3 | **GO**: quarantine + khoá gửi duyệt chung đều FIXED; không có lỗi mới. Gợi ý thêm test tranh chấp đồng thời flow/direct |
| 2026-10-13 | F.4a + 0.7 (`4a847a0`) | 3 | **GO**: chấp nhận 2 phản biện (finish khi lease hết hạn an toàn nhờ token; dừng hợp tác cho Python thuần). MED còn: kill cây tiến trình, quota JSON nhúng, tham số rỗng — sửa ở `199a6f5` (sẽ được xác nhận trong vòng review F.4b) |
| 2026-10-13 | F.3 (`d516752`) | 1 | **NO-GO**: marker trong file không phải test/comment được tính; test bị skip / suite không liên quan vẫn ✅; báo cáo diễn giải checkpoint cũ theo spec mới; start trùng làm mất AC; AC1000 bị đánh số lại — sửa ở `28cae3d` |
| 2026-10-13 | F.3 (`28cae3d`) | 2 | **NO-GO**: FIXED 3/5; report JUnit cũ còn được tính, checkpoint coverage/suite lệch commit; HIGH mới: Java khớp theo tên class đơn — sửa ở `e5b5bbd` |
| 2026-10-13 | F.3 (`e5b5bbd`) | 3 | **NO-GO**: HIGH tiền tố package (`com/original.java`) giả làm file test — sửa ở `fa4ceb5` |
| 2026-10-13 | F.3 (`fa4ceb5`) | 4 | **GO** |
| 2026-10-13 | F.5 (`32257f7`) | 1 | **NO-GO**: 3 HIGH (TChat build/freeform lách cổng; `review_queue add/add-text` lách cổng; userinfo lách kiểm host) + 2 MED (mr_watch mất MR khi tiến trình chết; policy sai kiểu = bật) — sửa ở `fd921b3` |
| 2026-10-13 | F.5 (`fd921b3`) | 2 | **NO-GO**: FIXED 4/5; câu hỏi tự do từ group vẫn chạy agent đủ quyền — sửa ở `0905ba4` (agent chỉ đọc) |
| 2026-10-13 | F.5 (`0905ba4`) | 3 | **GO** |
| 2026-10-13 | F.7 (`3c87212`) | 1 | **NO-GO**: 3 HIGH (review song song đổi `GITLAB_PROJECT_ID` toàn tiến trình; lease hết hạn hồi sinh được → 2 holder; task verify cũ không có khoá repo) + 4 MED (giới hạn song song theo từng caller, accept nhận nhầm task bị chặn khác, lỗi phụ thuộc không lan qua chuỗi, board mất việc mở + cháu) — sửa ở `80b230b` |
| 2026-10-13 | F.7 (`80b230b`) | 2 | **NO-GO**: FIXED 4/7; còn backfill khoá repo, chính sách capacity chung, accept chưa atomic — sửa ở `ad62576` |
| 2026-10-13 | F.7 (`ad62576`) | 3 | **NO-GO**: accept vẫn dùng checkpoint đã bị vô hiệu — sửa ở `7c661e7` |
| 2026-10-13 | F.7 (`7c661e7`) | 4 | **GO** |
| 2026-10-13 | F.11 (`b26077a`) | 1 | **NO-GO**: 2 HIGH (ngân sách kiểm theo bước, không giữ chỗ → vượt trần; nhiều lối gọi agent không bị tính) + 5 MED (lượt bị ngắt không tính, chặn cả bước không dùng agent, 100% không dừng, policy hỏng = mở, đổi model sót lối) + LOW DST — thiết kế lại ở `0a23507` |
| 2026-10-13 | F.11 (`0a23507`) | 2 | **NO-GO**: FIXED 6/8; còn làm tròn timeout lên 1 s, API không có hạn chót tổng, mất kiểu QuotaExceeded qua role, NaN — sửa ở `a989e1d` |
| 2026-10-14 | F.11 (`a989e1d`) | 3 | **NO-GO**: API chỉ thôi chờ chứ không huỷ; `proc.run` chạy lố 1 nhịp poll — sửa ở `b8159f7` |
| 2026-10-14 | F.11 (`b8159f7`) | 4 | **NO-GO**: watchdog không huỷ được DNS/kết nối → POST sau hạn — sửa ở `d59e5fe` (tiến trình con) |
| 2026-10-14 | F.11 (`d59e5fe`) | 5 | **GO** (khe hở kiểm-rồi-gửi chấp nhận được cho kiểm soát ngân sách) |
| 2026-10-14 | F.12 (`7b917c4`) | 1 | **NO-GO**: HIGH symlink lộ secret qua grep + 4 MED (frame thư viện gán vào file repo, lỗi truy vấn không rơi về grep, che mơ hồ / tên đầy đủ, đường dẫn Node Windows) + LOW quét repo khi không có trace — sửa ở `84fed60` |
| 2026-10-14 | F.12 (`84fed60`) | 2 | **GO** (còn MED: `../`, dữ liệu lồng sai kiểu, grep cho tên đầy đủ, inner class — đã sửa ở `bc785de`) |
| 2026-10-14 | F.9 (`0fab6f5`) | 1 | **NO-GO**: 2 HIGH (agent debate ghi được repo + tự go/decide; accept không gắn ADR/đầu vào) + 6 MED (reject không atomic, đổi tên lách từ chối, debate 1 checkpoint, đoán phiên bản, lọc điểm nóng sai, markdown không vô hiệu) — sửa ở `d44b752` |
| 2026-10-14 | F.9 (`d44b752`) | 2 | **NO-GO**: FIXED 6/8; accept chưa so input key hiện tại; phiên bản không đọc được bị bỏ im; + migrate danh sách từ chối cũ, trần 20 tên — sửa ở `772465e` |
| 2026-10-14 | F.9 (`772465e`) | 3 | **NO-GO**: pom lấy property `.version` không dùng làm dependency — sửa ở `567b6d0` |
| 2026-10-14 | F.9 (`567b6d0`) | 4 | **GO** |
| 2026-10-14 | F.10a (`78910ea`) | 1 | **NO-GO**: 4 HIGH (HEAD làm yếu luật cho nhánh sau; file quá lớn/symlink lọt; xuống dòng lách luật; chuyển file lách luật) + 6 MED — sửa ở `db8d8f1` |
| 2026-10-14 | F.10a (`db8d8f1`) | 2 | **NO-GO**: FIXED 6/10; + Kotlin không `;`, baseline cũ, đổi tên không lưu — sửa ở `eba0e36` |
| 2026-10-14 | F.10a (`eba0e36`) | 3 | **NO-GO**: import JS/TS có `{}` lọt; câu dài chuyển ra ngoài — sửa ở `033e07b` |
| 2026-10-14 | F.10a (`033e07b`) | 4 | **NO-GO**: export trước nuốt import sau (chấp nhận giới hạn move+rewrite) — sửa ở `6fc2a12` |
| 2026-10-14 | F.10a (`6fc2a12`) | 5 | **GO** |
| 2026-10-14 | F.10b (`a7b950f`) | 1 | **NO-GO**: 5 HIGH (nhiều phụ thuộc bắt đầu sai code; nhánh cũ dùng lại; kiểm nhánh sau agent; agent đụng clone chung; duyệt chỉ chặn bằng hook) + 3 MED (base không ghim, spec yếu, DAG không atomic) — sửa ở `482c347` |
| 2026-10-14 | F.10b (`482c347`) | 2 | **NO-GO**: FIXED 5/8 (cô lập + duyệt chấp nhận tạm chờ F.6b); HIGH mới: publish trước khi verify — sửa ở `6699b6a` |
| 2026-10-14 | F.10b (`6699b6a`) | 3 | **NO-GO**: snapshot bỏ qua nhánh của chính chương trình — sửa ở `e365d84` |
| 2026-10-14 | F.10b (`e365d84`) | 4 | **NO-GO**: chỉ kiểm đích, agent reset được nhánh đã publish — sửa ở `d36a862` |
| 2026-10-14 | F.10b (`d36a862`) | 5 | **GO** |
| 2026-10-14 | R.M1/R.M6 (`b889d86`) | 1 | **NO-GO**: 2 HIGH (import động lọt; cạnh script dò sót) + 4 MED (ngoại lệ theo vùng, import tương đối, tiền tố vùng chung, CLI trong vòng lặp) — sửa ở `7548c6d` |
| 2026-10-14 | R.M1/R.M6 (`7548c6d`) | 2 | **NO-GO**: FIXED 3/6; alias loader, định danh script, mốc độc lập — sửa ở `a4b2ecd` |
| 2026-10-14 | R.M1/R.M6 (`a4b2ecd`) | 3 | **NO-GO**: `proc.run` qua import đầy đủ; mốc rỗng không kiểm — sửa ở `cbb8275` |
| 2026-10-14 | R.M1/R.M6 (`cbb8275`) | 4 | **GO** |
| 2026-10-14 | R.M3 (`f8c2c15`) | 1 | **NO-GO**: HIGH race phá khoá cũ (thừa hưởng từ code gốc) + MED chỗ dò .env, khoá trùng — sửa ở `4c7befb` |
| 2026-10-14 | R.M3 (`4c7befb`) | 2 | **GO** |
| 2026-10-14 | R.M4 lát 1 (`51e0f5f`) | 1 | **NO-GO**: HIGH phản hồi rỗng = thành công (đăng trùng); MED ghi không chắc bị coi là bị từ chối — sửa ở `3582009` |
| 2026-10-14 | R.M4 lát 1 (`3582009`) | 2 | **NO-GO**: đối chiếu inline chỉ 100 discussion / chỉ số < 200 — sửa ở `c8ba4f4` |
| 2026-10-14 | R.M4 lát 1 (`c8ba4f4`) | 3 | **GO** |
| 2026-10-14 | F.10c (`9abc9d6`) | 1 | **NO-GO**: 2 HIGH (file output cũ cho kết quả đạt giả; record ghi ra ngoài repo qua link) + 4 MED (đường dẫn output, khoảng trắng đầu snapshot, lộ secret trong lỗi, Infinity) — sửa ở `a281699` |
| 2026-10-14 | F.10c (`a281699`) | 2 | **NO-GO**: record không kiểm lại output sau khi chạy lệnh — sửa ở `855ff4b` |
| 2026-10-14 | F.10c (`855ff4b`) | 3 | **GO** |
| 2026-10-14 | F.10d (`eb6a41c`) | 1 | **NO-GO**: HIGH tool lỗi bỏ qua bước kiểm an toàn + MED publish id cache, xu hướng ratchet sai — sửa ở `283f333` |
| 2026-10-14 | F.10d (`283f333`) | 2 | **GO** |
| 2026-10-15 | F.13 (`b7999b0`) | 1 | **NO-GO**: 3 MED (đọc slot chặn xoá trên Windows, ngưỡng stale lệch runner, view chỉ đọc mở Store ghi) — sửa ở `1b32342` |
| 2026-10-15 | F.13 (`1b32342`) | 2 | **NO-GO**: MED default_path() tạo thư mục state — sửa ở `75c17ca` |
| 2026-10-15 | F.13 (`75c17ca`) | 3 | **GO** |
| 2026-10-15 | F.14 (`f9d8b6c`) | 1 | **NO-GO**: HIGH lỗi/trace có thể lộ secret + 4 MED (thư mục duyệt lệch, forward gọi mkdir/tasklist, 1 reader chậm chặn mọi request, section lỗi = "không có gì") + LOW xong-24h — sửa ở `4d409b1` |
| 2026-10-15 | F.14 (`4d409b1`) | 2 | **NO-GO**: HIGH redact chỉ theo tên khoá; MED file đọc lỗi bị bỏ qua; LOW xong-24h bị giới hạn 200 — sửa ở `3c9fe55` |
| 2026-10-15 | F.14 (`3c9fe55`) | 3 | **NO-GO**: HIGH SAFE_KEYS vẫn nhận chuỗi không tin cậy; MED việc lỗi rơi khỏi danh sách — sửa ở `2431579` |
| 2026-10-15 | F.14 (`2431579`) | 4 | **NO-GO**: HIGH khoá dict không qua redact — sửa ở `2a374b2` |
| 2026-10-15 | F.14 (`2a374b2`) | 5 | **GO** |
| 2026-10-15 | F.15 (`e3deb05`) | 1 | **NO-GO**: 3 MED (request trùng khi gọi đồng thời, cache không theo danh tính, timeout không chặn được worker) + LOW dạng JSON lạ — sửa ở `7fd7bca` |
| 2026-10-15 | F.15 (`7fd7bca`) | 2 | **NO-GO**: MED `read(8192)` bị server nhỏ giọt giữ lâu — sửa ở `30eb98b` |
| 2026-10-15 | F.15 (`30eb98b`) | 3 | **NO-GO**: MED request bị bỏ ghi đè kết quả mới hơn — sửa ở `9fae379` |
| 2026-10-15 | F.15 (`9fae379`) | 4 | **GO** |
| 2026-10-15 | F.16 (`e502f29`) | 1 | **NO-GO**: HIGH backtick escape lách bọc mention; MED status lạ làm validate crash; LOW `|` trong đường dẫn — sửa ở `9728544` |
| 2026-10-15 | F.16 (`9728544`) | 2 | **NO-GO**: HIGH `\v`/fence trong list lệch với markdown — sửa ở `08c0e90` |
| 2026-10-15 | F.16 (`08c0e90`) | 3 | **NO-GO**: HIGH nhãn link-ref có `\]`/xuống dòng — sửa ở `bd684f9` |
| 2026-10-15 | F.16 (`bd684f9`) | 4 | **NO-GO**: HIGH HTML entity (`&#64;`) — sửa ở `3baa9c5` |
| 2026-10-15 | F.16 (`3baa9c5`) | 5 | **NO-GO**: HIGH `_@all_` (gạch dưới đứng trước) — sửa ở `3828852` |
| 2026-10-15 | F.16 (`3828852`) | 6 | **NO-GO**: HIGH `@\_x` (markdown bỏ escape) — sửa ở `f266a46` (trung hoà MỌI `@`) |
| 2026-10-15 | F.16 (`f266a46`) | 7 | **NO-GO**: HIGH `%40` trong link — sửa ở `d3f94b1` |
| 2026-10-15 | F.16 (`d3f94b1`) | 8 | **NO-GO**: HIGH fence `json:table` render markdown trước bộ lọc tag — sửa ở `f7caaca` |
| 2026-10-15 | F.16 (`f7caaca`) | 9 | **NO-GO**: HIGH `json table` (tham số cách khoảng) — sửa ở `7e4b28f` |
| 2026-10-15 | F.16 (`7e4b28f`) | 10 | **NO-GO**: HIGH entity không `;` trong thuộc tính HTML — sửa ở `f2b568a` (vòng 10 viết lại yêu cầu theo hướng phòng thủ: bộ lọc OpenAI chặn lần đầu) |
| 2026-10-15 | F.16 (`f2b568a`) | 11 | **NO-GO**: HIGH fence mở thụt 1–3 space ghép cặp khác — sửa ở `267656d` |
| 2026-10-15 | F.16 (`267656d`) | 12 | **NO-GO**: HIGH fence dài >255 (giới hạn cmark-gfm) — sửa ở `0922cd5` |
| 2026-10-15 | F.16 (`0922cd5`) | 13 | **NO-GO**: MED suggestion chỉ có code mất fence — sửa ở `7e30119` |
| 2026-10-15 | F.16 (`7e30119`) | 14 | **NO-GO**: MED Date theo mtime làm hỏng hash duyệt; LOW CR trong ô bảng — sửa ở `e502977` |
| 2026-10-15 | F.16 (`e502977`) | 15 | **GO** |
| 2026-10-16 | F.17 (`29c72b0`) | 1 | **NO-GO**: 3 MED (kiểm trang cũ ngoài khoá, `os.replace` lỗi trên Windows, sửa lần đầu khi chưa có store bị coi là cũ) + 2 LOW (so ô chọn nhiều theo thứ tự, JSON gốc không phải object) — sửa ở `d90a319` |
| 2026-10-16 | F.17 (`d90a319`) | 2 | **NO-GO**: MED ký tự phân dòng lạ làm 4 parser đọc lệch; MED từ khoá có dấu cách bị tách — sửa ở `ca9d870` |
| 2026-10-16 | F.17 (`ca9d870`) | 3 | **NO-GO**: MED tự duyệt của group là CỘNG THÊM chứ không thay thế — mô tả + cảnh báo ở `b9e352b` |
| 2026-10-16 | F.17 (`b9e352b`) | 4 | **NO-GO**: MED đổi TELEGRAM_AUTO_APPROVE cũng phải restart group_watch — sửa ở `449093c` (commit này lọt 1 test chập chờn; sửa ở `c834309`: đọc body trước khi trả lời) |
| 2026-10-16 | F.17 (`c834309`) | 5 | **GO** |
| 2026-10-16 | F.18 (`cb5683c`) | 1 | **NO-GO**: 3 MED (inline_done cũ khi đã tắt inline, cắt câu trong code span nhiều backtick, tên file trùng) — sửa ở `e6c1614` |
| 2026-10-16 | F.18 (`e6c1614`) | 2 | **NO-GO**: MED `**` trong code span bị coi là đậm — sửa ở `ed16514` |
| 2026-10-16 | F.18 (`ed16514`) | 3 | **NO-GO**: MED bỏ `**` lẻ có thể xoá hết nội dung — sửa ở `fa3d5d9` (đóng thay vì cắt) |
| 2026-10-16 | F.18 (`fa3d5d9`) | 4 | **GO** |
| 2026-10-16 | F.19 (`fbec9de`) | 1 | **NO-GO**: MED dòng Why/Suggestion gộp thành một đoạn; MED nhãn file trùng — sửa ở `7bbfa26` |
| 2026-10-16 | F.19 (`7bbfa26`) | 2 | **GO** |
| 2026-10-16 | F.20 (`38de4ab`) | 1 | **NO-GO**: 2 HIGH (exit 0 chưa xác nhận bị coi là đã gửi, crash trước khi có trạng thái báo) + 4 MED (gửi trùng sau crash, giới hạn theo job, close_idle ghi đè, che token sau khi cắt) — sửa ở `b68b764` |
| 2026-10-16 | F.20 (`b68b764`) | 2 | **NO-GO**: 2 HIGH (timeout có thể đã gửi, lưu thông báo đè job vừa xếp lại) — sửa ở `cd247fd` |
| 2026-10-16 | F.20 (`cd247fd`) | 3 | **NO-GO**: 2 MED (tiến trình con chết = có thể đã gửi; lưu 'posted' làm mất người yêu cầu mới) — sửa ở `509a8c9` |
| 2026-10-16 | F.20 (`509a8c9`) | 4 | **NO-GO**: MED các lần lưu giữa chừng (inline) vẫn ghi đè — sửa ở `38a16fb` |
| 2026-10-16 | F.20 (`38a16fb`) | 5 | **GO** |
| 2026-10-17 | F.21 (`5ca96b5`) | 1 | **NO-GO**: 2 HIGH (con cháu sống sót khi reload, Ctrl+C lúc khởi động/dừng) + 3 MED (git lỗi = đổi, sửa chưa commit, khởi động lỗi thoát luôn) — sửa ở `5ecb961` |
| 2026-10-17 | F.21 (`5ecb961`) | 2 | **NO-GO**: 3 HIGH (con sinh trước khi gán job, Ctrl+C khi khởi động, POSIX mồ côi sau crash) — sửa ở `a833d9a` |
| 2026-10-17 | F.21 (`a833d9a`) | 3 | **NO-GO**: HIGH macOS mồ côi; MED zombie; MED rò handle job — sửa ở `c87b43b` |
| 2026-10-17 | F.21 (`c87b43b`) | 4 | **NO-GO**: HIGH POSIX con sinh trong lúc kill; MED rò handle khi SetInformationJobObject lỗi — sửa ở `8c49685` |
| 2026-10-17 | F.21 (`8c49685`) | 5 | **NO-GO**: MED Ctrl+C khi reload bị mất; HIGH chạy bản mới khi bản cũ còn sót — sửa ở `4652211` |
| 2026-10-17 | F.21 (`4652211`) | 6 | **NO-GO**: HIGH thiếu argtypes Win32 / query lỗi = rỗng; MED Ctrl+C khi khởi động lỗi — sửa ở `76ce948` |
| 2026-10-17 | F.21 (`76ce948`) | 7 | **NO-GO**: 2 MED POSIX (PID tái dùng, quét lỗi = rỗng) — sửa ở `448e2c9` |
| 2026-10-17 | F.21 (`448e2c9`) | 8 | **NO-GO**: MED EPERM bị coi là đã chết — sửa ở `8cc1177` |
| 2026-10-17 | F.21 (`8cc1177`) | 9 | **GO** |
| 2026-10-18 | F.22 (`6a685ed`) | 1 | **NO-GO**: 4 MED (file tạm trùng, đọc chậm thành 'không biết' 15 phút, đổi thư mục tài khoản, thời điểm/% lạ) — sửa ở `4b72a5d` |
| 2026-10-18 | F.22 (`4b72a5d`) | 2 | **NO-GO**: MED dashboard làm mới lỗi tạm thời — sửa ở `1d3d2cc` |
| 2026-10-18 | F.22 (`1d3d2cc`) | 3 | **GO** |
| 2026-10-18 | F.23 (`0401530`) | 1 | **NO-GO**: 7 MED (JSON sai schema crash doctor, file rỗng tính là có, kubeconfig chỉ trong registry, kubectl suy từ Lens, báo sai .env, Jenkins thiếu USER, bỏ qua bằng chứng đăng nhập) — sửa ở `6b56ccc` |
| 2026-10-18 | F.23 (`6b56ccc`) | 2 | **NO-GO**: 5 MED (doctor Jenkins, env đăng nhập kế thừa, kubectl rỗng/không execute, GH_CONFIG_DIR, entry project sai) — sửa ở `cd66480` |
| 2026-10-18 | F.23 (`cd66480`→`201e8b1`) | 3–9 | **NO-GO** mỗi vòng 1–2 MED hẹp (kubectl qua PATH/KUBECTL_BIN nguyên văn/Lens mới nhất/bản ghim, env tài khoản như agent_runner, PATH tài khoản, CODEX_HOME nguyên văn, VPN USER, GH_TOKEN) — sửa ở `96244c7`, `0702a53`, `87afeca`, `50e9008`, `612cf95`, `9dce432`, `201e8b1` |
| 2026-10-18 | F.23 (`201e8b1`) | 10 | **NO-GO**: MED khối k8s không ghim bị bỏ qua — sửa ở `2e947f8` |
| 2026-10-18 | F.23 (`2e947f8`) | 11 | **GO** (phân loại HIGH/MED/LOW nghiêm ngặt; không có HIGH ở mọi vòng) |
| 2026-10-20 | F.24 (`903030c`) | 1 | **NO-GO**: 6 HIGH (secret lộ qua mã lỗi khác, git lỗi = sạch, dòng dài/file lớn bỏ qua im lặng, `.env`/JSON không quét, `${VAR:mặc-định}` được miễn, mapping ANY khớp luật theo method) + 5 MED (dấu vân tay trùng, YAML list/block, `-prod-us`, chiều cờ TLS, miễn XXE cả file) — sửa ở `46b229f` |
| 2026-10-20 | F.24 (`46b229f`) | 2 | **NO-GO**: 4 HIGH (nháy escape, span chồng, JSON cắt 16k, match dài qua ranh giới đoạn) + 2 MED (miễn XXE theo giá trị/biến, miễn version mọi khoá) — sửa ở `e7acca5` |
| 2026-10-20 | F.24 (`e7acca5`→`055e4e1`) | 3–11 | **NO-GO** mỗi vòng 1–3 điểm, chủ yếu che secret theo cú pháp (`\` cuối chuỗi, template, `"a"+"b"`, `obj["password"]=`, setter, nháy trong regex literal) + XXE từng factory/gán lại + ngữ cảnh dependency JSON theo cấu trúc — sửa ở `2b4eb94`, `9298110`, `7e50461`, `96cce30`, `376b99e`, `709bd04`, `41f4943`, `055e4e1`, `17e90aa`; chốt cách che bảo thủ (cắt dòng từ dấu nháy đầu) |
| 2026-10-20 | F.24 (`17e90aa`) | 12 | **GO** |
| 2026-10-17 | Lộ trình sản phẩm (`docs/design/PRODUCT-ROADMAP.md`) | 1→4 | **GO** vòng 4 (v1–v3 GO-WITH-CHANGES: 7→3→1 HIGH; 2 LOW vòng 4 đã đưa vào v4.1) — thiết kế, chưa triển khai |
