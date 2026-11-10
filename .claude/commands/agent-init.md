---
description: "Dựng chuẩn kiến thức .agent/ cho một repo sản phẩm (vendor-neutral)"
---

# agent-init — chuẩn kiến thức `.agent/`

Đưa kiến thức project ra khỏi file chỉ-một-agent-đọc (`CLAUDE.md`) vào `.agent/`,
rồi để mọi file vendor thành **con trỏ mỏng**. Agent nào cũng đọc được; kiến thức
chỉ có một bản nên không thể lệch.

## Chạy

```bash
python agentkit.py agent_init scan  --project <P>          # facts tất định, không AI
python agentkit.py agent_init draft --project <P>          # dựng vào temp/agent_init/<P>/
python agentkit.py agent_init apply --project <P>          # dry-run: xem sẽ ghi gì
python agentkit.py agent_init apply --project <P> --yes    # [WRITE] ghi vào repo sản phẩm
python agentkit.py agent_init check --project <P>          # drift: knowledge vs code
python agentkit.py agent_init docs  --project <P> [--yes] # xếp kho tài liệu cũ thành 4 tầng
```

## `docs` — xếp tài liệu sẵn có

Repo chạy một năm sẽ có plan/progress/report nằm lẫn với tài liệu thật. Agent đọc
`PHASE2_PROGRESS.md` như ý định hiện tại — tài liệu cũ còn hại hơn không có tài liệu.

| Tầng | Ở đâu | Mức tin |
|---|---|---|
| Canon | `.agent/` + `.agent/decisions/` | Đọc trước tiên |
| Guides | `docs/guides/` | Quy tắc cross-cutting, còn hiệu lực |
| Reference | thư mục đã curated (`api/`, `perf/`…) | **Giữ nguyên chỗ** |
| Archive | `docs/archive/` | **Điểm-thời-gian, KHÔNG phải ý định hiện tại** |

Mặc định **không** đụng thư mục đã curated: comment trong mã nguồn trỏ vào chúng theo
đường dẫn, di chuyển = phải sửa hàng chục file mã cho một lợi ích thẩm mỹ. Cần thì
`--include-reference`.

Dùng `git mv` (giữ lịch sử), rewrite link trong `.md` **và** `.java/.xml/.yml/...`,
dọn thư mục rỗng, rồi sinh lại `project.yml` + `.agent/index.md`. Không xoá gì.

## Project MỚI — MỘT lệnh

```bash
python agentkit.py agent_init add --repo <git-url>          # dry-run: xem sẽ làm gì
python agentkit.py agent_init add --repo <git-url> --yes    # clone + tra GitLab + registry + draft
```

Nó làm 4 việc: clone vào `work/<name>` · tra `gitlab_project_id` + default branch qua API
(hỏng API thì vẫn chạy tiếp, chỉ thiếu field) · ghi registry · `draft`.

**Dừng ở `draft`, không `apply`** — người phải nhìn thứ sắp vào repo công ty trước đã.
Clone hỏng thì báo lỗi và **không** ghi registry nửa vời.

Sau đó: `agent_init apply --project <name> --yes` (và `docs --yes` nếu repo có kho tài liệu cũ).

## Project MỚI — 3 bước thủ công (nếu không dùng `add`)

```bash
# 1. clone vào work/ (work/ đã gitignore)
git clone <url> work/<name>

# 2. đăng ký — agent_init đọc clone_dir từ đây, không đoán
python agentkit.py registry add <name> --clone-dir "$(pwd)/work/<name>"        --gitlab-path <group>/<name> --default-target-branch <branch>

# 3. dựng chuẩn
python agentkit.py agent_init draft --project <name>     # xem trước ở temp/
python agentkit.py agent_init apply --project <name> --yes
python agentkit.py agent_init docs  --project <name> --yes   # nếu repo có kho docs cũ
```

Không có bước 2 thì bước 3 báo lỗi rõ ràng (`registry has no clone_dir`), không đoán bừa.

`registry.py` và `agent_init` chấp nhận cả hai cách đặt tên field
(`gitlab_path`/`repo_path`, `default_target_branch`/`default_branch`).

## Luồng chuẩn

1. `scan` — xem tool hiểu repo đúng chưa (stack, commands, modules, datastores).
2. `draft` — **đọc `dropped_lines`**: migrate là verbatim, dòng nào bị bỏ phải thấy rõ.
   Đọc `ignored_pointers`: con trỏ bị `.gitignore` nuốt = đồng đội không bao giờ thấy.
3. Xem lại `temp/agent_init/<P>/` bằng mắt. Đây là nội dung sẽ vào repo công ty.
4. `apply --yes` → `git diff` trong repo sản phẩm → **người quyết định commit**, không phải agent.
5. `check` định kỳ (hoặc trong CI) — repo chạy tiếp mà `.agent/` đứng yên thì kiến thức thành sương mù.

## Ràng buộc

- `project.yml` **được commit** → tuyệt đối không chứa host/user/password. Hạ tầng nằm ở
  `work/projects.json` + `.env`.
- Không sửa file con trỏ. Sửa `.agent/`.
- `apply` không tự commit, không tự tạo branch, không push.
