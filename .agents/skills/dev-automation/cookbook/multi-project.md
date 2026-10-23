# Làm việc trên nhiều project (multi-project)

Bộ skill này (`fork-repository-skill`) là **toolset dùng chung**. Workspace project sống trong
`./work/` ngay trong repo (đã **gitignore** → thấy được nhưng không commit). Tool đọc registry
tại `<repo>/work/projects.json` (đè bằng env `WORK_DIR` nếu cần).

```
<repo>/work/
  projects.json     # registry: project → {gitlab_project_id, azure_project, clone_dir, stack, environments, ...}
  proj.sh           # shell switcher cho terminal của con người
  .profiles/        # (tuỳ chọn) file profile per-project
  atask/            # git clone <group>/atask
  <project-B>/      # git clone ...
```

## Nguyên lý

Tool đọc config qua `config.get()` → ưu tiên **biến môi trường** rồi mới tới `.env`
(`os.environ.setdefault`). Vì vậy chỉ cần export đúng `GITLAB_PROJECT_ID` /
`AZURE_DEVOPS_PROJECT` là cùng một bộ tool trỏ sang project khác — **không sửa code, không sửa
`.env`**. Token Azure/GitLab giữ nguyên 1 chỗ trong skill `.env` (dùng chung cho cả org).

## Cho con người (terminal)

```bash
source ./work/proj.sh     # thêm dòng này vào ~/.bashrc
proj                      # liệt kê project
proj atask                # export env + cd vào ./work/atask
# từ đây mọi lệnh dev-automation tự trỏ đúng project atask
```

## Cho agent (Claude Code)

Đọc `./work/projects.json` để lấy `gitlab_project_id` / `azure_project` / `clone_dir` của
project đang làm, rồi **prefix env inline** mỗi lệnh (vì mỗi Bash call là shell mới):

```bash
# ví dụ project có gitlab_project_id=123, azure_project=My-Project
GITLAB_PROJECT_ID=123 AZURE_DEVOPS_PROJECT=My-Project \
  python3 gitlab_api.py list-mrs opened
```

Bước viết code/commit/push chạy bên trong `clone_dir` của project tương ứng.

## Thêm project mới

1. Clone repo về workspace:
   ```bash
   git clone https://gitlab.your-company.com/<group>/<repo>.git ./work/<name>
   ```
   (Để tránh nhúng token vào remote: clone xong set remote sạch + dùng credential helper.)
2. Lấy `GITLAB_PROJECT_ID`: mở repo trên GitLab → Settings → General (hoặc qua API).
3. Thêm 1 mục vào `./work/projects.json` (mẫu đầy đủ: `workspace/projects.sample.json` trong skill repo):
   ```json
   "<name>": {
     "gitlab_project_id": "<id>",
     "gitlab_path": "<group>/<repo>",
     "azure_project": "<azure project nếu khác>",
     "clone_dir": "<đường dẫn tuyệt đối tới ./work/<name>>",
     "default_target_branch": "develop",
     "build_cmd": "mvn -B -q -DskipTests package",
     "test_cmd": "mvn -B test",
     "lint_cmd": "mvn -B -q checkstyle:check"
   }
   ```
4. Xong — `proj <name>` hoặc prefix env là dùng được ngay.

## Cổng test cho auto-dev (`*_cmd`)

`build_cmd` / `test_cmd` / `lint_cmd` là **mở rộng cho skill `auto-dev`** — tool
`test_runner.py` đọc chúng để chạy cổng test trước khi tạo MR:

```bash
python test_runner.py run --project <name> --kind test   # đọc test_cmd
python test_runner.py detect --project <name>            # xem lệnh resolve/auto-detect
```

- **Bỏ trống một `*_cmd`** → `test_runner.py` tự dò theo file mốc trong `clone_dir`
  (`pom.xml`→maven, `package.json`→npm, `build.gradle`→gradle, `pyproject.toml`→pytest, `go.mod`→go).
- **Chưa có `./work/projects.json`** → truyền trực tiếp: `--cwd <dir> --cmd "<lệnh test>"`.

Để kiểm thử **integration/e2e đa project** (API/DB/Redis/Kafka/Jenkins per-project), thêm khối
`stack` vào mỗi mục project và dùng cờ `--project <name>` cho các probe — chi tiết:
`auto-dev/cookbook/stack-verify.md` §Đa project.

### Nhánh git theo môi trường (branch-per-env)

Nếu project có **nhánh riêng cho từng môi trường** (vd `dev`, `uat`, `pre-prod`, `prod`), khai
báo `branch` trong từng `environments.<env>`. Auto-dev sẽ **branch off** và **MR target** về
nhánh của env đang làm:
```json
"environments": {
  "dev":     { "branch": "dev",      "jenkins": { "job": "..." } },
  "uat":     { "branch": "uat" },
  "sandbox": { "branch": "pre-prod" },
  "prod":    { "branch": "prod" }
}
```
Lấy nhánh đích: `python -c "import project_config as p; print(p.target_branch('<name>','<env>'))"`
→ ưu tiên `environments.<env>.branch`, fallback `default_target_branch`. `prod` nằm trong
`protected_envs` nên thao tác **ghi/deploy** vào prod cần `--allow-prod` (xác nhận thủ công).

## Lưu ý

- Token dùng chung: nếu một project nằm ở **org/GitLab khác**, phải thêm token riêng — khi đó
  khai báo cả `GITLAB_PRIVATE_TOKEN` / `AZURE_DEVOPS_PAT` trong profile của project đó.
- `clone_dir` là đường dẫn máy-cụ-thể → `projects.json` nằm ở `<repo>/work` (đã gitignore, không commit).
