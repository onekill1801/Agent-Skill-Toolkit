# Multi-repo task (một task sửa nhiều project)

Dùng khi một task chạm ≥2 repo (vd `etask` BE + `etask-gateway` + FE). Coordinator chạy ở
**folder context** của Orca (hoặc thư mục toolkit), mỗi repo một worktree con + một worker.

## 1. Registry: khai báo nhóm sản phẩm

`work/projects.json` (gitignored) — thêm khoá `products` cạnh các project:

```json
"products": {
  "etask-suite": {
    "repos": ["etask", "etask-gateway", "etask-fe"],
    "depends": {"etask-gateway": ["etask"], "etask-fe": ["etask-gateway"]},
    "contract_paths": {"etask": ["src/main/resources/swagger/", "src/main/java/**/web/rest/dto/"]},
    "integration_env": "dev"
  }
}
```
- `depends`: repo B phụ thuộc hợp đồng của A → task của B có `--deps` vào task của A.
- `contract_paths`: nơi chứa hợp đồng (OpenAPI, DTO, event schema) — worker phía sau ĐỌC từ đây.

## 2. Luồng coordinator

| Bước | Việc | Công cụ |
|---|---|---|
| 1 | Làm rõ yêu cầu, xác định repo bị ảnh hưởng | `clarify`, codegraph/grep từng repo |
| 2 | **Chốt hợp đồng trước** (API path, request/response DTO, Kafka topic + schema, mã lỗi) vào `.handoff/<task>/contract.md` | coordinator |
| 3 | Duyệt plan + hợp đồng | gate ✋ |
| 4 | Mỗi repo: worktree con cùng tên nhánh `<type>/<task_id>-<desc>` | `orca worktree create --repo name:<repo> --parent-worktree folder:<id>` |
| 5 | Task DAG theo `depends`; mỗi worker CHỈ sửa repo của mình, đọc `contract.md` | `orca orchestration task-create --deps`, `worker-start --agent <router>` |
| 6 | Mỗi worker: code → test repo đó → ghi `.handoff/<task>/<repo>.result.json` | `test_runner.py` |
| 7 | **Verify tích hợp** khi mọi worker xong: chạy cùng lúc (local) hoặc deploy dev | `local_app.py` + `runtime_isolator.py` · `jenkins.py` + `k8s.py logs --errors` + `flow_check.py` |
| 8 | Lỗi tích hợp → giao lại đúng worker kèm log | fix loop |
| 9 | **Một gate cho cả bộ MR**; mô tả mỗi MR liệt kê MR liên quan + THỨ TỰ MERGE | gate ✋ → `gitlab_api.py create-mr` |

## 3. Quy tắc

- Hợp đồng đổi giữa chừng → coordinator cập nhật `contract.md`, báo MỌI worker phụ thuộc (không để worker tự đổi).
- Codegraph index từng repo riêng: lời gọi HTTP/Kafka giữa repo KHÔNG có trong graph — hợp đồng là nguồn sự thật.
- Merge theo thứ tự `depends` (A trước B); MR sau ghi "chờ !<iid A> merge".
- Model/tài khoản cho từng worker: `agent_runner.run_role(..., role="implement")` theo `work/agents.json`;
  review chéo bằng model khác (`avoid=` backend đã viết code).
