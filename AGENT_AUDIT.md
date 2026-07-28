# Audit: Mô tả vs Thực thi (Description vs Enforcement)

> Mục đích: với mỗi "lời hứa" về an toàn / điều phối / học hỏi trong agent này, xác định nó
> được **code enforce thật** hay chỉ là **văn bản dựa vào agent tuân thủ**. Đây là bản đồ nền
> để quyết định "sửa gì trước" dựa trên bằng chứng, không phải phỏng đoán.
>
> **Phương pháp:** đọc trực tiếp source (không dựa vào SKILL.md/cookbook). Mọi verdict đều trích
> `file:line` đã kiểm chứng tận tay. Ngày lập: 2026-07-27.

## Thang verdict

| Ký hiệu | Nghĩa |
|---|---|
| 🟢 **ENFORCED** | Code chặn thật. Agent không đi tiếp được nếu chưa thỏa. |
| 🟡 **PARTIAL** | Code có enforce nhưng có escape hatch, hoặc chỉ đúng trong 1 chế độ / phụ thuộc input truyền đúng. |
| 🔴 **PROSE-ONLY** | Chỉ là văn bản/hướng dẫn. An toàn = agent tự nguyện tuân theo. |
| ⚫ **INERT** | Code tồn tại nhưng chưa nối vào luồng thật / chưa từng chạy. |

---

## Bảng 1 — Gate & Checkpoint an toàn

| # | Lời hứa (mô tả) | Nơi nêu | Thực thi trong code | Verdict |
|---|---|---|---|---|
| 1 | Gate `clarity`/`test`/`lint`/`review`/`ac` chặn advance nếu chưa pass | SKILL.md | `policy()` chặn khi `mode=="auto"` (run_log.py:303-305); ở `checkpoint` chỉ trả `missing` + `allowed=True` (306-311) | 🟡 |
| 2 | Mặc định an toàn | — | Mode mặc định = **checkpoint** (run_log.py:115,137) → mặc định là **INFORM, không chặn** | 🟡 |
| 3 | "Never pass a checkpoint without approval" (`after_plan`/`before_mr`/`before_notify`) | SKILL.md:34 | `policy()` **không hề đọc** checkpoint dict — chỉ đọc gate. `cmd_checkpoint` chỉ set `state["checkpoints"][name]=status` (run_log.py:176-184), **chính agent tự gọi được**. Checkpoint là state trang trí. | 🔴 |
| 4 | Gate cứng là bất khả xâm phạm | SKILL.md | `advance --force` ghi state kể cả gate chưa thỏa, chỉ để lại note (run_log.py:372-377) | 🟡 |
| 5 | Thay đổi chạm runtime bắt buộc phải verify | SKILL.md:133-140 | `touches_runtime()` = **regex tên file** (`Controller\|Service\|Repository...` verify_gen.py:52). Có target nhưng **không khớp regex → trả `False`** (verify_gen.py:96-104) → verify KHÔNG được require → merge chỉ với unit test. **Lỗ thật.** | 🔴 |
| 6 | "Never deliver on red tests" / confirm trước notify/delete/close | SKILL.md:173, CLAUDE.md guardrails | Không có code chặn tương ứng; nằm trong prompt. An toàn = agent obey. | 🔴 |
| 7 | Fix test đỏ giới hạn số lần, đỏ mãi thì DỪNG | SKILL.md:93 | `max_attempts` (mặc định 3) enforce thật, state ở `_fixloop.json` sống qua lần gọi (fix_loop.py:276,289,323) | 🟢 |
| 8 | Diff MR lớn được cảnh báo khi bị cắt | — | `MAX_DIFF_CHARS=200_000`; JSON **có** field `diff_truncated` (review_gate.py:33,71,91). *Cảnh báo tồn tại*; việc đọc/hành động theo nó thì là prose. | 🟢 (phơi cờ) / 🔴 (hành động) |

---

## Bảng 2 — Điều phối & thứ tự (orchestration)

| # | Lời hứa | Nơi nêu | Thực thi | Verdict |
|---|---|---|---|---|
| 9 | Luồng chuẩn queue → prep → run → resolve | commands/*.md | Chỉ là **quy ước**. Không có state ép đúng thứ tự giữa các skill. | 🔴 |
| 10 | `/atask-run` dừng nếu task chưa `approved` | atask-run.md:52 | Là **câu hướng dẫn**, không phải validation code. Gọi run thẳng vẫn chạy. | 🔴 |
| 11 | Chống 2 luồng tự động cùng làm 1 task | atask-run/queue | Lock per-flow (owner `task_resolver`) có thật cho queue/run. | 🟢 |
| 12 | Chống `run` và `resolve` xung đột trên cùng task | atask-resolve.md:35 | State `atask_resolved.json` chỉ chống **resolve lấy trùng cùng trạng thái**, KHÔNG chống run-đang-code vs resolve-đóng-task. | 🔴 |

---

## Bảng 3 — Vòng lặp học & tự động hóa nền

| # | Lời hứa | Nơi nêu | Thực thi | Verdict |
|---|---|---|---|---|
| 13 | Agent học từ can thiệp người: recall bơm bài học cũ vào run sau | SKILL.md:125 | recall tự gọi **chỉ** ở `task_queue.intake` (task_queue.py:290-301) + debate qua flag `--corrections-file` thủ công. **Không** tự gọi ở review/triage. | 🟡 |
| 14 | Sau mỗi checkpoint, ghi feedback (edited/approved/rejected) | pipeline.md | `tg_gate.cmd_parse` chỉ **trả về chuỗi hướng dẫn** "hãy ghi `feedback.py add`" (tg_gate.py:171), **không tự ghi**. Không code nào kiểm tra agent có ghi hay không. | 🔴 |
| 15 | Auto-fix thành công được học lại | — | `fix_loop` tự gọi `feedback.cmd_add` khi xanh (fix_loop.py:223-233,307-313) — đây là chỗ DUY NHẤT ghi feedback tự động | 🟢 |
| 16 | Daemon `mr_watch` theo dõi & tự review MR | watch-mrs | Logic poll/supervise/backoff đầy đủ (mr_watch.py, daemon_common.py) nhưng **không có cron/systemd/auto-start** — phải chạy tay. | ⚫ |
| 17 | Bảng trạng thái tổng hợp | status.py | Đọc 5 nguồn state, hoạt động. Nhưng **read-only**, không phải control plane. | 🟢 (đọc) |
| 18 | Sổ học từ phản hồi (feedback ledger) | feedback.py | `work/feedback/` **chưa tồn tại**. Chưa có `run_log` thật nào (chỉ 1 artifact intake tay `manual-t1`). Toàn bộ vòng lặp **chưa từng chạy end-to-end**. | ⚫ |

---

## Phát hiện cốt lõi

1. **Checkpoint an toàn là ảo giác an toàn.** Ba mốc duyệt người (`after_plan`, `before_mr`,
   `before_notify`) — thứ được quảng bá là lớp bảo vệ chính — `policy()` **không đọc chúng**.
   Chính agent set `checkpoint approved` được. Không gì buộc phải có người duyệt thật (#3).

2. **Gate cứng chỉ cứng ở `mode=auto`, mà mặc định lại là `checkpoint`.** Nghĩa là ở cấu hình
   mặc định, gate fail chỉ *thông báo* rồi cho đi tiếp (#1, #2). Cộng thêm `--force` bỏ qua cả
   auto (#4).

3. **Lỗ verify runtime là rủi ro kỹ thuật cụ thể nhất.** An toàn "chạm runtime → phải verify"
   phụ thuộc **tên file khớp regex**. File chạm DB/behavior mà tên không khớp → ship chỉ với
   unit test (#5). Đây là chỗ dễ gây bug lọt production nhất khi chạy không giám sát.

4. **Vòng lặp học đúng như memory ghi: rỗng và chưa cắm điện.** Hạ tầng đủ, nhưng ghi feedback
   dựa vào agent tự nhớ (#14), recall chỉ nối 1 chỗ (#13), daemon chưa từng chạy (#16), ledger
   chưa tồn tại (#18). "Đường ống có, nước chưa chảy."

5. **Điều phối giữa các skill là quy ước, không phải rule.** Quên bước giữa → task kẹt; gọi sai
   thứ tự → không bị chặn; run vs resolve có thể đóng task khi code chưa xong (#9-#12).

## Ý nghĩa cho việc "giao nhiều việc + tin tưởng"

Mẫu hình lặp lại: **an toàn được mã hóa vào PROSE nhiều hơn vào CODE/GATE/STATE.** Điều này
an toàn *khi có người ngồi giám sát* (checkpoint mode + mắt người lấp mọi lỗ prose) — nhưng đó
đúng là điều kiện biến mất khi bạn delegate. Muốn tin tưởng giao việc **không giám sát**, thứ tự
sửa theo rủi ro:

- **P0 (rủi ro cao, chạy-không-người sẽ cắn):** #5 verify fail-safe · #3 checkpoint thành gate thật cho thao tác không đảo ngược (notify/merge/close/delete) · #12 lock run-vs-resolve.
- **P1 (làm hệ thống tự đứng được):** #14 ghi feedback tự động (enforce) · #13 recall nối vào review/triage · #16 daemon tự sống · chạy 1 pilot thật để #18 có dữ liệu.
- **P2 (dễ dùng đúng):** #9-#10 gộp entry point + ép thứ tự bằng code · #4 giới hạn `--force`.

> Ghi chú độ tin cậy: mọi verdict trong file này đã đọc source tận tay. Riêng các dòng gắn
> "🔴 (hành động)" là **[Suy luận]** dựa trên việc code chỉ *phơi* thông tin chứ không *ép* hành
> động — mức độ tuân thủ thực tế của agent chưa được đo bằng một lần chạy thật nào.
