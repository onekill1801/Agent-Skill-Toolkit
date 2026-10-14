# Kiểm kê repo (tự sinh — `python .claude/skills/_runtime/inventory.py`)

## Runtime `agent_skill_toolkit`

| Module | Vùng |
|---|---|
| `agent_skill_toolkit` | root |
| `agent_skill_toolkit.core` | core |
| `agent_skill_toolkit.core.console` | core |
| `agent_skill_toolkit.core.env` | core |
| `agent_skill_toolkit.core.human` | core |
| `agent_skill_toolkit.core.locks` | core |
| `agent_skill_toolkit.core.paths` | core |
| `agent_skill_toolkit.core.proc` | core |
| `agent_skill_toolkit.core.store` | core |
| `agent_skill_toolkit.core.text` | core |
| `agent_skill_toolkit.core.winexec` | core |
| `agent_skill_toolkit.core.yamlsub` | core |
| `agent_skill_toolkit.workflows` | workflows |
| `agent_skill_toolkit.workflows.coordinator` | workflows |
| `agent_skill_toolkit.workflows.engine` | workflows |
| `agent_skill_toolkit.workflows.execution` | workflows |
| `agent_skill_toolkit.workflows.execution.agent_call` | workflows |
| `agent_skill_toolkit.workflows.execution.handoff` | workflows |
| `agent_skill_toolkit.workflows.execution.quota` | workflows |
| `agent_skill_toolkit.workflows.execution.usage` | workflows |
| `agent_skill_toolkit.workflows.intake` | workflows.intake |
| `agent_skill_toolkit.workflows.intake.atask_flow` | workflows.intake |
| `agent_skill_toolkit.workflows.intake.gate` | workflows.intake |
| `agent_skill_toolkit.workflows.intake.lanes` | workflows.intake |
| `agent_skill_toolkit.workflows.proposal` | workflows.proposal |
| `agent_skill_toolkit.workflows.proposal.flow` | workflows.proposal |
| `agent_skill_toolkit.workflows.proposal.scan` | workflows.proposal |
| `agent_skill_toolkit.workflows.refactor` | workflows.refactor |
| `agent_skill_toolkit.workflows.refactor.program` | workflows.refactor |
| `agent_skill_toolkit.workflows.refactor.ratchet` | workflows.refactor |
| `agent_skill_toolkit.workflows.refactor.survey` | workflows.refactor |
| `agent_skill_toolkit.workflows.verify` | workflows.verify |
| `agent_skill_toolkit.workflows.verify.ladder` | workflows.verify |
| `agent_skill_toolkit.workflows.verify.unittest_junit` | workflows.verify |

## Phụ thuộc giữa các vùng (số cạnh import)

| Từ | Tới | Cạnh |
|---|---|---|
| workflows | core | 5 |
| workflows.intake | core | 4 |
| workflows.intake | workflows | 1 |
| workflows.proposal | core | 3 |
| workflows.proposal | workflows | 2 |
| workflows.refactor | core | 4 |
| workflows.refactor | workflows | 2 |
| workflows.refactor | workflows.intake | 1 |
| workflows.refactor | workflows.proposal | 1 |
| workflows.refactor | workflows.verify | 1 |
| workflows.verify | core | 4 |
| workflows.verify | workflows | 1 |
| workflows.verify | workflows.refactor | 1 |

## Ngoại lệ layering (chỉ được giảm — `tests/test_layering.py`)

| Vùng | Phụ thuộc | Hợp đồng |
|---|---|---|
| agent_skill_toolkit.workflows.refactor.program | `agent_skill_toolkit.workflows.intake.gate` | gate.admit/submit: every new program enters through the intake gate |
| agent_skill_toolkit.workflows.refactor.program | `agent_skill_toolkit.workflows.verify.ladder` | ladder.load_spec/resolve_base/KIND: slices are proven by the verify ladder |
| agent_skill_toolkit.workflows.refactor.survey | `agent_skill_toolkit.workflows.proposal.scan` | scan.hotspots: the survey reuses the hotspot scanner |
| agent_skill_toolkit.workflows.verify.ladder | `agent_skill_toolkit.workflows.refactor.ratchet` | ratchet.parse_*/scan_isolated/compare: the `kind: ratchet` rung |

## Cạnh runtime → script skill (subprocess)

- `('agent_skill_toolkit.workflows.execution.agent_call', '.claude/skills/auto-dev/tools/agent_runner.py')` → `agents run through agent_runner`
- `('agent_skill_toolkit.workflows.intake.atask_flow', 'coordinator.py')` → `tells the human how to accept`
- `('agent_skill_toolkit.workflows.proposal.flow', 'proposals.py')` → `tells the human how to go / decide`
- `('agent_skill_toolkit.workflows.proposal.scan', 'proposals.py')` → `digest tells the human how to go / reject`
- `('agent_skill_toolkit.workflows.refactor.program', 'refactor.py')` → `tells the human how to approve`
- `('agent_skill_toolkit.workflows.verify.ladder', '.claude/skills/auto-dev/tools/agent_runner.py')` → `agents run through agent_runner`
- `('agent_skill_toolkit.workflows.verify.unittest_junit', 'unittest_junit.py')` → `its own argparse prog name`

## Tool của skill (107 file; 12 là vỏ dùng runtime)

| Skill | Tool | Vỏ runtime | Lệnh con | Dòng |
|---|---|---|---|---|
| atask-automation | `analytics.py` |  |  | 379 |
| atask-automation | `atask_watch.py` | ✅ |  | 425 |
| atask-automation | `auth.py` |  |  | 67 |
| atask-automation | `checklists.py` |  |  | 188 |
| atask-automation | `client.py` |  |  | 131 |
| atask-automation | `config.py` |  |  | 111 |
| atask-automation | `governed_search.py` |  |  | 119 |
| atask-automation | `projects.py` |  |  | 247 |
| atask-automation | `search.py` |  |  | 201 |
| atask-automation | `task_resolver.py` |  |  | 746 |
| atask-automation | `tasks.py` |  |  | 328 |
| atask-automation | `view.py` |  |  | 167 |
| auto-dev | `agent_init.py` |  | add, apply, check, docs, draft, scan | 1586 |
| auto-dev | `agent_runner.py` | ✅ |  | 571 |
| auto-dev | `autopilot.py` |  | run, watch | 197 |
| auto-dev | `clarify.py` |  | analyze, brief, bundle | 392 |
| auto-dev | `context_pack.py` |  | build | 346 |
| auto-dev | `coordinator.py` | ✅ | accept, board, budget, capacity, usage, work | 160 |
| auto-dev | `debate_engine.py` |  | run | 725 |
| auto-dev | `fix_loop.py` |  | reset, run | 407 |
| auto-dev | `grounding.py` |  | run, scout | 436 |
| auto-dev | `proposals.py` | ✅ | decide, go, list, new, refresh, scan | 221 |
| auto-dev | `queue_worker.py` |  | run | 161 |
| auto-dev | `refactor.py` | ✅ | program, ratchet, survey | 170 |
| auto-dev | `review_gate.py` |  | run | 121 |
| auto-dev | `task_queue.py` |  | add, answer, approve, ask-tg, done, intake, list, mark, next, release, remove, reply, requeue, scan, show | 884 |
| auto-dev | `tg_gate.py` |  | parse, reply, send, wait | 228 |
| auto-dev | `triage.py` |  | classify, upgrade | 226 |
| auto-dev | `verify.py` | ✅ | plan, report, start, status, work | 195 |
| auto-dev | `verify_gen.py` |  | run | 322 |
| dev-automation | `azure_devops.py` |  |  | 213 |
| dev-automation | `bg_notify.py` |  |  | 311 |
| dev-automation | `cg.py` |  | analyze, prepare, status | 331 |
| dev-automation | `claude_account_rotation.py` |  |  | 408 |
| dev-automation | `code_context.py` |  | affected, callers, impact, locate, status, trace | 423 |
| dev-automation | `config.py` |  |  | 122 |
| dev-automation | `daemon_common.py` |  | status | 236 |
| dev-automation | `dbeaver.py` |  | forwards, list, probe, show | 226 |
| dev-automation | `doctor.py` |  |  | 142 |
| dev-automation | `feedback.py` |  | add, recall, stats | 280 |
| dev-automation | `flow_check.py` |  |  | 247 |
| dev-automation | `gitlab_api.py` |  |  | 366 |
| dev-automation | `jenkins.py` |  | build, console, info, jobs, status, trigger | 306 |
| dev-automation | `k8s.py` |  | access, contexts, deploys, describe, doctor, events, forwards, get, logs, pods | 673 |
| dev-automation | `kafka_ui.py` |  | clusters, login-check, messages, produce, topic, topics | 347 |
| dev-automation | `local_app.py` |  | logs, prep-java, start, status, stop, wait-health | 331 |
| dev-automation | `mr_watch.py` |  |  | 358 |
| dev-automation | `mysql_client.py` |  |  | 267 |
| dev-automation | `notifier.py` |  |  | 128 |
| dev-automation | `orca_adapter.py` |  |  | 265 |
| dev-automation | `podfwd.py` |  | fetch, list, logs, start, start-saved, stop | 344 |
| dev-automation | `postgres_client.py` |  |  | 239 |
| dev-automation | `postman_gen.py` |  |  | 276 |
| dev-automation | `probe_api.py` |  | call | 164 |
| dev-automation | `probe_common.py` |  |  | 89 |
| dev-automation | `probe_db.py` |  | check-db, query | 286 |
| dev-automation | `probe_kafka.py` |  | consume, produce | 222 |
| dev-automation | `probe_redis.py` |  |  | 183 |
| dev-automation | `project_config.py` |  |  | 234 |
| dev-automation | `run_log.py` |  | ac-add, ac-map, ac-waive, advance, checkpoint, field, get, init, list, note, record-gate, require, stage | 613 |
| dev-automation | `spring_config.py` |  | read | 244 |
| dev-automation | `status.py` | ✅ |  | 364 |
| dev-automation | `taskstore.py` | ✅ | add, cancel, events, list, show, summary, unblock | 77 |
| dev-automation | `test_runner.py` |  | detect, run | 344 |
| dev-automation | `vpn.py` |  | down, ensure, fingerprint, set-password, status, up, watch | 331 |
| fork-terminal | `agent_parser.py` |  | all, list, tag, wrap | 155 |
| fork-terminal | `fork_terminal.py` |  |  | 83 |
| fork-terminal | `runtime_isolator.py` |  |  | 248 |
| fork-terminal | `spawn_isolated_agent.py` |  | cleanup, spawn | 112 |
| fork-terminal | `worktree_manager.py` |  | create, remove | 174 |
| mr-review | `review_common.py` | ✅ |  | 333 |
| mr-review | `review_flow.py` | ✅ | enqueue, enqueue-text, reconcile, release-quarantine, status, work | 449 |
| mr-review | `review_post.py` |  | approval-status, approve, post, reject, render, request-approval, sweep, validate | 517 |
| mr-review | `review_queue.py` | ✅ | add, add-text, cancel, list, next, pending, set, show | 229 |
| mr-review | `review_rules.py` |  |  | 153 |
| mr-review | `review_ws.py` |  | check-stale, cleanup, diff, gc, prepare | 394 |
| new-product | `intake.py` |  | build | 189 |
| new-product | `registry.py` |  | add, get, list, remove | 169 |
| new-product | `repo_init.py` |  | create-gitlab, init, push | 224 |
| new-product | `scaffold.py` |  | check, recipes, run | 394 |
| remote-control | `approvals.py` |  |  | 68 |
| remote-control | `choices.py` |  |  | 96 |
| remote-control | `md2tg.py` |  |  | 130 |
| remote-control | `rc_config.py` |  |  | 92 |
| remote-control | `ssh_exec.py` |  |  | 176 |
| remote-control | `telegram_approve.py` |  |  | 159 |
| remote-control | `telegram_bridge.py` |  |  | 615 |
| remote-control | `tg_api.py` |  |  | 278 |
| tchat-automation | `auth.py` |  |  | 105 |
| tchat-automation | `client.py` |  |  | 115 |
| tchat-automation | `common.py` |  |  | 63 |
| tchat-automation | `config.py` |  |  | 145 |
| tchat-automation | `crypto.py` |  | conv, decrypt, unwrap-indexeddb, unwrap-qr | 254 |
| tchat-automation | `group_watch.py` | ✅ |  | 600 |
| tchat-automation | `groups.py` |  |  | 111 |
| tchat-automation | `listen.py` |  |  | 251 |
| tchat-automation | `messages.py` |  |  | 122 |
| tchat-automation | `notify_group.py` |  |  | 49 |
| tchat-automation | `reply_worker.py` |  |  | 191 |
| tchat-automation | `send.py` |  |  | 241 |
| tchat-automation | `style_profile.py` |  | gather, get, list, path, save, template | 157 |
| tchat-automation | `task_digest.py` |  |  | 719 |
| tchat-automation | `todos.py` |  |  | 142 |
| tchat-automation | `tokens.py` |  |  | 177 |
| tchat-automation | `users.py` |  |  | 94 |
| tchat-automation | `ws_client.py` |  |  | 147 |
| team-registry | `team.py` |  | bootstrap, get, list, match, remove, set | 223 |

## Test: 32 file, 590 hàm test

- `test_agent_init.py`
- `test_agentkit.py`
- `test_cg.py`
- `test_clarify.py`
- `test_claude_account_rotation.py`
- `test_code_context.py`
- `test_coordinator.py`
- `test_core_primitives.py`
- `test_dbeaver.py`
- `test_debate_loop.py`
- `test_fix_loop.py`
- `test_grounding_review.py`
- `test_intake_gate.py`
- `test_intake_lanes.py`
- `test_isolation_guard.py`
- `test_k8s.py`
- `test_layering.py`
- `test_mr_review.py`
- `test_orca_adapter.py`
- `test_proposals.py`
- `test_refactor.py`
- `test_refactor_program.py`
- `test_review_flow.py`
- `test_run_log.py`
- `test_store_engine.py`
- `test_task_queue.py`
- `test_triage.py`
- `test_usage_throttle.py`
- `test_verify_gate.py`
- `test_verify_ladder.py`
- `test_vpn.py`
- `test_winexec.py`
