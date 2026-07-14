---
name: Auto Dev Orchestrator
description: >
  End-to-end coding pipeline: take a request (Azure DevOps / aTask task, or a direct
  description) and drive it through Plan -> Implement -> Test -> Deliver with human
  checkpoints. Gates the merge request on a green test run. Trigger phrases:
  "auto dev", "tự động làm task", "chạy pipeline", "auto-dev task", "làm task tự động",
  "run the pipeline", "automate this task end to end".
---

# Auto Dev Orchestrator

Master skill that chains the existing `dev-automation` and `atask-automation` tools into
one pipeline: **Intake → Plan → Implement → Test → Deliver**. It does not replace those
skills — it sequences them and adds the two missing pieces: a **test gate** and a
**resumable run-log**.

## Autonomy: Hybrid by complexity

Triage at Intake classifies each run into a **tier** + **mode** (see `triage.py`):

| tier | mode | checkpoints | gate behaviour |
|------|------|-------------|----------------|
| `trivial` | `auto` | skipped | a failed gate **BLOCKS** the transition (no human in the loop) |
| `standard` / `complex` | `checkpoint` | the three below | a failed gate **INFORMS** the human approver (does not auto-block) |

The three checkpoints (checkpoint mode):
1. `after_plan` — after the plan, before any code change.
2. `before_mr` — after tests pass + review, before the merge request.
3. `before_notify` — before any `notifier.py` call (a real person will see it).

At each checkpoint: present the artifact, ask for approval, record it with
`run_log.py checkpoint <run_id> <name> approved`, then continue. Never pass a checkpoint
without explicit approval.

### Evidence-gated transitions (core principle)

A stage is advanced with `run_log.py advance <run_id> <stage>` — which only marks it `done`
when the **evidence** it requires has been recorded (`run_log.py record-gate ...`). The gate
guards the state machine; it is not advisory narration. **Do not use `stage <id> <s> done`** to
self-declare progress in an automated run (that legacy command trusts the caller and exists only
for manual unstick / backward compatibility). `advance` reads the run's `mode` to decide
block-vs-inform — that single fork is the entire difference between the autonomy levels.

Required gates: `plan→clarity`, `implement→grounding`, `test→test+lint` (lint mandatory, waivable),
`deliver→review+ac`. `build`/`integration` are advisory (reported, never block alone).

## Tools

Pipeline tools live in `tools/` (this skill) and `../dev-automation/tools/` (shared). Read tool
source before first use.

| Tool | Role in pipeline |
|------|------------------|
| `tools/autopilot.py` | **MỘT LỆNH chạy trọn chu trình** — `run --resolve-existing`: resolver review loạt → prep từng task (agent, hỏi Telegram khi cần làm rõ/duyệt; timeout = để nguyên, nhắc người) → worker code tuần tự + merge local. Telegram chỉ chạm người ở: làm rõ · duyệt solution · đóng task FIXED |
| `tools/task_queue.py` | **Intake queue + SERIAL resolver flow** — `scan/intake` enrich+clarify many tasks up front; `next/done` hand out ONE item at a time under a per-flow lock (owner `task_resolver`, shared with `task_resolver.py` — manual work is never blocked). `answer` syncs the clarified brief back onto the aTask task ([WRITE]) so it can be handed off. See `cookbook/intake.md` § Queue, slash `/atask-queue` |
| `tools/context_pack.py` | **Intake: enrich** — gather description + **comments + checklist + subtasks** (aTask) or **AC/root-cause/solution** (Azure) into `temp/runs/<src>-<id>_context.md`; emits `ac_seeds` + `signals.thin_description`. Feeds clarify/scout/debate as `--desc`. Run FIRST |
| `tools/clarify.py` | **Intake gate `clarity`** — surface ambiguities (scope/io/acceptance/edge/non-functional) as blocking-vs-assumed questions; `brief` folds answers into `temp/runs/<id>_brief.md`. Heuristic-first, optional `--backend`. Required gate on stage `plan`. Feed it the context pack via `--desc-file` |
| `tools/triage.py` | **Intake: triage** — classify tier (trivial/standard/complex) + mode (auto/checkpoint); heuristic-first, optional `--backend` agent |
| `tools/debate_engine.py` | **Plan: Agent Debate** — Dev/Architect/Moderator via subscription CLI agent (claude/cursor/agy, headless; no API key) → `temp/runs/<task_id>_plan.xml`. Loops critique↔rebuttal up to `--rounds` (default 2; Architect `<verdict>APPROVE</verdict>` converges early). Skipped for `tier=trivial` |
| `tools/agent_runner.py` | **Shared headless-agent primitive** — `run_turn()`; reused by triage / grounding / review_gate |
| `tools/grounding.py` | `scout` = **Intake pre-grounding** (grep repo by task keywords → candidate files BEFORE the plan, so it anchors to real code); `run` = **Implement gate `grounding`** (gather the plan's target files + neighbours + stack before coding) |
| `tools/review_gate.py` | **Deliver gate `review`** — review the real `git diff` pre-MR, JSON verdict, posts NOTHING |
| `tools/tg_gate.py` | **Mốc duyệt qua Telegram, trả lời TỰ DO** — `send` mọi checkpoint (after_plan/before_mr/before_notify/fix_diff) dạng mục đánh số + đề xuất; `parse` comment "1: ok; 2: sửa X" → approved/comment từng mục. Mục có comment = thực hiện chỉnh + ghi feedback ledger |
| `tools/fix_loop.py` | **Test/Verify: vòng tự sửa** — target đỏ → bóc nguyên nhân (boot log / flow step / error_context) → fix-agent sửa code trong clone_dir → compile → retest. Theo mode: auto tự lặp ≤3; checkpoint dừng trả diff chờ người duyệt. Sửa thành công tự ghi feedback ledger (stage=fix) |
| `tools/verify_gen.py` | **Plan: sinh kịch bản verify** — plan + AC ledger → `temp/runs/<RID>_verify.json` (flow_check format, mỗi AC hành-vi/dữ-liệu = 1 step "ACn: ..."); trả `touches_runtime` → `run_log.py require <RID> verify`. Người duyệt kịch bản cùng plan ở `after_plan` |
| `../dev-automation/tools/spring_config.py` | **Đọc config app Spring** — parse `application-<env>.yml` (chuẩn + JHipster `resources/config/`) → DB/port/base_url; `project_config` tự gap-fill khi registry thiếu (registry luôn thắng) |
| `azure_devops.py` / `search.py` (atask) | Intake: read the task + acceptance criteria |
| `test_runner.py` | **Test gates** — `run --project <p> --kind test\|lint\|build` → JSON `passed` |
| `probe_*.py` (`probe_db.py check-db` guards the isolated DB) `flow_check.py` `jenkins.py` | **Integration / e2e / CI** — see `cookbook/stack-verify.md` |
| `local_app.py` | **Local app-under-test lifecycle** — start (mvn/jar) → wait-health → stop, for localhost e2e (run app → call API → watch DB) |
| `run_log.py` | **Evidence-gated state machine** — `record-gate` / `advance` / AC ledger; resume + audit in `temp/runs/<run_id>.json` |
| `../dev-automation/tools/feedback.py` | **Learning loop** — `recall` past human corrections into the next run's prompts; `add` a record whenever a human edits a plan / rejects an approval / overrides triage. Ledger at `work/feedback/<project>.jsonl` (gitignored). Closes the "no memory" gap so accuracy improves over runs |
| `gitlab_api.py` | branch + MR |
| `notifier.py` | progress notifications (checkpoint `before_notify` guards these) |
| `../fork-terminal/tools/agent_parser.py` | **Parse Agent↔Agent data** — `<target_files>` / `<error_context>` / `<review>` via stdlib regex |

> New for the evidence-gated pipeline: `triage.py`, `agent_runner.py`, `grounding.py`,
> `review_gate.py`, the `run_log.py` gate/AC commands, and `probe_db.py check-db`. See
> `cookbook/pipeline.md` for the command sequence, `cookbook/intake.md` for triage + intake,
> `cookbook/stack-verify.md` for integration/e2e. Unit tests for the framework live in `tests/`
> (`python -m unittest discover -s tests`).

## The pipeline

```
Intake ──> Plan ──> [✋ after_plan] ──> Implement ──> Test ──> (green?) ──> [✋ before_mr]
                                            ▲             │ no
                                            └─ fix ───────┘  (retry, cap = MAX_TEST_RETRIES)
   ──> Deliver: create MR ──> self-review ──> [✋ before_notify] ──> notify ──> update task state
```

`MAX_TEST_RETRIES = 3`. If tests are still red after the cap, STOP, set the test stage
to `failed`, post the failure summary, and hand back to the human — do not deliver.

### Step sequence (checkpoint mode)

1. **Intake + enrich + scout + recall.** Resolve the task, then **`context_pack.py build --source
   <atask|azure> --task <id>`** to pull comments/checklist/subtasks (aTask) or AC/root-cause (Azure)
   into `temp/runs/<src>-<id>_context.md` — use THIS as `--desc-file` for everything downstream (never
   the bare description). `ac-add` its `ac_seeds`. Then `grounding.py scout --run <RID> --root
   <clone_dir> --keywords "<pack.keywords>"` → candidate files (`temp/runs/<RID>_scout.md`) so the plan
   anchors to real code. Also **`feedback.py recall --project <P> --stage plan --type <t> --query
   "<title/desc>"`** → save its `block` to `temp/runs/<RID>_corrections.xml` (past human corrections to
   feed the debate). Resolve project from `./work/projects.json`; if absent, ask.
1b. **Clarify (gate `clarity`) → then Triage.** Run clarify FIRST so triage knows if the task is vague:
   `clarify.py analyze --desc-file <pack> --context-file temp/runs/<RID>_scout.md --backend claude` →
   questions, each with a **`proposed`** grounded answer. Present blocking ones as "Q → proposed answer"
   for one-click confirm/edit (checkpoint mode); auto mode does NOT ask — `needs_clarification` fails
   the gate. `clarify.py brief --answers-file ... --out temp/runs/<id>_brief.md` folds answers; use the
   brief (+ scout candidates) as the debate `--desc` and `ac-add` the `acceptance_seeds`.
   `record-gate <RID> clarity --verdict pass|fail`. **Then** `triage.py classify --desc-file <pack>
   --clarity <clarify_verdict> --backend claude` → tier/mode: `mode=auto` requires tier=trivial AND
   clarity=pass (a vague task is force-downgraded to `checkpoint`). Pick a `run_id`,
   `run_log.py init <run_id> ... --tier <t> --mode <m>`. For real tasks prefer `--backend` on both
   clarify and triage (heuristic fallback is automatic and safe).
2. **Plan.** `stage plan active`. For `standard|complex`: run `debate_engine.py run ... --desc-file
   <brief> --corrections-file temp/runs/<RID>_corrections.xml` (subscription CLI agent, no API key;
   loops critique↔rebuttal up to `--rounds`, default 2 — bump to `--rounds 3` for `complex`) →
   `<final_specification>` at `temp/runs/<task_id>_plan.xml`. For `trivial`: skip the debate, write a
   lean spec (keep `<target_files>`). `stage plan done`.
   **✋ Checkpoint `after_plan`** (checkpoint mode) — present the spec as clean Markdown, get approval.
   **If the human edits the plan before approving, record it:** `feedback.py add --project <P> --stage
   plan --run-id <RID> --task-type <t> --action edited --correction "<what changed>" --reason "<why>"
   --tags <...>` — this is what makes the next run smarter. Same on a rejected approval (`--action
   rejected`) or a triage override (`--stage triage --action overridden`). A CLEAN approval is
   recorded too (`--action approved`, one line, no correction) — it is the denominator of the
   straight-through rate in `feedback.py stats`; without it the metric is meaningless.
3. **Implement.** `stage implement active`. Branch off the env's target branch. **Run `grounding.py`
   and `record-gate <RID> grounding`** before editing. Write code in `clone_dir` (read the grounding
   artifact, follow `java-standards.md`), add/adjust tests. `advance <RID> implement`.
4. **Test.** `stage test active`. Run + `record-gate` each: `test` (required), `lint` (required,
   `--verdict waived` if no linter), `build` (advisory). **`verify` (required iff `touches_runtime`
   — promoted via `run_log.py require <RID> verify` at Plan):** actually RUN the change —
   `local_app.py start/wait-health` (DB + base_url resolved from the Spring app's
   `application-<env>.yml` via `spring_config.py` when the registry lacks them) → `probe_db.py
   check-db` → `flow_check.py --file temp/runs/<RID>_verify.json` → `record-gate <RID> verify
   --json v.json`. A probe `{"error":true}` = "could not run", never a pass. Fix-and-retry the
   `test`/`verify` gates up to `MAX_TEST_RETRIES`; still red → STOP, report, no MR. Then
   `advance <RID> test`.
5. **Deliver.** `review_gate.py run ...` on the real diff → `record-gate <RID> review` (no posting;
   fix blockers before MR). Map behaviour/data ACs with `ac-map <RID> ACn --verify-json v.json`
   (the "ACn: ..." step must have PASSED; hand-written `--evidence` is for non-runtime ACs only).
   **✋ Checkpoint `before_mr`** — show test+verify+review+AC, get approval, then
   `gitlab_api.py create-mr ...`, store `mr_url`.
6. **✋ Checkpoint `before_notify`** — confirm, then `notifier.py mr-created <task_id> <url>`,
   update task state, and `advance <RID> deliver` (done iff `review`+`ac` gates pass).

## Resuming a run

`run_log.py get <run_id>` shows where it stopped. `run_log.py list --open` lists unfinished
runs. Re-enter at the first stage whose status is not `done`.

## Routing

| User says... | Action |
|---|---|
| "auto-dev task #123" / "làm task 123 tự động" | Run the full pipeline for task 123 |
| "resume run atask-123" | `run_log.py get atask-123` → continue from first non-done stage |
| "show pipeline runs" | `run_log.py list --open` |

Slash command: `/auto-dev <task_id|description>`.

## Giao thức giao tiếp Agent (BẮT BUỘC)

Mọi dữ liệu trung gian Agent↔Agent và Agent↔Tool (Plan, Target Files, Error Log, lời thoại
tranh luận) dùng **thẻ HTML/XML nghiêm ngặt**, KHÔNG Markdown — chi tiết + bộ thẻ chuẩn ở
`prompts/SYSTEM_PROMPT.md`. Bóc tách bằng `../fork-terminal/tools/agent_parser.py` (không cắt
chuỗi theo dòng). **Đầu ra cuối cho người dùng vẫn là Markdown sạch** (bỏ thẻ).

## Guardrails (inherits CLAUDE.md)

1. Honor the three checkpoints. Never skip `before_notify` — a real person sees notifications.
2. Never deliver on red tests. The test gate is mandatory.
2b. Run integration/e2e tests against **non-prod** (dev/uat/sandbox) via `--project <P> --env <env>`.
    Writes to a protected env (prod) are refused unless `--allow-prod` — never pass that flag
    autonomously; if a task genuinely needs prod, ask the user to confirm first.
3. Always `run_log` each stage transition so an interrupted run can resume.
4. Confirm before any destructive op (delete branch, close task) and before final task states.
5. Never hardcode tokens/URLs — config flows through `config.py` / inline env prefix.
