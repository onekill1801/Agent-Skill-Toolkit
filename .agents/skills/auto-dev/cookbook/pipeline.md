# Auto-Dev Pipeline — chi tiết câu lệnh

Pipeline đầy đủ cho một task: **Intake → (Triage) → Plan → Implement → Test → Deliver**,
**autonomy Hybrid theo độ phức tạp**. Mọi tool ở `.claude/skills/dev-automation/tools/` (lõi
state) và `.claude/skills/auto-dev/tools/` (triage / grounding / review).

Quy ước trong tài liệu này:
- `<RID>` = run_id, gợi ý `<project>-<task_id>` (vd `atask-12345`).
- `<P>` = tên project trong `./work/projects.json`.
- Đặt env inline mỗi lệnh GitLab/Azure khi làm đa project (xem `dev-automation/cookbook/multi-project.md`).

> **Nguyên tắc gate (cốt lõi):** một stage chỉ được `done` qua `run_log.py advance` khi đã có
> **bằng chứng** (gate-result `pass`/`waived`) cho stage đó — KHÔNG dùng `stage <RID> <s> done`
> để tự khai trong mạch tự động (lệnh `stage` vẫn còn để gỡ tay/tương thích ngược).
> `advance` đọc `mode` của run: **auto** → gate hỏng = CHẶN; **checkpoint** → gate hỏng = báo cho
> người duyệt (không chặn). Đó là toàn bộ khác biệt giữa hai mức autonomy.

---

## 0. Triage + khởi tạo run-log

Triage phân loại task để chọn tier/mode (chi tiết nguồn intake: `cookbook/intake.md`):

```bash
cd .claude/skills/auto-dev/tools
python triage.py classify --type bugfix --title "<tiêu đề>" --desc "<mô tả>"
# -> {tier, mode, reason, skip_debate}. Heuristic mặc định; thêm --backend claude khi mơ hồ.
```

Khởi tạo run-log với tier/mode lấy từ triage:

```bash
cd ../../dev-automation/tools
python run_log.py init <RID> --task <task_id> --project <P> --type bugfix --title "<tiêu đề>" \
    --tier <trivial|standard|complex> --mode <auto|checkpoint>
```

- `trivial` → `--mode auto`, **được phép skip debate** và `advance` tự đi nếu gate xanh.
- `standard|complex` → `--mode checkpoint` (mặc định an toàn), giữ 3 mốc duyệt.
- Trích **acceptance criteria** từ task vào sổ AC (đối chiếu ở Deliver):
  `python run_log.py ac-add <RID> --text "<tiêu chí>"` (lặp cho từng tiêu chí).

Nếu chưa có `./work/projects.json`: hỏi người dùng đường dẫn thư mục code (`clone_dir`) và
lệnh test, rồi truyền trực tiếp qua `--cwd` / `--cmd` ở bước Test.

## 1. Intake — đọc task

Azure: `python azure_devops.py get <task_id>` · aTask: xem `cookbook/intake.md`.
Trích: tiêu đề, mô tả, repro/acceptance, severity, work item liên quan.

## 2. Plan — qua Agent Debate (tranh biện trước khi code)

```bash
python run_log.py stage <RID> plan active
```

> **Skip debate cho `tier=trivial`:** task tầm thường (1 file, rủi ro thấp) thì debate là overkill.
> Khi `skip_debate=true` từ triage: bỏ qua debate, viết spec gọn thẳng vào
> `temp/runs/<task_id>_plan.xml` (vẫn cần `<target_files>` để bước grounding bóc tách), rồi
> `run_log.py stage <RID> plan done`. Stage `plan` có required-gate `clarity` (xem Intake →
> Clarify): phải `record-gate <RID> clarity --verdict pass` trước thì `advance <RID> plan` mới qua
> ở auto mode. Với `standard|complex` → chạy đủ debate dưới đây.

Thay vì để 1 agent viết thẳng plan, chạy **Agent Debate Engine** ở đầu tầng Plan: ba vai (Dev /
Architect / Moderator) tranh biện để thẩm định kiến trúc, soi lỗ hổng bảo mật (SQLi, rate-limit,
connection pool, memory leak, thiếu cache) và tối ưu hiệu năng trước khi đụng code thật.

**Tranh biện theo VÒNG (không phải 1 lượt).** Dev đề xuất một lần, rồi lặp critique↔rebuttal tối đa
`--rounds` vòng (mặc định 2): mỗi vòng Architect soi *bản mới nhất* và kết bằng
`<verdict>APPROVE|REVISE</verdict>`. `APPROVE` → hội tụ sớm, sang phán quyết luôn (tiết kiệm token);
`REVISE` → Dev sửa, vòng sau Architect soi chính bản sửa đó (đóng lỗ "rebuttal không ai review").
Hết vòng mà chưa APPROVE → Moderator chốt với phần bất đồng còn lại. `--rounds 1` = hành vi 1-lượt cũ.
JSON kết quả có `rounds_used` + `converged` để orchestrator biết debate đã hội tụ hay chạm trần vòng.

**Recall bài học cũ (tuỳ chọn nhưng nên dùng):** trước khi debate, nạp các lần người dùng từng sửa
plan tương tự để agent đừng lặp lại:
```bash
cd ../../dev-automation/tools
python feedback.py recall --project <P> --stage plan --type <bugfix|feature> --query "<tiêu đề/mô tả>" \
  | python -c "import sys,json;open(r'../../../../temp/runs/<RID>_corrections.xml','w',encoding='utf-8').write(json.load(sys.stdin)['block'])"
# rồi thêm --corrections-file ../../../../temp/runs/<RID>_corrections.xml vào lệnh debate bên dưới.
```

```bash
cd ../../auto-dev/tools
# Mặc định gọi CLI agent bản subscription → KHÔNG cần ANTHROPIC_API_KEY:
python debate_engine.py run --task <task_id> --desc "<mô tả task>"            # backend=claude, 2 vòng (mặc định)
python debate_engine.py run --task <task_id> --desc "..." --rounds 3          # tranh biện sâu hơn cho task phức tạp
python debate_engine.py run --task <task_id> --desc "..." --backend cursor     # Cursor CLI (binary `agent`)
python debate_engine.py run --task <task_id> --desc "..." --backend agy         # Antigravity
# Người dùng xem cuộc tranh biện (có màu) trên STDERR; STDOUT là JSON {spec_path, rounds_used, converged}.
# Kết quả: temp/runs/<task_id>_plan.xml chứa <final_specification> đã được phản biện.
```
Backend (cờ đã đối chiếu `--help` thực tế; tất cả qua subscription, KHÔNG cần API key):
- `claude` (mặc định) — `claude -p --model <m> --dangerously-skip-permissions`. **✅ Đã chạy thật, nhiều vòng OK.**
- `cursor` — Cursor CLI, binary tên **`agent`**: `agent -p --output-format text --model <m> --force --trust`.
  Cờ khớp `agent --help` (print mode thiết kế cho script/non-interactive). Nên chạy được headless;
  xác nhận trên máy bạn (binary `agent` ở PATH cmd của bạn).
- `agy` — Antigravity: `agy --model <m> --print --dangerously-skip-permissions`. ⚠️ **[chưa chạy được
  headless ở đây]**: probe thực tế bị treo/timeout, `agy models` in stdout rỗng → agy có vẻ ghi ra
  TUI/kênh khác khi không có TTY. Nếu treo: dùng `claude`/`cursor`, hoặc `--cmd-template`.
- CLI khác / binary tên khác → `--backend custom --cmd-template "<lệnh> {model}"`.
- Chỉ `--backend api` mới cần `ANTHROPIC_API_KEY`. `--dry-run` = mock, không gọi gì.
- `<final_specification>` là dữ liệu Agent↔Agent (thẻ HTML/XML, KHÔNG Markdown — xem
  `auto-dev/prompts/SYSTEM_PROMPT.md`); plan theo tư duy **DATA-DRIVEN, HIGH-LEVEL**: bên trong có
  `<overview>`, `<architecture>`, `<data_flow>` (các `<step>` với `<input>`/`<transform>`/`<output>`),
  `<target_files>`, `<test_strategy>`. **Plan KHÔNG chứa code** — chỉ kiến trúc + luồng dữ liệu.
- **Gate Data-Driven**: JSON của `debate_engine` có `code_flagged`. Nếu `code_flagged=true` (plan lọt
  code sau khi engine đã bắt viết lại 1 lần) → **stage `plan` PHẢI fail**: `record-gate <RID> clarity
  --verdict fail` (hoặc không advance) và yêu cầu viết lại dạng luồng dữ liệu trước khi qua `after_plan`.
  Tuyệt đối không cho Implement viết code khi plan còn code / chưa được duyệt.

Khi bước Implement cần danh sách file cần sửa, **KHÔNG cắt chuỗi Markdown theo dòng** — bóc tách
cấu trúc từ file spec bằng parser chuẩn (`--file` đặt SAU tên lệnh con):

```bash
# Danh sách file cần sửa:
python ../../fork-terminal/tools/agent_parser.py list target_files file --file ../../../../temp/runs/<task_id>_plan.xml
# Luồng dữ liệu / kiến trúc / test (đọc để lập trình theo):
python ../../fork-terminal/tools/agent_parser.py tag data_flow --file ../../../../temp/runs/<task_id>_plan.xml
python ../../fork-terminal/tools/agent_parser.py tag architecture --file ../../../../temp/runs/<task_id>_plan.xml
```

> Worktree của Agent phụ (fork ra qua `fork-terminal`) đọc CHÍNH `temp/runs/<task_id>_plan.xml`
> này để lập trình — không cần truyền plan qua kênh khác.

**Sinh kịch bản verify (chạy thật → soi output → soi DB) NGAY sau plan:**
```bash
python verify_gen.py run --run <RID> --plan ../../../../temp/runs/<task_id>_plan.xml \
    --context-file ../../../../temp/runs/<RID>_scout.md --root "<clone_dir>" --backend claude
# --root: tự bóc ENDPOINT BỊ ẢNH HƯỞNG từ controller trong <target_files> (sửa XxxService ->
#   tự dò XxxController/XxxResource cùng tên) -> kịch bản PHẢI gọi đúng các API đó.
# -> temp/runs/<RID>_verify.json (flow_check format; mỗi AC hành-vi/dữ-liệu = 1 step "ACn: ...")
# JSON trả: touches_runtime · affected_endpoints · endpoints_untested (API bị ảnh hưởng mà
#   kịch bản KHÔNG gọi -> soi lại) · acs_uncovered · needs_review (step agent đoán)
```
`touches_runtime=true` (đụng controller/service/repository/sql/migration) → **nâng gate verify
thành BẮT BUỘC** — ở auto mode, Test không qua được nếu chưa chạy verify xanh:
```bash
cd ../../dev-automation/tools
python run_log.py require <RID> verify        # gate verify bắt buộc trên stage test
cd ../../auto-dev/tools
```

```bash
python run_log.py stage <RID> plan done
```

### ✋ Checkpoint `after_plan`
**KÊNH DUYỆT MẶC ĐỊNH: TELEGRAM, trả lời tự do** (áp dụng cho MỌI mốc ✋ trong pipeline —
after_plan / before_mr / before_notify / diff của fix_loop):
```bash
python tg_gate.py send --run <RID> --gate after_plan --title "<tiêu đề task>" \
  --item "PLAN (data-flow): <overview> + luồng Input->Transform->Output || đề xuất: duyệt" \
  --item "KỊCH BẢN VERIFY <n> step: <tóm tắt> || đề xuất: duyệt" \
  --item "<từng mục needs_review của verify_gen> || đề xuất: <cách xử lý>"
# Người dùng trả lời MỘT tin nhắn tự do: "<RID> 1: ok; 2: sửa X; 3: ok" (bỏ qua/'ok' = duyệt).
python tg_gate.py parse --run <RID> --gate after_plan --text "<nguyên văn trả lời>"
# -> approved_all + comment từng mục: mục có comment = THỰC HIỆN chỉnh + feedback.py add
#    --action edited; xong hết mới:
python run_log.py checkpoint <RID> after_plan approved
```
(Không có Telegram/ngồi tại terminal → trình plan dạng Markdown sạch + kịch bản verify
ngay trong phiên như cũ.) KHÔNG sửa code trước khi mốc này được duyệt.

## 3. Implement

> **Nhánh đích theo env (branch-per-env):** base branch + MR target = nhánh của env đang làm,
> lấy bằng `project_config.target_branch(<P>, <env>)` (vd dev→`dev`, uat→`uat`, prod→`prod`,
> sandbox→`pre-prod`; không có env → `default_target_branch`). Đặt `<TB>` = nhánh đó.
> Branch off đúng `<TB>` (checkout/pull `<TB>` trong `clone_dir` trước khi tạo nhánh mới).

```bash
python run_log.py stage <RID> implement active
TB=$(python -c "import project_config as p; print(p.target_branch('<P>','<env>'))")
GITLAB_PROJECT_ID=<id> python gitlab_api.py create-branch "bugfix/<task_id>-<short>" "$TB"
python run_log.py field <RID> branch "bugfix/<task_id>-<short>"
```

**Grounding (gate bắt buộc của Implement)** — đọc code thật trước khi sửa, tránh code mù lệch
convention. Gom `<target_files>` + file lân cận + stack hint thành artifact cho agent implement:

```bash
cd ../../auto-dev/tools
python grounding.py run --run <RID> --root "<clone_dir>" > ../../../../temp/runs/<RID>_grounding.json
#   --backend claude (tuỳ chọn) để agent tóm tắt convention vào artifact.
cd ../../dev-automation/tools
python run_log.py record-gate <RID> grounding --verdict pass --json ../../../../temp/runs/<RID>_grounding.json
```
> `grounding` trả `verdict:"fail"` khi KHÔNG định vị được file nào trong plan (sai path/sai
> checkout) → ở `mode=auto` sẽ chặn Implement (đúng: không nên code khi plan không khớp repo).

- Viết code trong `clone_dir`, đọc `temp/runs/<RID>_grounding.md`, theo `java-standards.md`.
- **Thêm/chỉnh test** chứng minh thay đổi (bug: test fail trước fix, pass sau fix).
- Commit + push trong thư mục project.

```bash
python run_log.py advance <RID> implement      # qua được iff gate 'grounding' pass/waived
```

## 4. Test (cổng bắt buộc)

```bash
python run_log.py stage <RID> test active
# Unit test (bắt buộc):
python test_runner.py run --project <P> --kind test  > t.json
python run_log.py record-gate <RID> test --json t.json    # verdict suy từ "passed" trong t.json
# Lint (BẮT BUỘC, waivable): project có linter -> record verdict theo kết quả; không có linter ->
python test_runner.py run --project <P> --kind lint  > l.json
python run_log.py record-gate <RID> lint --json l.json        # hoặc: --verdict waived --summary "no linter"
# Build (advisory — không tự chặn):
python test_runner.py run --project <P> --kind build > b.json
python run_log.py record-gate <RID> build --json b.json
```
`record-gate --json` tự suy verdict từ `{passed/timed_out/error}` của test_runner.

Xử lý fail (cổng `test`):
- `test` đỏ → JSON kèm `error_context` (thẻ `<error_context>...</error_context>`). Đưa NGUYÊN thẻ
  cho Fix-agent, sửa nguyên nhân, chạy lại + `record-gate` lại. Lặp tối đa **3 lần**. Hết 3 lần:
  ```bash
  python run_log.py note <RID> "test still red after 3 retries: <tóm tắt>"
  ```
  → DỪNG, báo người dùng, KHÔNG `advance`, KHÔNG tạo MR.

Chốt cổng Test:
```bash
python run_log.py advance <RID> test    # auto: chặn nếu test|lint chưa pass/waived; checkpoint: báo + đi tiếp
```

Khi chưa có registry, thay `--project <P>` bằng `--cwd <dir> --cmd "<lệnh test>"`
(hoặc `--cwd <dir> --auto` để tự dò pom.xml/package.json/...).

**Cổng `verify` — chạy code thật, soi output + DB (BẮT BUỘC khi `touches_runtime`,
đã `require` ở bước Plan).** Kịch bản đã sinh sẵn ở Plan (`<RID>_verify.json`) và đã
được người duyệt. DB connection tự resolve: registry → thiếu thì đọc thẳng
`application-<env>.yml` của app Spring (spring_config gap-fill, registry luôn thắng):

```bash
# 0) (nếu test local) chạy app lên — base_url/port lấy từ spring_config:
python spring_config.py read --project <P> --env dev          # xem db + base_url app
python local_app.py start --name <P> --project <P>            # lệnh chạy: --cmd > registry
#   `app_run_cmd` trong projects.json = lệnh ĐÃ BIẾT chạy được app này trên máy này
#   (vd atask: java + PropertiesLauncher vì mvn spring-boot:run chết error=206) > mvn default
python local_app.py wait-health --name <P>
# 1) đúng DB chưa (nhất là worktree đã isolate):
python probe_db.py check-db --engine <mysql|postgres> --project <P> --env dev --expect-db "<db>"
# 2) chạy kịch bản verify: gọi API thật -> assert response -> assert row trong DB:
python flow_check.py --file ../../../temp/runs/<RID>_verify.json --project <P> --env dev > v.json
python run_log.py record-gate <RID> verify --json v.json      # verdict suy từ passed
# 3) dừng app local nếu có chạy:
python local_app.py stop --name <P>
```
- **`verify`/`test` đỏ → `fix_loop.py` tự chẩn đoán và SỬA CODE** (thay cho việc bỏ dở):
  ```bash
  cd ../../auto-dev/tools
  python fix_loop.py run --run <RID> --project <P> --kind verify --env dev
  # bóc nguyên nhân theo loại (boot log 'Caused by' | flow_check step đỏ | <error_context>
  # của unit test) -> fix-agent headless sửa TỐI THIỂU trong clone_dir -> compile -> chạy lại.
  # THEO MODE của run:  auto = tự áp + lặp tới --max-attempts (mặc định 3)
  #                     checkpoint = DỪNG sau mỗi lần sửa, trả diff chờ NGƯỜI duyệt;
  #                                  duyệt xong gọi lại fix_loop (nó compile + retest);
  #                                  từ chối: git -C <clone_dir> checkout -- . rồi fix_loop reset
  # Xanh sau khi sửa -> bài học tự ghi vào feedback ledger (stage=fix, tag <kind>-fail)
  # + kết quả verify ở temp/runs/<RID>_verify_result.json cho record-gate/ac-map.
  # Hết attempts vẫn đỏ -> status=failed + history đầy đủ, bàn giao người, KHÔNG advance.
  ```
  Ở `mode=auto`, `advance <RID> test` vẫn **CHẶN** khi gate verify chưa pass (do đã `require`).
- Kết quả `v.json` dùng tiếp ở Deliver: `ac-map --verify-json` (bằng chứng thật cho AC).

**Cổng integration/e2e bổ sung** (Kafka/Redis/scenario tay) — chi tiết: `cookbook/stack-verify.md`.
Chạy ở env **non-prod** (dev/uat/sandbox) qua `--project <P> --env <env>`:

> **Trước probe DB trên worktree đã isolate:** `runtime_isolator` chỉ ĐỔI TÊN DB (`<db>_task_<id>`),
> KHÔNG tạo DB. Chạy guard xác nhận đang ở đúng DB đã isolate (và nó tồn tại) — nếu sai/không có,
> guard trả `error` (= chưa kiểm được, KHÔNG phải pass):
> ```bash
> python probe_db.py check-db --engine postgres --project <P> --env dev --expect-db "<db>_task_<task_id>"
> ```
> Tên `<db>_task_<task_id>` lấy từ `runtime_isolator.isolated_db_name(<db>, <task_id>)`.

```bash
python flow_check.py --file ../../auto-dev/scenarios/<scenario>.json --project <P> --env dev
# hoặc probe lẻ cho phần thay đổi:
python probe_api.py call --url /api/... --project <P> --env dev --expect-status 200 --expect-json '$.x=y'
python probe_db.py query --engine postgres --sql "select ..." --project <P> --env dev --expect-rows 1
python probe_kafka.py consume --topic <t> --project <P> --env dev --expect-contains '...'
python probe_redis.py get <key> --project <P> --env dev --expect-exists
# Ghi cổng integration (advisory) nếu có chạy:
python run_log.py record-gate <RID> integration --verdict pass --summary "<scenario> ok"
```
> Guard: thao tác **ghi** vào `prod` bị từ chối trừ khi `--allow-prod`. Pipeline mặc định test
> non-prod; KHÔNG tự ý chạy `--allow-prod` — nếu cần đụng prod phải hỏi xác nhận người dùng.

**Cổng CI Jenkins** (tuỳ chọn): `python jenkins.py build --project <P> --env dev --wait` → chỉ qua khi result=SUCCESS.

> Probe trả `{"error":true}` = **không kiểm được** (service down/thiếu config), KHÔNG phải đạt —
> coi như chưa verify, không được giao MR dựa trên đó.

## 5. Deliver

**Pre-MR review (gate `review`, chạy TRƯỚC khi tạo MR, KHÔNG post lên MR):** review diff thật
bằng agent headless, trả verdict JSON:
```bash
cd ../../auto-dev/tools
python review_gate.py run --root "<clone_dir>" --base "$TB" --branch "bugfix/<task_id>-<short>" > r.json
cd ../../dev-automation/tools
python run_log.py record-gate <RID> review --json r.json    # passed=false nếu có blocker
```
- `blockers` → sửa hết rồi review lại (đừng tạo MR khi còn blocker). `warnings` → cân nhắc.

**Đối chiếu acceptance criteria (gate `ac`, suy từ sổ AC):** AC dạng hành-vi/dữ-liệu
map bằng **kết quả verify thật** (step "ACn: ..." phải PASS — step fail/thiếu là bị từ chối):
```bash
python run_log.py ac-map <RID> AC1 --verify-json v.json    # bằng chứng = step 'AC1: ...' đã pass
# AC ngoài phạm vi runtime (docs/refactor) mới dùng --evidence tay:
python run_log.py ac-map <RID> AC1 --evidence "test ReportServiceTest / probe_api 200"
# ... map từng AC tới bằng chứng; AC ngoài phạm vi: ac-waive <RID> ACx --note "..."
```
> Ở `mode=auto`: nếu `r.json.passed=false` (còn blocker) hoặc còn AC `open` → **DỪNG, KHÔNG tạo
> MR** (cổng `deliver` sẽ chặn ở `advance` cuối). Ở `mode=checkpoint`: trình các phát hiện này cho
> người duyệt ở mốc `before_mr` dưới đây.

### ✋ Checkpoint `before_mr`
Trình bày: kết quả test xanh + verdict review + AC đã map + tóm tắt diff. Chờ duyệt:
```bash
python run_log.py checkpoint <RID> before_mr approved
GITLAB_PROJECT_ID=<id> python gitlab_api.py create-mr "bugfix/<task_id>-<short>" "Fix: <tiêu đề>" "$TB"
python run_log.py field <RID> mr_url "<url trả về>"
```

### ✋ Checkpoint `before_notify`
Người thật sẽ thấy notification → confirm trước:
```bash
python run_log.py checkpoint <RID> before_notify approved
python notifier.py mr-created <task_id> "<mr_url>"
python azure_devops.py state <task_id> Resolved
# Task nguồn aTask: chuyển trạng thái + đính link MR lên task [WRITE]:
python ../../auto-dev/tools/task_queue.py mark <qid> --to approved --comment "MR: <mr_url>"
#   (--to theo workflow của bạn: processing khi BẮT ĐẦU làm — gọi ngay sau `next`;
#    approved/"chờ phê duyệt" khi MR xong; completed khi task được nghiệm thu)
python run_log.py advance <RID> deliver    # done iff gate review+ac đạt (auto chặn / checkpoint báo)
```

## 6. Học từ can thiệp (feedback ledger)

Mỗi lần con người **sửa / bác / override** đề xuất của agent = một bài học. Ghi lại thì lần sau
agent bớt sai (recall ở mục 2 sẽ bơm lại vào prompt). Ghi ngay tại điểm can thiệp:

```bash
cd ../../dev-automation/tools
# Người dùng sửa plan trước khi duyệt (mốc after_plan):
python feedback.py add --project <P> --stage plan --run-id <RID> --task-type <t> --action edited \
  --correction "<đã đổi gì so với plan agent>" --reason "<vì sao — trường quan trọng nhất>" \
  --tags convention,wrong-file
# Bác một approval: --action rejected ; đổi tier/mode so với triage: --stage triage --action overridden
# Duyệt SẠCH (không sửa gì) cũng PHẢI ghi — 1 dòng, để stats có mẫu số đo tỉ lệ duyệt-thẳng:
python feedback.py add --project <P> --stage plan --run-id <RID> --task-type <t> --action approved
```
- Ledger: `work/feedback/<P>.jsonl` (append-only, **gitignored** → máy-cụ-thể, tự backup/sync nếu
  chạy nhiều máy).
- Xem "càng chạy càng chính xác": `python feedback.py stats --project <P>`
  (tỉ lệ duyệt-thẳng-không-sửa theo stage + phân bố tag lỗi).
- Recall chỉ dùng `edited|rejected|overridden` (bài học); nhưng `approved` vẫn **bắt buộc ghi ở mỗi
  mốc duyệt** — thiếu nó, `stats` không có mẫu số và tỉ lệ duyệt-thẳng luôn ≈ 0 (vô nghĩa).

## Resume

```bash
python run_log.py get <RID>          # xem dừng ở đâu
python run_log.py list --open        # các run chưa xong
```
Vào lại ở stage đầu tiên chưa `done`. Nếu một checkpoint còn `pending`, trình bày lại artifact của stage đó và xin duyệt trước khi đi tiếp.

## Bảng xử lý lỗi

| Tình huống | Hành động |
|---|---|
| Không tìm được project trong registry | Hỏi `clone_dir` + lệnh test, dùng `--cwd/--cmd` |
| Test fail do test cũ lỗi thời | Cập nhật test, ghi `note`, không tính là fix sai |
| Test timeout (>30 phút) | `test_runner` trả `timed_out`; chia nhỏ hoặc tăng `--timeout` |
| Fix cần đổi schema | Kèm migration script, ghi rõ trong mô tả MR |
| Hết 3 lần retry vẫn đỏ | `stage test failed`, dừng, bàn giao người |
