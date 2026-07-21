# Stack-verify — kiểm thử full luồng thành phần backend

Bộ probe để assert state thật của từng thành phần backend, cộng `flow_check.py` chạy
kịch bản e2e xuyên thành phần. Dùng ở **bước Test** của pipeline auto-dev, *sau* khi unit
test (`test_runner.py`) đã xanh.

Tất cả tool ở `.claude/skills/dev-automation/tools/`, chỉ dùng **stdlib** (HTTP qua `urllib`,
Redis qua socket RESP, DB qua bọc CLI `psql`/`mysql`, Kafka qua REST Proxy). Config đọc từ
`.env` (xem khối "Stack-verify toolkit" trong `.env.sample`). **Trỏ vào môi trường TEST**, đừng prod.

> **DB config lấy từ đâu?** Ưu tiên registry (`work/projects.json` → `environments.<env>.db`);
> **thiếu thì tự đọc `application-<env>.yml` của app Spring** (`spring_config.py`, hỗ trợ cả
> layout JHipster `src/main/resources/config/`) — probe hit đúng DB mà app đang dùng, không cần
> khai lại. Registry luôn thắng theo từng khoá. Xem nhanh:
> `python spring_config.py read --project <P> --env dev` (mật khẩu che, `--show-secrets` để hiện).

## Probe lẻ (mỗi thành phần)

```bash
cd .claude/skills/dev-automation/tools

# API — gọi endpoint, assert status + field JSON
python probe_api.py call --method POST --url /api/users \
  --body '{"name":"A"}' --expect-status 201 --expect-json '$.status=ACTIVE' --save 'uid=$.id'

# DB — chạy query, assert số dòng/giá trị. KHÔNG cần psql/mysql CLI (socket thuần stdlib).
python probe_db.py query --engine postgres --sql "select status from users where id=42" \
  --schema drive --expect-rows 1 --expect-value ACTIVE       # --schema = set search_path (postgres)
python probe_db.py query --engine mysql --sql "select count(*) from orders" --expect-value 0
python probe_db.py query --engine postgres --sql "..." --dry-run   # xem lệnh, mask mật khẩu

# DB pre-flight — assert kết nối ĐÚNG database (cho env cô lập / isolated DB).
# Isolation chỉ ĐỔI TÊN db trong config, KHÔNG tạo db; check-db xác nhận db tồn tại
# VÀ đang nối đúng db kỳ vọng — sai/thiếu db trả {"error":true} (không tính là pass).
python probe_db.py check-db --engine postgres --expect-db atask_task_123

# Redis — kiểm cache
python probe_redis.py get user:42 --expect-exists
python probe_redis.py ttl user:42 --expect-ttl-min 30
python probe_redis.py exists cart:42 --expect-missing

# Kafka — kiểm message qua REST Proxy
python probe_kafka.py produce --topic user.created --value '{"uid":42}'
python probe_kafka.py consume --topic user.created --timeout 15 --expect-contains '"uid":42'

# Kafka qua Provectus Kafka UI (KHÔNG phải REST Proxy) — login form + cookie.
# Hầu hết là đọc; chỉ `produce` là ghi (bị prod-guard chặn nếu env protected).
python kafka_ui.py login-check                       # xác thực phiên
python kafka_ui.py clusters                          # list cluster + status
python kafka_ui.py topics  --cluster <c>             # list topic (--expect-contains-topic <t>)
python kafka_ui.py topic   --cluster <c> --topic <t> # mô tả 1 topic (partitions/replication)
python kafka_ui.py messages --cluster <c> --topic <t> --from latest --limit 20 \
  --expect-contains '"status":"accepted"' --expect-min-count 1
# --from latest = message mới nhất (mặc định) | beginning = từ đầu topic
python kafka_ui.py produce --cluster <c> --topic <t> --value '{"uid":42}' --key 42  # [WRITE]

# Jenkins — chạy pipeline CI/CD, chờ kết quả (+ discovery read-only)
python jenkins.py build   --job my-service --param BRANCH=develop --wait
python jenkins.py status  --job my-service --number 42        # đọc trạng thái 1 build
python jenkins.py console --job my-service --number 42 --tail 40
python jenkins.py info    --job my-service                    # đọc: last/last-successful build #
python jenkins.py jobs                                        # đọc: liệt kê job/folder (discovery)
```

> **Chờ-nền dưới Telegram bridge** — ĐỪNG spawn poll nền rồi kết thúc lượt với câu
> "mình sẽ tự báo khi xong": phiên `claude -p` headless thoát ngay khi lượt kết thúc →
> tiến trình nền mồ côi, KHÔNG còn agent để báo về (bạn sẽ không nhận được gì). Với mọi
> tác vụ dài (jenkins build, compile, test…) hãy bọc bằng `bg_notify.py` — nó tách rời
> khỏi `claude -p`, chạy tới khi xong rồi TỰ gửi kết quả (✅/❌ + thời lượng) về Telegram:
> ```
> python bg_notify.py --label "Build dev atask" -- python jenkins.py build --project atask --env dev --wait
> python bg_notify.py --label "Compile/test atask" -- python test_runner.py run --project atask --kind test
> ```
> In ngay `{"detached": true, …}` → báo người dùng "đã chạy nền, sẽ nhắn khi xong" rồi
> KẾT THÚC lượt. (Tự bật khi env `CLAUDE_TG_BRIDGE=1`; chạy tay thì mặc định đồng bộ.)

Mọi probe trả JSON `{"passed": bool, ..., "checks": [...]}`. `passed:false` là **assertion
fail** (đọc `checks`), khác với `{"error": true}` là **không chạy được** (mất kết nối/thiếu config/thiếu CLI).

## Bàn giao API cho FE/tester — `postman_gen.py`

Sau khi viết feature/fix, sinh **Postman Collection** để FE/tester import là dùng (không cần
chỉnh sửa). Dùng khi backend **không có Swagger/OpenAPI** — tool parse controller Spring.

```bash
python postman_gen.py --src <thư mục source Spring> --base-url https://atask.dev
python postman_gen.py --project atask          # src = clone_dir trong ./work/projects.json
python postman_gen.py --src ./work/atask --out temp/atask.postman_collection.json
```

Sinh ra file `*.postman_collection.json` (mặc định trong `temp/`): folder theo controller, mỗi
endpoint có method + URL `{{baseUrl}}` + path var `:id` + query param + body skeleton (kèm tên
DTO trong description), auth bearer `{{token}}`. FE: Postman → Import → chọn file → set biến
`baseUrl`/`token` một lần → chạy mọi endpoint.

Trích được: `@RestController`, base `@RequestMapping`, `@Get/Post/Put/Delete/PatchMapping`,
`@RequestMapping(method=...)`, `@PathVariable`, `@RequestParam`, `@RequestBody`.

> [Giới hạn] Parser theo regex (không phải trình phân tích Java đầy đủ): **body chỉ là skeleton
> rỗng** + ghi tên DTO, **không tự bung field**. Định dạng annotation lạ có thể bị bỏ sót — nên
> rà lại kết quả. Muốn body đầy đủ field thì cần bước bung DTO (làm sau nếu cần).

## Full luồng — `flow_check.py`

Viết kịch bản JSON, mỗi bước là một probe; biến `save`/`saveFrom` truyền sang bước sau qua
`{tên}`. Dừng ở bước fail đầu tiên (trừ khi bước đặt `"continue_on_fail": true`).

```bash
python flow_check.py --file ../../auto-dev/scenarios/example-create-user.json
python flow_check.py --file s.json --var name=Bob          # seed biến
echo '{...}' | python flow_check.py --stdin
```

Mẫu: `auto-dev/scenarios/example-create-user.json` (API tạo user → DB → Kafka → Redis).

### Cú pháp bước

| type | field chính | expect |
|---|---|---|
| `api` | method, url, body, headers, saveFrom`{var:$.path}` | status, json`{$.path:val}`, contains[] |
| `db` | engine(postgres\|mysql), sql, database | rows, value, empty, contains[] |
| `redis` | op(get/exists/ttl/keys/set/del), key, value, ex | exists, missing, value, ttl_min, count |
| `kafka` | op(produce/consume), topic, value, timeout | contains, json`"$.p=v"`, min_count |
| `jenkins` | job/path, params`{}`, wait, timeout | (passed = build SUCCESS) |
| `wait` | seconds | — |

- `{var}` thay bằng giá trị đã `save`. `"*"` trong expect = "có giá trị, bất kỳ".
- `save` ở cấp bước trích từ **kết quả bước** qua JSONPath (vd `{"save":{"n":"$.rows"}}`);
  `saveFrom` (chỉ api) trích từ **body JSON** của response.

## Chạy app local rồi test e2e (mvn → gọi API → soi DB) — `local_app.py`

`flow_check`/`probe_*` assert vào service **đang chạy**; chúng không tự khởi động app. `local_app.py`
lấp chỗ đó: start app trên localhost (vd `mvn spring-boot:run`), chờ health endpoint UP, sau khi
chạy scenario thì stop. Vòng đầy đủ: **build/run → gọi API → theo dõi DB → tear down**.

```bash
cd .claude/skills/dev-automation/tools
# 1) start (detached, log vào temp/local_apps/<name>.log). --project lấy cwd = clone_dir.
python local_app.py start --name atask --project atask \
  --cmd "mvn -q spring-boot:run -Dspring-boot.run.profiles=dev"
# 2) chờ tới khi UP (JHipster: /management/health -> {"status":"UP"}); fail nhanh nếu app chết
python local_app.py wait-health --name atask \
  --url http://localhost:8271/management/health --timeout 300 --expect-text UP
python local_app.py logs --name atask --tail 80      # xem log boot nếu lỗi
# 3) chạy kịch bản e2e: gọi API tạo task -> SELECT bảng task xác nhận row
API_BASE_URL=http://localhost:8271 API_AUTH_HEADER="X-aTask-PAT: <PAT>" \
  python flow_check.py --file ../../auto-dev/scenarios/atask-create-task-e2e.json \
  --project atask --env dev --var listId=<list_task_id_thật> --allow-prod
# (--allow-prod CHỈ vì step có ghi; xem cảnh báo DB bên dưới)
# 4) stop (kill cả cây tiến trình mvn -> java)
python local_app.py stop --name atask
```

Mẫu: `auto-dev/scenarios/atask-create-task-e2e.json` (POST `/api/ai/execute` `create_task` → SELECT
`task` → dọn `delete_task`). Sửa JSONPath `$.data.id` cho khớp response thật (chạy riêng step 1 xem JSON trước).

> ⚠️ **DB & môi trường (BẮT BUỘC đọc).** Profile `dev` của atask trỏ datasource vào **DB dev DÙNG
> CHUNG** (vd `10.x.x.x/atask_dev`) và cần eureka/redis/ES/kafka/UAA reachable. Chạy e2e có GHI
> theo cách này **làm bẩn data chung** và phụ thuộc cả hệ backend dùng chung. **Khuyến nghị**: trỏ app + probe
> vào **DB cục bộ/cô lập** trước khi chạy ghi:
> ```bash
> # ví dụ ép datasource sang MySQL local (qua env Spring), rồi guard đúng DB trước khi test:
> python local_app.py start --name atask --project atask --env "SPRING_DATASOURCE_URL=jdbc:mysql://localhost:3306/atask_local?useSSL=false" --cmd "mvn -q spring-boot:run -Dspring-boot.run.profiles=dev"
> DB_HOST=localhost DB_NAME=atask_local python probe_db.py check-db --project atask --env dev --expect-db atask_local
> ```
> `check-db` đảm bảo probe (và mặc nhiên app) đang ở DB cô lập — sai DB trả `{"error":true}`, không
> tính pass. Chỉ dùng `--allow-prod` khi thật sự cần ghi vào env protected (xác nhận thủ công).

Tác vụ dài (mvn build/run, test suite) → bọc `bg_notify.py` nếu chạy dưới Telegram bridge (xem khối
cảnh báo "Chờ-nền dưới Telegram bridge" ở đầu file).

## Tích hợp vào cổng Test của auto-dev

Bước Test (xem `pipeline.md` §4) chạy theo thứ tự, dừng ở fail đầu tiên:

1. **Unit/build** — `test_runner.py run --project <P> --kind test` (bắt buộc; JHipster: Surefire
   `*Test` qua `./mvnw` — tự ưu tiên wrapper nếu repo có `mvnw`/`gradlew`, tránh lệch version).
2. **Integration probe / e2e** — nếu task chạm DB/API/Kafka/Redis: `flow_check.py --file <scenario>`
   hoặc probe lẻ cho phần thay đổi.
3. **(tuỳ chọn) CI** — `jenkins.py build --job <job> --wait` nếu muốn pipeline Jenkins xanh trước khi tạo MR.

Chỉ qua checkpoint `before_mr` khi **tất cả** cổng trên xanh. Retry-fix tối đa 3 lần như unit test.

### Integration test (`*IT`) + frontend — THỦ CÔNG (không auto-gate)

JHipster giữ integration test nặng (Testcontainers) ở Failsafe (`mvn verify`), và test UI ở
`package.json`. Cổng test **tự động chỉ chạy unit** để nhanh. Khi task chạm sâu runtime, **đề xuất
người dùng bấm** chạy thêm — đừng tự chạy vì có thể mất vài phút:

```bash
python test_runner.py run --project <P> --kind integration   # ./mvnw verify → chạy *IT (Failsafe)
python test_runner.py run --project <P> --kind frontend      # npm test (kéo từ package.json dù pom.xml thắng)
```

`detect` liệt kê cả `integration`/`frontend` để biết lệnh sẽ chạy trước khi xác nhận:
`python test_runner.py detect --project <P>`.

## Đa project × đa môi trường (multi-project, multi-env)

Mỗi project có nhiều môi trường (local/dev/uat/sandbox/prod), mỗi env có endpoint riêng.
Khai báo trong `./work/projects.json` (mẫu: `workspace/projects.sample.json`):
- `stack` = **base dùng chung** mọi env (vd db user/port, jenkins job).
- `environments.<env>` = **override** lên base (deep-merge), thường chỉ khác host/URL.
- `default_env` = env mặc định khi không truyền `--env`.
- `protected_envs` = danh sách env cấm ghi (mặc định `["prod","production"]`).

Mọi probe + `flow_check` nhận `--project <name> --env <env>`:

```bash
python probe_db.py query --engine postgres --sql "select ..." --project atask --env uat --expect-rows 1
python probe_redis.py get user:42 --project atask --env dev --expect-exists
python flow_check.py --file <scenario> --project atask --env sandbox
python jenkins.py build --project atask --env dev --wait    # job lấy từ stack.jenkins.job
```

Kịch bản tự gắn bằng field gốc `"project":"atask"` và `"env":"dev"` (cờ CLI đè field này).
Không truyền `--env` mà project có nhiều env và **không có** `default_env` → tool báo lỗi rõ
(liệt kê env hợp lệ), **không đoán** env.

### Guard prod (an toàn bắt buộc)

Thao tác **ghi** vào env trong `protected_envs` bị **từ chối**, trừ khi thêm `--allow-prod`:

| Tool | Bị coi là "ghi" |
|---|---|
| probe_api | method ≠ GET/HEAD/OPTIONS |
| probe_db | SQL không bắt đầu bằng select/show/explain/desc/values/with |
| probe_redis | op = set / del |
| probe_kafka | produce |
| kafka_ui | produce (các lệnh khác đều đọc) |
| jenkins | trigger / build |
| flow_check | bất kỳ step nào ở trên |

Thao tác **đọc** (GET, select, redis get/exists/ttl, kafka consume, jenkins
status/console/info/jobs, kafka_ui clusters/topics/messages, probe_db check-db)
vào prod **luôn được phép**. Pipeline auto-dev nên test ở dev/uat/sandbox; chỉ đụng prod khi
thật sự cần và có `--allow-prod` (tương đương xác nhận thủ công theo guardrail CLAUDE.md).

**Thứ tự ưu tiên config** (cao → thấp): cờ CLI tường minh → env shell → `environments.<env>`
→ `stack` base → `.env` chung. Tức `--project/--env` nạp endpoint (đè `.env`), nhưng
`DB_HOST=... python probe_db.py ...` inline vẫn thắng để override tạm.

```json
"atask": {
  "default_env": "dev",
  "protected_envs": ["prod", "production"],
  "stack":   { "db": {"engine":"postgres","port":"5432","user":"app","name":"atask"},
               "redis": {"port":"6379","db":"0"}, "jenkins": {"job":"team/job/atask-ci"} },
  "environments": {
    "local": { "api_base_url":"http://localhost:8080", "db":{"host":"localhost"}, "redis":{"host":"localhost"}, "kafka_rest_url":"http://localhost:8082" },
    "dev":   { "api_base_url":"https://atask.dev", "db":{"host":"pg.dev"}, "redis":{"host":"redis.dev"}, "kafka_rest_url":"http://kafka-rest.dev:8082" },
    "uat":   { "api_base_url":"https://atask.uat", "db":{"host":"pg.uat"} },
    "prod":  { "api_base_url":"https://atask.prod", "db":{"host":"pg.prod"} }
  }
}
```
> Endpoint **không bí mật** để trong registry (`./work`, gitignore, không commit). Secret dùng chung nên
> để `.env`; nếu để token/password theo env thì giữ file private — env shell luôn override.

## Lưu ý môi trường

- Probe cần **service test đang chạy** (DB/Redis/Kafka REST/API/Jenkins reachable). Thiếu →
  probe trả `{"error":true}`; coi đó là "chưa kiểm được", không phải "đạt".
- DB: **KHÔNG cần CLI `psql`/`mysql`** — `probe_db` nói chuyện trực tiếp qua socket như
  probe_redis/kafka. MySQL/MariaDB qua `mysql_client.py` (auth `mysql_native_password`);
  PostgreSQL qua `postgres_client.py` (auth SCRAM-SHA-256 / MD5 / cleartext, PG 10+ default
  là SCRAM; `--schema` đặt search_path). Cả hai **non-TLS** — cần TLS thì dùng CLI riêng.
- Kafka: hai cơ chế đã hỗ trợ — **Confluent REST Proxy** (`probe_kafka.py`, produce+consume,
  `KAFKA_REST_URL`) và **Provectus Kafka UI** (`kafka_ui.py`, đọc + produce, login form
  `KAFKA_UI_URL/USER/PASSWORD`). Chọn theo hạ tầng thật; hạ tầng khác nữa thì bổ sung probe.
- TChat notification: **chưa làm** (hoãn theo quyết định). Khi cần, thêm `tchat.py`
  theo pattern `notifier.py`.
