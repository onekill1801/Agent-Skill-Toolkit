# Bảng việc nhóm qua FPT Chat — định danh · thẻ việc · bản đồ môi trường (thiết kế v2)

> Trạng thái: **v3 — Codex ACCEPT + Fable ACCEPT (vòng 3, 30/10/2026)**, duyệt ở mức thiết kế; mọi thao tác ghi
> vẫn chờ spike S1. Vòng 1 REVISE×2 → v2; vòng 2 Codex REVISE (2 P1) + Fable ACCEPT có điều kiện → v3.
> v1–v3 + phản biện ở `temp/codex/workboard/`. Điều kiện khi code: §12. Phạm vi: bot trong Agent-Skill-Toolkit + extension `fpt-chat-quick-reply`
> (UI v1.5.0, định dạng tin `PROTOCOL.md`). Bố cục theo `agent-ecosystem.md §B`.

## 0. Vấn đề và mục tiêu

Nhóm "New Group" (12 người), 60 tin 28–29/10: 20 yêu cầu, 19 ack, 18 kết quả; 41/60 tin từ tài khoản chủ nhóm
(bot gửi bằng tài khoản người); 0 reply/reaction; etask!1388 hỏi 9 lần; etask!1394 ack mà không có kết quả.

- G1. Lịch sử có ý nghĩa: mỗi việc có mã, trạng thái, "ai làm gì tiếp".
- G2. Thành viên tự thao tác (review, aTask, deploy, duyệt) bằng tài khoản CỦA HỌ; bot kiểm quyền.
- G3. Minh bạch môi trường: repo × env chạy phiên bản/commit nào, rollout lúc nào, MR đã có ở đâu — luôn kèm link
  gốc + mức xác minh để tự kiểm khi toolkit sai.
- G4. Dễ debug: mọi quyết định có dấu vết (tin nguồn, người, lệnh, thẻ, nguồn dữ liệu, thời điểm).

Ngoài phạm vi: deploy/duyệt PROD qua chat; tài khoản bot riêng; edit/reply/reaction/pin (tới khi §1.1 xác minh).

## 1. Giả định nền và điều phải xác minh TRƯỚC (Pha 0 — chặn mọi thứ ghi)

### 1.1 Spike bắt buộc
| # | Kiểm | Vì sao | Nếu thất bại |
|---|---|---|---|
| S1 | Gửi khung WS với `senderId` SAI (Saved Messages) → server từ chối hay ghi đè bằng id thật? | `send.py:61-72` để client điền `senderId`. Nếu server tin giá trị client ⇒ ai cũng giả danh được | **Dừng G2** (không lệnh ghi qua chat); chỉ giữ G1/G3 đọc |
| S2 | Tin lịch sử/WS có trả lại đúng `requestId` client gửi? | Outbox §3.3 khớp theo requestId | Khớp theo hash(content)+thời gian (yếu hơn) |
| S3 | Thu hồi tin để lại dấu gì trong nhóm/lịch sử? | §4.4 | Không dùng thu hồi |
| S4 | Wire format edit/reply/reaction/pin (người dùng thao tác tay, hook WS chỉ đọc trong Orca) | 1 tin/thẻ cập nhật tại chỗ | Giữ "tin mới mỗi lần đổi trạng thái" |
| S5 | GitLab: tags API, `merge_base`, `/commits/:sha/merge_requests` với token hiện tại | §5 | Hạ mức xác minh |
| S6 | Job nào có git tag / build SHA / image digest; k8s: quyền đọc pod (image digest, readiness) ở env nào | §5 | Env đó tối đa "chưa xác minh" |
| S7 | aTask: tạo thay người khác (created_by) | §6 | Thẻ T ghi rõ "tạo bằng tài khoản <chủ nhóm>" |
| S8 | Job deploy nào nhận tham số SHA/tag (hoặc ops job sinh từ registry nhận SHA) | Ghim commit §3.5 | Repo/env đó không deploy qua chat (trừ dev opt-in) |

### 1.2 Mô hình tin cậy
- **Quyền chỉ theo `senderId` của tin** (sau khi S1 xác nhận server-enforced). `metadata.mentions` do client dựng
  (`send.py:77-99`) ⇒ **không bao giờ dùng mention để cấp quyền**; mention chỉ chọn đối tượng, resolve lại qua danh bạ
  (thành viên nhóm, liên kết duy nhất).
- Tài khoản chủ nhóm là **kênh dùng chung người + bot** ⇒ không đủ làm bằng chứng "người đã gõ" cho thao tác đặc quyền.
  Duyệt deploy/đăng ký quyền của chủ nhóm đi qua **Telegram gate** hiện có (`tg_gate`, proof), không qua chat.
- Extension chỉ tin thẻ có cấu trúc khi `senderId` == `botSenderId` cấu hình (chặn thành viên đăng thẻ giả
  "thành công"); thẻ từ nguồn khác hiện như tin thường.

## 2. Kiến trúc

```
FPT Chat group ──WS──► inbox (bền, unique group+messageId) ──► intake (parse lệnh neo ^)
                                                                  │ tin thuộc outbox ⇒ bỏ (tin của bot)
                                                                  ▼
                                identity.resolve(senderId) ─► policy.check(action, member, card@revision)
                                                                  ▼
                       cards (store, revision) ◄── workflows: review | task | deploy | envmap
                                  │  render (PROTOCOL)                  │ integrations: gitlab · jenkins · k8s · atask
                                  ▼                                     ▼
                        send.py: outbox ghi TRƯỚC khi gửi ──► FPT Chat
env_watch (định kỳ) ─► env_snapshots ─► diff ─► 1 tin thay đổi/env + cập nhật thẻ (store) + bản tổng hợp
```
- P1. Store là sự thật; chat là bản hiển thị dựng lại được.
- P2. Lịch sử chat chỉ dùng để **phục dựng/hiển thị, không bao giờ để thực thi** lệnh.
- P3. Mọi thao tác ghi: inbox → policy → transition nguyên tử → integration (idempotent) → event.
- P4. Mọi dữ kiện môi trường: nguồn + link + mức xác minh + thời điểm quan sát; nguồn lệch ⇒ báo lệch, không chọn.

## 3. Định danh, inbox/outbox, quyền

### 3.1 Danh bạ `work/team.json`
Giữ map phẳng hiện tại của `team.py` (`members.<key>.{name, handles{…}}`) và **thêm** `perms: []`, `verified_by`,
`verified_at` (migration không phá dữ liệu cũ; khoá lạ được giữ nguyên).
`perms`: `review` · `task` · `deploy:<env>` · `approve:<env>` · `admin`; env ∈ {dev, sandbox, demo}.
Đăng ký: `@bot đăng ký gitlab=… atask=…` ⇒ `pending`; chỉ admin xác nhận qua `team.py verify` (terminal) hoặc
Telegram. Username GitLab tồn tại ≠ sở hữu ⇒ xác minh do admin, không tự động.

### 3.2 Inbox (chống replay)
Bảng `inbox(group_id, message_id, message_id_inc, sender_id, created_at, status, card_id, outcome)` UNIQUE
(group_id, message_id). Watermark bền theo nhóm. Khởi động lại: catch-up lịch sử **không thực thi**, nhưng lệnh gửi trong
lúc bot dừng (≤ 12 giờ) được **trả lời một lần** (gộp 1 tin): "bot vừa khởi động lại — lệnh lúc HH:MM của @X chưa
xử lý, gửi lại" (không nuốt im lặng như ca !1394). Lệnh `createdAt` cũ hơn 15 phút lúc nhận ⇒ `stale`, trả lời tương tự.
Thay `seen = set()` trong RAM (`group_watch.py:483-487`).

### 3.3 Outbox (nhận ra tin của chính bot)
- `send.py` nhận `request_id` từ caller (hoặc tự sinh) và **ghi `outbox(request_id, group_id, sha256(content),
  card_id, state='sending')` TRƯỚC khi mở WS**; echo ⇒ `sent` + `message_id_inc`; lỗi ⇒ `uncertain`.
  Mọi đường gửi đều qua `send.send_text` (kể cả `notify_group.py` mà `review_post` gọi) ⇒ một chỗ.
  Outbox nằm trong **đúng một DB** (`store.default_path()`, qua `_runtime/bootstrap`); `review_post` gọi subprocess
  với env đã lọc ⇒ truyền tường minh đường DB; test bắt buộc "subprocess ghi outbox, watcher đọc thấy".
- Intake: tin từ tài khoản chủ nhóm khớp outbox (requestId; dự phòng hash content + ±2 phút) ⇒ tin bot, bỏ.
- Tài khoản chủ nhóm chỉ ra lệnh khi có tiền tố `@bot` (giữ `_strip_self_prefix`) VÀ không khớp outbox;
  outbox không đọc được ⇒ **mọi lệnh ghi từ tài khoản chủ nhóm bị chặn** (fail-closed).
- Render bot: không dòng nào bắt đầu bằng động từ lệnh / `@bot` / `🤖 [` ngoài dòng đầu của chính nó (golden test);
  kết quả agent tự do (`_post_results`) bị lọc cùng luật.

### 3.4 Luật (policy) — kiểm lúc nhận VÀ lúc thực thi
| Hành động | Điều kiện |
|---|---|
| review / review lại R-n | `review` |
| tạo task (nháp) | `task`; người được giao: thành viên đã liên kết `atask_user_id` duy nhất |
| xác nhận / huỷ T-n | chính người yêu cầu (senderId) |
| xong T-n | người được giao/người yêu cầu ⇒ bot hỏi lại `xác nhận xong T-n` (đóng task trên aTask = trạng thái cuối ở hệ thống ngoài — guardrail 2) |
| deploy repo env | `deploy:<env>`; nhánh = nhánh job hiện hành (đọc cấu hình job lúc yêu cầu) |
| duyệt / từ chối D-n | `approve:<env>`, ≠ người yêu cầu, ≠ tài khoản chủ nhóm (chủ nhóm duyệt qua Telegram) |
| huỷ D-n | người yêu cầu, khi `chờ duyệt` |
| deploy lại D-n | người yêu cầu ⇒ thẻ D mới, duyệt mới |
Từ chối luôn trả lý do + cách xin quyền.
Luật trạng thái cuối: lệnh làm thay đổi trạng thái cuối **ở hệ thống ngoài** (đóng task aTask) phải xác nhận lại 1 lần;
`huỷ T-n` (bản nháp, chưa tạo gì) và `huỷ D-n` (chưa chạy) chỉ đổi thẻ nội bộ ⇒ không hỏi lại.

### 3.5 Duyệt deploy (gate thật)
Thẻ D lúc tạo chốt **payload**: repo, env, job path, nhánh, `commit = HEAD(nhánh)`, revision cấu hình job (hash
config.xml nếu đọc được), người yêu cầu. `approval = {approver senderId, message_id, payload_hash, expires(+2h)}`
— dùng một lần. Lúc chạy: kiểm lại quyền, hạn, `HEAD(nhánh) == commit` và hash cấu hình ⇒ khác ⇒ huỷ, xin duyệt
lại. Transition `chờ duyệt → đang chạy` trong `begin immediate` (store) ⇒ hai người duyệt cùng lúc chỉ một thắng.
Lưu proof vào event: đường chat = (message id + senderId); đường Telegram (chủ nhóm) = proof của `tg_gate`
(callback id + user Telegram đã cấu hình). Không bao giờ tự khai `source=human`.

**Ghim commit (chặn đua HEAD).** Kiểm HEAD trước khi trigger vẫn hở: job checkout nhánh lúc chạy, HEAD có thể đổi
trong lúc chờ queue. Vì vậy:
- Job build hỗ trợ tham số SHA/tag ⇒ truyền `commit` đã duyệt ⇒ **ghim**. (Ops job của `jenkins_ops.py` chỉ nhận TAG
  image **đã build** ⇒ dùng được để *promote* artifact có sẵn, không phải build commit mới.)
- Thực tế etask: demo không tham số, sandbox hard-code nhánh ⇒ S8 nhiều khả năng thất bại ⇒ deploy qua chat ban đầu
  chỉ còn dev opt-in (hoặc promote image đã build). Nói rõ với người dùng, không hứa hơn.
- Job không hỗ trợ ⇒ **mặc định chặn deploy qua chat** cho repo/env đó. Ngoại lệ opt-in `allow_unpinned: true` theo
  env trong registry (chỉ dev): thẻ D ghi rõ "duyệt deploy **HEAD nhánh tại thời điểm chạy**, không ghim commit";
  sau build đọc SHA thực của build (build data) + digest image; khác `commit` lúc duyệt ⇒ thẻ `⚠ build khác commit đã
  duyệt` (không tính `thành công`), báo người duyệt.
- Mọi trường hợp: thẻ D lưu SHA build thực và so với payload.

## 4. Thẻ việc

### 4.1 Store
`cards(group_id, id, kind, ref, revision, state, title, requester, assignee, approver, author, env, payload json,
created, updated)` UNIQUE (group_id, kind, ref) cho thẻ mở; mã `R|T|D-<n>` cấp bằng bảng `seq(group_id, kind,
next)` trong cùng transaction. Thẻ R: `ref = <gitlab_project_id>!<iid>`. Dùng lại lease/checkpoint/`uncertain`
của `store.py` cho việc chạy (review, task create, Jenkins build); khoá theo `repo:env` cho deploy;
idempotency key cho tạo task/build; timeout ⇒ đối soát (task đã tạo? build đã chạy?) trước khi retry.
Migration có `schema_version`, backup `toolkit.db` trước khi nâng.

### 4.2 Trạng thái — như `PROTOCOL.md` (R/T/D), thêm D `huỷ`.

### 4.3 Định dạng tin (sửa PROTOCOL)
- Mention **inline** đúng offset (hàm mới `send.with_mentions(text, people)`), không dồn lên đầu.
- Dòng môi trường của R: `Dev: ✅ 29/10 · Sandbox: — · Demo: ?` (mỗi đoạn có `:` để parser đọc được).
- Thêm loại `E-<repo>-<env>` (môi trường; khoá gom (group, repo, env), không ghi đè giữa env) vào PROTOCOL + extension.
  Tin E liệt kê **mã R** của MR thêm/mất (`MR thêm: R-7 R-9`) để extension cập nhật chip môi trường của thẻ R
  (extension chỉ đọc tin chat, không đọc store).
- PROTOCOL: bỏ `[nhánh <b>]` khỏi lệnh deploy (nhánh = nhánh job hiện hành, §3.4); sửa form deploy của extension.
- Golden test chung: render (bot) ↔ parse (extension `board.js` — chạy bằng node trong test).

### 4.4 Giảm nhiễu
- MR đã có thẻ ⇒ trả ngay 1 dòng trỏ tới thẻ (không ack mới). MR mới: không gửi ack nếu kết quả có trong ≤ 3 phút;
  quá 3 phút gửi thẻ `chờ bot`. Không thu hồi tin (tới khi S3 rõ) —
  thẻ kết quả là tin mới, extension gom theo mã.
- Hỏi lại cùng MR khi head SHA chưa đổi và lượt trước thành công ⇒ 1 dòng trỏ tới thẻ cũ; `review lại R-n` để ép.
- `chờ bot` > 30 phút ⇒ bot kiểm job `review_flow` và đổi `lỗi` kèm lý do.

## 5. Bản đồ môi trường

### 5.1 Snapshot — khoá (repo, env, cluster, namespace, workload kind/name, container)
Hỗ trợ Deployment **và DeploymentConfig** (demo dùng `oc set image dc/…`), nhiều workload/repo (gateway…).
| Trường | Nguồn | Ghi chú |
|---|---|---|
| image (spec) + **digest từng pod** + ready | k8s (read-only) | spec ≠ đang chạy khi rollout dở |
| rollout_at | k8s: ReplicaSet/RC hiện hành + điều kiện rollout | không dùng giờ build làm giờ deploy |
| built_at, build_url, result | Jenkins build tạo ra image (tìm theo tag trong console) | SUCCESS = build xong, chưa phải đã chạy |
| commit | provenance: digest/tag → build → SHA (build data) hoặc git tag `<prefix><ver>` qua GitLab tags API | tag trùng tên ≠ cùng artifact ⇒ chỉ ✅ khi digest khớp build |
| branch_config | cấu hình job đọc **live** mỗi lần env_watch chạy (cache 1 giờ; 403 ⇒ dùng registry + tuổi `scanned_at`) | là nhánh BUILD, không phải "nhánh đang chạy" |
| observed_at | thời điểm quan sát | |

Mức xác minh: **✅ đã xác minh** (digest pod = digest build có SHA) · **🟡 suy ra** (khớp tag/tên, hoặc chỉ Jenkins) ·
**❔ chưa xác minh** (thiếu nguồn/quyền) · **⚠ lệch** (spec ≠ pod, build ≠ k8s, cấu hình cũ > 7 ngày).
Env không có quyền k8s ⇒ tối đa 🟡. Sandbox/prod dùng chung prefix tag + image repo ⇒ luôn đi từ k8s của env đó.

### 5.2 "MR có ở môi trường X?"
Commit của MR theo kiểu merge thực tế (`merge_commit_sha` | `squash_commit_sha` | ff: head SHA). Trả lời 4 mức:
**có (ancestor)** — kèm link compare `env_commit...mr_commit` · **nghi port** (không ancestor nhưng tìm thấy commit
cùng tiêu đề/cherry-pick trailer trên nhánh env — kiểm tay) · **không thấy commit gốc** (không ancestor, không thấy
dấu port — KHÔNG khẳng định "chưa có", vì cherry-pick đổi tiêu đề/không trailer vẫn có thể chứa thay đổi; kèm link
compare để kiểm tay) · **không xác định** (thiếu commit env / mức ❔). MR chưa merge ⇒ "chưa merge" (khẳng định được). Revert: diff giữa 2 snapshot liệt kê MR **thêm và mất**; MR có commit
revert sau đó ⇒ gắn cờ "có revert — kiểm tay".
MR giữa 2 commit: `/repository/commits/:sha/merge_requests` cho tối đa 50 commit; vượt ⇒ link compare + 🟡.

### 5.3 Báo
- Mỗi lần env đổi phiên bản ⇒ **1 tin `E-<repo>-<env>`**: phiên bản, commit, rollout_at, mức xác minh, MR thêm/mất
  (mã R nếu có), link build/tag/compare. Không gửi tin R riêng; thẻ R cập nhật trong store (extension đọc từ E).
- Dev đổi nhiều lần/ngày ⇒ gom vào bản tổng hợp 08:30; sandbox/demo báo ngay.
- Lệnh đọc: `@bot env <repo>`, `@bot MR <iid> <repo> ở đâu?` (trả lời theo yêu cầu, chi tiết đầy đủ).
- Không đăng namespace/registry/kubeconfig; chỉ phiên bản, commit, thời điểm, link. Prod tắt mặc định.

## 6. Task và deploy
**Task**: `@bot tạo task: <tiêu đề> | giao: @X | hạn: dd/mm/yyyy | mô tả: …` (regex neo `^`, không LLM) ⇒ T `chờ xác nhận`
⇒ `xác nhận T-n` ⇒ `tasks.py create` (idempotency key = card id) ⇒ `đã tạo` + link. Thẻ T ghi rõ "tạo bằng
tài khoản <chủ nhóm> thay <người yêu cầu>" (tới khi S7 có cách khác).
**Deploy**: `@bot deploy <repo> lên <env>` ⇒ policy ⇒ D `chờ duyệt` (payload §3.5, tag người có `approve:<env>`)
⇒ `duyệt D-n` ⇒ kiểm lại ⇒ Jenkins build (khoá `repo:env`) ⇒ đợi build ⇒ **đợi rollout k8s** (nếu có quyền) ⇒
`thành công` chỉ khi pod chạy digest mới; không có quyền k8s ⇒ `build xong — chưa xác minh rollout`.
Nhóm đã bật bảng việc: thay luồng build hiện tại (agent + Telegram) bằng luồng này; Telegram giữ cho chủ nhóm duyệt.

## 7. Thay đổi theo thành phần
| Thành phần | Việc |
|---|---|
| `send.py` | `request_id` từ caller; outbox trước gửi; `with_mentions` inline; `recall` dùng `tokens.ensure_fresh` |
| `core/store.py` | migration: `inbox`, `outbox`, `cards`, `seq`, `env_snapshots`, `schema_version` |
| `core/identity.py`, `core/policy.py` | danh bạ + perms; bảng §3.4 (test dạng bảng) |
| `workflows/group_board/` | `intake`, `cards` (state machine + revision), `render`, `review`, `task`, `deploy`, `envmap` |
| `integrations/gitlab` | tags, merge_base, compare, commit→MRs, MR merge/squash sha, head sha |
| `integrations/jenkins` | build info (number, result, timestamp, url, build SHA, image tag/digest từ console theo regex registry) |
| `integrations/k8s` | workload (Deployment/DC) + pods digest/ready + rollout |
| `group_watch.py` | vỏ: WS → inbox → intake; bỏ `_ACK` cho nhóm đã bật |
| `review_post.py` | `notice_text` → render thẻ R |
| `env_watch.py` | daemon dưới `supervise.py`; health; backoff 429; cache tag/commit |
| `team.py` | `perms`, `verify`, migration |
| extension | `botSenderId`; loại E; dòng `Dev: …`; parse mention inline; `BOT_SUPPORTS` theo pha |

## 8. Lộ trình
| Pha | Nội dung | Xong khi |
|---|---|---|
| 0 | Spike S1–S7; outbox trong `send.py`; inbox bền; `with_mentions` inline | S1 có kết luận; test crash trước/sau gửi |
| 1a | Thẻ R thay `notice_text`; dedupe head SHA; hạn 30′; giảm ack — **không lệnh mới** | số tin bot/ngày giảm; golden render↔parse |
| 1b | `review lại R-n`, `@bot tôi là ai`, policy `review` qua inbox/outbox | test replay/restart, outbox lỗi |
| 2a (song song 1b) | Đọc: `@bot env`, `MR … ở đâu` | so tay 3 repo × 3 env khớp link, ghi mức xác minh |
| 2b | E tự động + bản 08:30, shadow 2 ngày (ghi log, không gửi) | không báo sai trong shadow |
| 3 | Task (S1 đạt) | 1 task thật người dùng xác nhận |
| 4 | Deploy dev/sandbox + duyệt 4 mắt | 1 deploy dev có người duyệt khác; test duyệt đồng thời, HEAD đổi |
| 5 | Extension bật lệnh; edit/pin nếu S4 đạt | — |
Test bắt buộc trước pha 3/4: crash trước/sau gửi, restart + replay lịch sử, duyệt đồng thời, outbox lỗi, thẻ giả từ
thành viên, rollback, tag lệch, DC demo. Mỗi pha: code → test Windows → Codex review (bản chụp sạch) → commit;
CHECKLIST.md mục "8. Bảng việc nhóm".

## 9. Quan sát và đo
Inbox backlog, outbox `uncertain`, tuổi snapshot, độ trễ lệnh→phản hồi, số lệnh bị policy chặn (theo lý do), số lần
nguồn env lệch; mục tiêu sản phẩm: % thẻ R có phản hồi người, số thẻ quá hạn, số tin bot/ngày (trước/sau).
`@bot trace <mã>` trả dòng sự kiện của thẻ (cho admin).

## 10. Rủi ro còn lại
- S1 thất bại ⇒ G2 không làm được với kiến trúc này (cần cơ chế xác thực khác).
- Tài khoản chủ nhóm dùng chung người+bot: đã giới hạn (tiền tố `@bot`, outbox, chủ nhóm duyệt qua Telegram).
- Dữ liệu env sai do thao tác tay ngoài pipeline: phát hiện qua digest/lệch, không tự sửa.
- Link chỉ mở được với người có quyền; prod Jenkins etask 403 ngay cả với toolkit.
- Fallback outbox theo hash ±2′ có thể bỏ qua một tin thật của chủ nhóm trùng nguyên văn tin bot — chấp nhận
  (chủ nhóm gửi lại); ghi event `ignored_as_bot` để debug.
- Job không ghim được commit ⇒ deploy qua chat bị chặn (trừ dev opt-in) — có thể phải sửa pipeline hoặc dùng ops job.

## 11. Câu hỏi mở cho người dùng
1. Danh sách repo; có hiện prod không. 2. Ai `admin` / `deploy:<env>` / `approve:<env>`. 3. Project/list aTask mặc
định. 4. Giờ bản tổng hợp. 5. Quyền k8s của máy chạy bot theo env. 6. Cho phép làm spike S1 (gửi 1 khung WS với
senderId sai vào Saved Messages của chính bạn).

## 12. Điều kiện khi code (P2 từ vòng 3 — Codex + Fable)
- Kiểm hai người duyệt khác nhau theo **identity đã xác minh** (không chỉ khác senderId).
- Trả lời sau khởi động lại chỉ cho tin inbox `status=new` (supervise khởi động lại thường xuyên ⇒ tránh lặp tin).
- Extension: `RE_CARD`/`TYPE_BY_PREFIX` nhận loại `E`; golden test render ↔ parse bắt lỗi này.
- S8 nằm trong checklist pha 0.
- Test: SHA qua queue Jenkins, dev opt-in, rollback, DC demo, parser, crash trước/sau gửi, replay, duyệt đồng thời,
  outbox subprocess.
