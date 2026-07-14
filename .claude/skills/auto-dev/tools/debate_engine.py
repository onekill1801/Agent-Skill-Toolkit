#!/usr/bin/env python3
"""Agent Debate Engine — tranh biện đa-agent ở tầng Lên kế hoạch của pipeline /auto-dev.

Trước khi đụng vào code thật, ba vai AI tranh luận để thẩm định kiến trúc, soi lỗ hổng bảo mật
và tối ưu hiệu năng. Kết quả là một bản đặc tả (spec) đã được phản biện, lưu thành
`temp/runs/{task_id}_plan.xml` để Agent phụ (fork ra git worktree) đọc và lập trình theo.

Giao tiếp Agent↔Agent dùng **thẻ HTML/XML nghiêm ngặt** (xem
`auto-dev/prompts/SYSTEM_PROMPT.md`); bóc tách bằng `fork-terminal/tools/agent_parser.py`.

Luồng (state machine có VÒNG LẶP lên plan):
    1. Đề xuất:      Dev        -> <dev_proposal>                (một lần)
    2. Vòng lặp (tối đa --rounds vòng, mặc định 2):
         a. Phản biện:  Architect -> <architect_critique>        (soi SQLi / rate-limit / connection
                                                                  pool / memory leak / thiếu cache),
                        kết bằng <verdict>APPROVE|REVISE</verdict>
         b. Nếu APPROVE -> hội tụ sớm, thoát vòng.
         c. Nếu REVISE  -> Dev sửa -> <dev_rebuttal>; vòng sau Architect soi CHÍNH bản sửa đó.
    3. Phán quyết:   Moderator  -> <final_specification>  (chốt, chứa <target_files> ...)

BACKEND — KHÔNG cần ANTHROPIC_API_KEY. Mặc định gọi CLI agent bản subscription đã cài trên máy
(đồng bộ với skill `fork-terminal`), chạy ở chế độ headless/print và bắt stdout. Cờ dưới đây đã
đối chiếu `claude --help` / `agy --help` thực tế:
    --backend claude   ->  claude -p --model <m> --dangerously-skip-permissions   (Claude Code sub)
    --backend cursor   ->  agent -p --output-format text --model <m> --force --trust (Cursor CLI)
    --backend agy      ->  agy --model <m> --print --dangerously-skip-permissions  (Antigravity)
    --backend custom   ->  tự đặt qua --cmd-template "<lệnh> {model}"
    --backend api      ->  Claude API qua urllib (CHỈ backend này cần ANTHROPIC_API_KEY)
    --dry-run          ->  mock, không gọi gì (test state machine + parser)

> Cursor CLI có binary tên `agent` (headless qua -p); nếu máy bạn đặt tên khác (vd cursor-agent),
> dùng `--cmd-template`. CLI print-mode không có ô "system" riêng nên prompt phân vai được GHÉP
> vào trước nội dung task.

Stdlib-only (subprocess/urllib), không cần pip install.

Usage:
    python debate_engine.py run --task 6955 --desc "Thêm API export báo cáo doanh thu"
    python debate_engine.py run --task 6955 --desc "..." --backend agy
    python debate_engine.py run --task t --desc "..." --backend custom \
        --cmd-template "claude -p --model sonnet --dangerously-skip-permissions"
    python debate_engine.py run --task demo --desc "..." --dry-run

Đầu ra:
    - Transcript tranh biện (có màu) -> STDERR  (cho người xem trực tiếp)
    - Một dòng JSON tổng kết         -> STDOUT  (cho orchestrator)
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

# --- nạp các module nội bộ (stdlib path juggling) -------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.dirname(os.path.dirname(_HERE))  # .../.claude/skills
sys.path.insert(0, os.path.join(_SKILLS, "dev-automation", "tools"))
sys.path.insert(0, os.path.join(_SKILLS, "fork-terminal", "tools"))

import config  # noqa: E402  (reuse .env loader + UTF-8 stdout fix)
import agent_parser  # noqa: E402  (bóc tách thẻ HTML/XML giữa các vòng)

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-4-8"      # chỉ dùng cho backend "api"
DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MAX_TOKENS = 4000              # backend "api"
CLI_TIMEOUT = 600                      # CLI agent có thể chạy lâu
API_TIMEOUT = 300


class DebateError(Exception):
    """Lỗi không phục hồi được trong lúc tranh biện (config/CLI/API/refusal)."""


# --- 1. ĐỊNH NGHĨA 3 PHÂN VAI (SYSTEM PROMPTS) -----------------------------------------------
# Mỗi vai được đặt tên bằng thẻ HTML và BẮT BUỘC chỉ trả lời trong đúng một khối thẻ của mình.

ROLE_PROMPTS = {
    "agent_dev": """<agent_dev>
Bạn là một lập trình viên năng nổ, ưu tiên TỐC ĐỘ THỰC THI: viết code nhanh, gọn, đi thẳng vào
giải pháp khả thi nhỏ nhất chạy được. Bạn không thích vẽ vời quá mức.
NHIỆM VỤ: đọc mô tả task và đề xuất một giải pháp kỹ thuật cụ thể (cách tiếp cận, các file sẽ
sửa, chiến lược test).
ĐỊNH DẠNG BẮT BUỘC: trả lời CHỈ trong một khối thẻ <dev_proposal>...</dev_proposal>, bên trong
gồm <approach>, <target_files><file>...</file></target_files>, <test_strategy>. TUYỆT ĐỐI KHÔNG
dùng ký tự Markdown (#, **, -). Không viết gì ngoài khối thẻ.
</agent_dev>""",

    "agent_architect": """<agent_architect>
Bạn là một kiến trúc sư giải pháp KHÓ TÍNH, chuyên bắt lỗi hệ thống. Nhiệm vụ là săm soi đề xuất
của Dev để tìm rủi ro, đặc biệt: SQL Injection, thiếu Rate Limit, nghẽn Database (Connection
Pool), Memory Leak, và thiếu tầng Cache (Redis/Kafka). Với mỗi rủi ro tìm thấy, nêu rõ vị trí và
đề xuất biện pháp khắc phục. Nếu một hạng mục không có rủi ro, nói rõ "không phát hiện".
Đây là tranh biện NHIỀU VÒNG: nếu đây không phải vòng đầu, bạn đang soi bản đã được Dev sửa —
chỉ nêu rủi ro CÒN LẠI, đừng lặp lại thứ Dev đã xử lý.
ĐỊNH DẠNG BẮT BUỘC: trả lời CHỈ trong một khối thẻ <architect_critique>...</architect_critique>,
bên trong dùng các thẻ con <risk category="sql_injection|rate_limit|connection_pool|memory_leak|
cache"> ... mô tả + khắc phục ... </risk>, và KẾT THÚC bằng đúng một thẻ
<verdict>APPROVE</verdict> (khi mọi rủi ro chặn đã được xử lý — không cần sửa thêm) HOẶC
<verdict>REVISE</verdict> (khi vẫn còn rủi ro Dev phải sửa). TUYỆT ĐỐI KHÔNG dùng Markdown.
Không viết gì ngoài khối thẻ.
</agent_architect>""",

    "agent_moderator": """<agent_moderator>
Bạn là Tech Lead / Thẩm phán. Bạn lắng nghe toàn bộ cuộc tranh luận (đề xuất của Dev, phản biện
của Architect, phần bảo vệ/sửa đổi của Dev), cân nhắc thiệt hơn giữa tốc độ và độ an toàn/hiệu
năng, rồi CHỐT phương án cuối cùng — đã hợp nhất các rủi ro hợp lệ mà Architect nêu.
ĐỊNH DẠNG BẮT BUỘC: trả lời CHỈ trong một khối thẻ <final_specification>...</final_specification>,
bên trong gồm <approach>, <target_files><file>...</file></target_files>, <test_strategy>,
<risks_addressed><risk>...</risk></risks_addressed>. TUYỆT ĐỐI KHÔNG dùng Markdown. Không viết gì
ngoài khối thẻ.
</agent_moderator>""",
}


# --- Terminal: màu sắc + ký hiệu để người dùng quan sát "cuộc cãi nhau" ----------------------

_ANSI = {
    "dev": "\033[32m",        # xanh lá
    "architect": "\033[31m",  # đỏ
    "moderator": "\033[36m",  # xanh cyan
    "system": "\033[90m",     # xám
    "reset": "\033[0m",
    "bold": "\033[1m",
}
_SYMBOL = {"dev": "🛠  DEV", "architect": "🔍 ARCHITECT", "moderator": "⚖  MODERATOR",
           "system": "··· SYSTEM"}


def _enable_ansi_on_windows():
    """Bật VT processing để mã màu ANSI hiển thị trên console Windows (cho STDERR)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-12), 7)  # -12 = STDERR
    except Exception:
        pass


class Narrator:
    """In transcript tranh biện ra STDERR (người xem); STDOUT để dành cho JSON kết quả."""

    def __init__(self, use_color: bool):
        self.use_color = use_color
        if use_color:
            _enable_ansi_on_windows()

    def _c(self, key: str) -> str:
        return _ANSI.get(key, "") if self.use_color else ""

    def banner(self, who: str, title: str):
        sym = _SYMBOL.get(who, who)
        line = "─" * 60
        print(f"{self._c(who)}{self._c('bold')}{line}\n{sym} — {title}\n{line}"
              f"{self._c('reset')}", file=sys.stderr, flush=True)

    def speech(self, who: str, text: str):
        color = self._c(who)
        for ln in text.splitlines() or [""]:
            print(f"{color}│{self._c('reset')} {ln}", file=sys.stderr, flush=True)
        print("", file=sys.stderr, flush=True)

    def system(self, msg: str):
        print(f"{self._c('system')}··· {msg}{self._c('reset')}", file=sys.stderr, flush=True)


# --- BACKENDS: nguồn sinh câu trả lời cho mỗi lượt ------------------------------------------
# Tất cả backend cùng giao diện .complete(system, user_content, output_tag) -> str (text thô).

def _combine_prompt(system: str, user_content: str) -> str:
    """CLI print-mode không có ô 'system' riêng → ghép phân vai + nội dung thành 1 prompt."""
    return f"{system}\n\n=== YÊU CẦU ===\n{user_content}"


def _resolve_exec(argv: list) -> list:
    """Đổi argv[0] sang dạng chạy được.

    Trên Windows, CLI hay là launcher .cmd/.bat (vd Cursor `agent.cmd`) hoặc .ps1 — Python
    subprocess (shell=False) không exec trực tiếp được; phải gọi qua cmd.exe / powershell.
    `shutil.which` tôn trọng PATHEXT nên tìm ra agent.cmd / claude.exe / agy.exe. Trên macOS/
    Linux thì which trả binary thường và nhánh Windows là no-op.
    """
    if not argv:
        return argv
    exe = shutil.which(argv[0])
    if not exe:
        return argv  # để subprocess ném FileNotFoundError với tên gốc (thông điệp rõ hơn)
    low = exe.lower()
    if os.name == "nt":
        if low.endswith((".cmd", ".bat")):
            return ["cmd", "/c", exe] + argv[1:]
        if low.endswith(".ps1"):
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", exe] + argv[1:]
    return [exe] + argv[1:]


# Preset cho từng CLI: argv mẫu ({model} thay bằng --model), cách đẩy prompt, model mặc định.
# Cờ đã đối chiếu `claude --help`, `agent --help` (Cursor CLI), `agy --help` thực tế.
CLI_PRESETS = {
    # `claude -p` = print/non-interactive; auth qua subscription (KHÔNG dùng --bare vì --bare
    # ép dùng ANTHROPIC_API_KEY). Đọc prompt từ stdin. ĐÃ chạy thật thành công.
    "claude": {
        "argv": ["claude", "-p", "--model", "{model}", "--dangerously-skip-permissions"],
        "default_model": "sonnet",
        "prompt_via": "stdin",
    },
    # Cursor CLI — binary tên `agent` (không phải `cursor-agent`). `-p --output-format text` để
    # in non-interactive; `--force`(=--yolo) auto-allow lệnh; `--trust` tin workspace (chỉ chạy
    # với --print/headless). Prompt là positional. Auth qua subscription (`agent login`).
    # Model ví dụ của Cursor: sonnet-4 | sonnet-4-thinking | gpt-5 (None = dùng mặc định account).
    "cursor": {
        "argv": ["agent", "-p", "--output-format", "text", "--model", "{model}",
                 "--force", "--trust"],
        "default_model": None,
        "prompt_via": "arg",
    },
    # `agy --print` (xác nhận cờ qua `agy --help`). CẢNH BÁO [KHÔNG chạy được headless ở đây]:
    # 2 lần probe `agy --print ... "<prompt>"` đều TREO tới khi bị kill (timeout, kể cả khi cho
    # --print-timeout 4m), stdout LẪN stderr rỗng; `agy models` cũng in stdout rỗng → agy cần TTY,
    # không xuất ra stdout khi bị capture. KHÔNG dùng agy cho engine này; dùng claude (đã verify).
    "agy": {
        "argv": ["agy", "--model", "{model}", "--print", "--dangerously-skip-permissions"],
        "default_model": "gemini-3-pro-preview",
        "prompt_via": "arg",
    },
}


class CliBackend:
    """Gọi một CLI agent bản subscription (headless), bắt stdout. KHÔNG cần API key."""

    def __init__(self, backend: str, model: str | None, cmd_template: str | None,
                 prompt_via: str, timeout: int):
        if cmd_template:
            argv_tmpl = shlex.split(cmd_template)
            default_model = None
        elif backend in CLI_PRESETS:
            preset = CLI_PRESETS[backend]
            argv_tmpl = list(preset["argv"])
            default_model = preset["default_model"]
            prompt_via = prompt_via or preset["prompt_via"]
        else:
            raise DebateError(f"backend CLI không rõ '{backend}'; dùng --cmd-template.")
        self.model = model or default_model or ""
        self.argv_tmpl = argv_tmpl
        self.prompt_via = prompt_via or "stdin"
        self.timeout = timeout
        self.label = backend
        self.dry_run = False

    def _argv(self, prompt: str) -> list:
        argv = []
        for tok in self.argv_tmpl:
            if tok == "{model}":
                # Bỏ cặp --model {model} nếu không có model.
                if not self.model:
                    if argv and argv[-1] in ("--model", "-m"):
                        argv.pop()
                    continue
                argv.append(self.model)
            else:
                argv.append(tok)
        if self.prompt_via == "arg":
            argv.append(prompt)
        return argv

    def complete(self, system: str, user_content: str, output_tag: str) -> str:
        prompt = _combine_prompt(system, user_content)
        argv = _resolve_exec(self._argv(prompt))
        stdin_data = prompt if self.prompt_via == "stdin" else None
        try:
            proc = subprocess.run(
                argv, input=stdin_data, capture_output=True, text=True,
                timeout=self.timeout, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            raise DebateError(
                f"không tìm thấy CLI '{argv[0]}' trên PATH. Cài đặt nó, hoặc đổi --backend, "
                f"hoặc truyền --cmd-template \"<lệnh>\".")
        except subprocess.TimeoutExpired:
            raise DebateError(f"CLI '{argv[0]}' quá {self.timeout}s không trả lời.")
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise DebateError(f"CLI '{argv[0]}' exit {proc.returncode}: {tail}")
        out = (proc.stdout or "").strip()
        if not out:
            raise DebateError(f"CLI '{argv[0]}' trả về stdout rỗng.")
        return out


class ApiBackend:
    """Backend tùy chọn: Claude API qua urllib. CHỈ backend này cần ANTHROPIC_API_KEY."""

    def __init__(self, model: str, max_tokens: int, timeout: int):
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.label = "api"
        self.dry_run = False
        self._api_key = config.get("ANTHROPIC_API_KEY")
        self._base_url = config.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL
        if not self._api_key:
            raise DebateError("backend 'api' cần ANTHROPIC_API_KEY. Dùng --backend claude "
                              "(CLI subscription) để khỏi cần key, hoặc --dry-run.")

    def complete(self, system: str, user_content: str, output_tag: str) -> str:
        payload = {
            "model": self.model, "max_tokens": self.max_tokens, "system": system,
            "messages": [{"role": "user", "content": user_content}],
        }
        url = self._base_url.rstrip("/") + "/v1/messages"
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     method="POST")
        req.add_header("content-type", "application/json")
        req.add_header("x-api-key", self._api_key)
        req.add_header("anthropic-version", ANTHROPIC_VERSION)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise DebateError(f"Claude API HTTP {e.code}: {e.read().decode('utf-8','replace')[:400]}")
        except urllib.error.URLError as e:
            raise DebateError(f"lỗi mạng khi gọi Claude API: {e.reason}")
        if data.get("stop_reason") == "refusal":
            raise DebateError(f"model từ chối (refusal): {data.get('stop_details')}")
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()


class DryRunBackend:
    """Mock: trả đúng khối thẻ theo vai (test FSM + parser, không gọi CLI/mạng)."""

    label = "dry-run"
    dry_run = True

    def complete(self, system: str, user_content: str, output_tag: str) -> str:
        return _MOCK_RESPONSES[output_tag]


_MOCK_RESPONSES = {
    "dev_proposal": (
        "<dev_proposal>"
        "<approach>Thêm endpoint GET /api/report/export đọc thẳng từ bảng orders rồi stream CSV."
        "</approach>"
        "<target_files><file>src/main/java/com/x/ReportController.java</file>"
        "<file>src/main/java/com/x/ReportService.java</file></target_files>"
        "<test_strategy>Unit test ReportService.export() với 100 dòng giả lập.</test_strategy>"
        "</dev_proposal>"
    ),
    "architect_critique": (
        "<architect_critique>"
        "<risk category=\"sql_injection\">Query ghép chuỗi từ param 'from/to' -> dùng PreparedStatement."
        "</risk>"
        "<risk category=\"connection_pool\">Stream toàn bảng giữ connection lâu -> dùng cursor/paging."
        "</risk>"
        "<risk category=\"memory_leak\">Load hết vào List trước khi ghi -> stream từng batch."
        "</risk>"
        "<risk category=\"rate_limit\">Endpoint export nặng, chưa có rate limit -> thêm bucket 5 req/phút."
        "</risk>"
        "<risk category=\"cache\">Báo cáo lặp lại -> cache kết quả 5 phút ở Redis.</risk>"
        "<verdict>REVISE</verdict>"
        "</architect_critique>"
    ),
    "dev_rebuttal": (
        "<dev_rebuttal>"
        "<approach>Đồng ý PreparedStatement + paging cursor + stream batch. Rate limit và cache "
        "Redis đưa vào phase sau, giữ MVP gọn nhưng không còn lỗ hổng SQLi/leak.</approach>"
        "<target_files><file>src/main/java/com/x/ReportController.java</file>"
        "<file>src/main/java/com/x/ReportService.java</file>"
        "<file>src/main/java/com/x/ReportRepository.java</file></target_files>"
        "</dev_rebuttal>"
    ),
    "final_specification": (
        "<final_specification>"
        "<approach>GET /api/report/export: PreparedStatement, đọc theo cursor paging, stream CSV "
        "từng batch 1000 dòng; rate limit 5 req/phút; cache Redis TTL 5 phút.</approach>"
        "<target_files><file>src/main/java/com/x/ReportController.java</file>"
        "<file>src/main/java/com/x/ReportService.java</file>"
        "<file>src/main/java/com/x/ReportRepository.java</file></target_files>"
        "<test_strategy>Unit test export() theo batch; integration test SQLi với input độc hại; "
        "test rate-limit trả 429 ở request thứ 6.</test_strategy>"
        "<risks_addressed><risk>sql_injection: PreparedStatement</risk>"
        "<risk>connection_pool+memory_leak: cursor paging + stream batch</risk>"
        "<risk>rate_limit: bucket 5/phút</risk><risk>cache: Redis TTL 5 phút</risk>"
        "</risks_addressed>"
        "</final_specification>"
    ),
}


# --- repo root + nơi lưu spec ----------------------------------------------------------------

def _repo_root() -> str:
    search = _HERE
    for _ in range(6):
        if os.path.isdir(os.path.join(search, ".git")) or \
           os.path.isfile(os.path.join(search, "CLAUDE.md")):
            return search
        search = os.path.dirname(search)
    return _HERE


def _spec_path(task_id: str) -> str:
    safe = "".join(c for c in str(task_id) if c.isalnum() or c in "-_.") or "debate"
    runs = os.path.join(_repo_root(), "temp", "runs")
    os.makedirs(runs, exist_ok=True)
    return os.path.join(runs, f"{safe}_plan.xml")


# --- 2. STATE MACHINE ------------------------------------------------------------------------

DEFAULT_ROUNDS = 2  # số vòng critique↔rebuttal tối đa trước khi Moderator chốt


class DebateEngine:
    def __init__(self, backend, narrator: Narrator, max_rounds: int = DEFAULT_ROUNDS):
        self.backend = backend
        self.narrator = narrator
        self.max_rounds = max(1, max_rounds)
        self.transcript = {}

    def _round(self, system_prompt_key: str, output_tag: str, who: str, title: str,
               user_content: str) -> str:
        self.narrator.banner(who, title)
        # Chốt đúng thẻ kết quả cho LƯỢT NÀY (vai Dev dùng lại cho cả proposal lẫn rebuttal,
        # nên không thể bake cứng tên thẻ trong ROLE_PROMPTS).
        system = (f"{ROLE_PROMPTS[system_prompt_key]}\n\n"
                  f"LƯU Ý LƯỢT NÀY: trả lời CHỈ trong đúng một khối thẻ "
                  f"<{output_tag}>...</{output_tag}>, không dùng thẻ tên khác, không Markdown.")
        raw = self.backend.complete(system, user_content, output_tag)
        content = agent_parser.extract_tag_content(raw, output_tag)
        if content is None:
            # Agent không bọc thẻ đúng — không crash, dùng raw nhưng cảnh báo (reality filter).
            self.narrator.system(
                f"[CẢNH BÁO] không tìm thấy thẻ <{output_tag}> trong output của {who}; dùng nguyên văn.")
            content = raw
        self.transcript[output_tag] = content
        self.narrator.speech(who, content)
        return content

    @staticmethod
    def _is_approve(critique: str) -> bool:
        """Architect đồng ý chốt khi thẻ <verdict> trong critique = APPROVE."""
        verdict = agent_parser.extract_tag_content(critique, "verdict")
        return verdict is not None and verdict.strip().upper().startswith("APPROVE")

    def run(self, task_id: str, task_description: str) -> dict:
        self.narrator.system(
            f"Bắt đầu Agent Debate cho task '{task_id}' "
            f"(backend={self.backend.label}, tối đa {self.max_rounds} vòng).")

        task_xml = f"<task><description>{task_description}</description></task>\n"

        dev_proposal = self._round(
            "agent_dev", "dev_proposal", "dev", "ĐỀ XUẤT BAN ĐẦU",
            task_xml + "Hãy đề xuất giải pháp trong thẻ <dev_proposal>.")

        # Vòng lặp critique↔rebuttal: Architect soi bản hiện tại; nếu APPROVE thì hội tụ sớm,
        # nếu không thì Dev sửa và vòng sau Architect soi chính bản sửa đó (đóng lỗ "rebuttal
        # không ai review"). `current` luôn là phương án mới nhất Dev đưa ra.
        current = dev_proposal
        rounds = []
        converged = False
        for r in range(1, self.max_rounds + 1):
            critique = self._round(
                "agent_architect", "architect_critique", "architect",
                f"PHẢN BIỆN · vòng {r}/{self.max_rounds}",
                task_xml +
                f"<current_solution>{current}</current_solution>\n"
                f"Hãy săm soi và trả về <architect_critique> (kết thúc bằng <verdict>).")
            rounds.append({"round": r, "critique": critique, "rebuttal": None})

            if self._is_approve(critique):
                converged = True
                self.narrator.system(
                    f"Architect APPROVE ở vòng {r} → hội tụ sớm, chuyển sang phán quyết.")
                break

            if r == self.max_rounds:
                self.narrator.system(
                    f"Hết {self.max_rounds} vòng mà chưa APPROVE → Moderator chốt với bất đồng còn lại.")
                break

            rebuttal = self._round(
                "agent_dev", "dev_rebuttal", "dev", f"BẢO VỆ / SỬA ĐỔI · vòng {r}/{self.max_rounds}",
                task_xml +
                f"<your_solution>{current}</your_solution>\n"
                f"<architect_critique>{critique}</architect_critique>\n"
                f"Bào chữa hoặc cập nhật giải pháp, trả về <dev_rebuttal>.")
            rounds[-1]["rebuttal"] = rebuttal
            current = rebuttal

        # Dựng lại toàn bộ transcript nhiều vòng cho Moderator phán quyết.
        debate_parts = [f"<dev_proposal>{dev_proposal}</dev_proposal>"]
        for rd in rounds:
            debate_parts.append(
                f"<round n=\"{rd['round']}\">"
                f"<architect_critique>{rd['critique']}</architect_critique>"
                + (f"<dev_rebuttal>{rd['rebuttal']}</dev_rebuttal>" if rd["rebuttal"] else "")
                + "</round>")
        history = (
            task_xml +
            "<debate>" + "".join(debate_parts) + "</debate>\n"
            "Chốt phương án cuối cùng trong thẻ <final_specification> "
            "(hợp nhất mọi rủi ro hợp lệ Architect đã nêu qua các vòng).")
        final_spec = self._round(
            "agent_moderator", "final_specification", "moderator", "PHÁN QUYẾT", history)

        last = rounds[-1] if rounds else {}
        return {
            "dev_proposal": dev_proposal,
            "architect_critique": last.get("critique"),
            "dev_rebuttal": last.get("rebuttal"),
            "final_specification": final_spec,
            "rounds": rounds,
            "rounds_used": len(rounds),
            "converged": converged,
        }


def _save_spec(task_id: str, final_spec: str) -> str:
    path = _spec_path(task_id)
    body = f"<final_specification>{final_spec}</final_specification>"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body + "\n")
    return path


# --- backend factory + CLI -------------------------------------------------------------------

def _make_backend(args):
    if args.dry_run or args.backend == "dry-run":
        return DryRunBackend()
    if args.backend == "api":
        return ApiBackend(model=args.model or DEFAULT_MODEL,
                          max_tokens=args.max_tokens, timeout=API_TIMEOUT)
    # CLI backends (claude/cursor/agy/custom).
    return CliBackend(backend=args.backend, model=args.model,
                      cmd_template=args.cmd_template, prompt_via=args.prompt_via,
                      timeout=args.cli_timeout)


def cmd_run(args) -> int:
    desc = args.desc
    if args.desc_file:
        with open(args.desc_file, encoding="utf-8") as f:
            desc = f.read().strip()
    if not desc:
        raise DebateError("cần --desc \"...\" hoặc --desc-file <path> (mô tả task).")

    # Recall: fold past human corrections (feedback.py recall → block) into the task the
    # three roles see, so the debate stops repeating mistakes the human already fixed.
    if args.corrections_file:
        try:
            with open(args.corrections_file, encoding="utf-8") as f:
                corrections = f.read().strip()
            if corrections:
                desc = (f"{desc}\n\n"
                        f"LƯU Ý — người dùng từng sửa các plan tương tự thế này; "
                        f"ĐỪNG lặp lại:\n{corrections}")
        except OSError:
            pass  # non-fatal: recall is an optimisation, never blocks the debate

    narrator = Narrator(use_color=not args.no_color)
    backend = _make_backend(args)
    engine = DebateEngine(backend, narrator, max_rounds=args.rounds)

    result = engine.run(args.task, desc)
    spec_path = None
    if not args.no_save:
        spec_path = _save_spec(args.task, result["final_specification"])
        narrator.system(f"Đã lưu spec -> {spec_path}")

    print(json.dumps({
        "ok": True,
        "task_id": args.task,
        "backend": backend.label,
        "spec_path": spec_path,
        "max_rounds": engine.max_rounds,
        "rounds_used": result["rounds_used"],
        "converged": result["converged"],
    }, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Debate Engine cho tầng Plan của /auto-dev.")
    sub = parser.add_subparsers(dest="action", required=True)

    p = sub.add_parser("run", help="Chạy tranh biện nhiều vòng và xuất final_specification")
    p.add_argument("--task", required=True, help="task_id (dùng cho tên file spec)")
    p.add_argument("--desc", help="Mô tả task")
    p.add_argument("--desc-file", help="Đọc mô tả task từ file")
    p.add_argument("--corrections-file", help="Khối <past_corrections> từ `feedback.py recall` "
                                              "(chèn bài học cũ vào prompt tranh biện)")
    p.add_argument("--backend", default="claude",
                   choices=["claude", "cursor", "agy", "custom", "api", "dry-run"],
                   help="Nguồn agent (mặc định: claude CLI subscription, không cần API key)")
    p.add_argument("--cmd-template", help="Lệnh CLI tùy biến (dùng với --backend custom); "
                                          "có thể chứa {model}")
    p.add_argument("--prompt-via", default="", choices=["", "stdin", "arg"],
                   help="Đẩy prompt vào CLI qua stdin (mặc định) hay tham số cuối")
    p.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                   help=f"Số vòng critique↔rebuttal tối đa (mặc định {DEFAULT_ROUNDS}; "
                        f"Architect APPROVE sẽ hội tụ sớm). --rounds 1 = hành vi 1 lượt cũ.")
    p.add_argument("--model", default=config.get("ANTHROPIC_MODEL") or "",
                   help="Model truyền cho CLI/api (rỗng = mặc định của backend)")
    p.add_argument("--cli-timeout", type=int, default=CLI_TIMEOUT)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="(backend api)")
    p.add_argument("--dry-run", action="store_true", help="Mock, không gọi CLI/API (test FSM)")
    p.add_argument("--no-color", action="store_true", help="Tắt màu ANSI")
    p.add_argument("--no-save", action="store_true", help="Không ghi file spec")

    args = parser.parse_args()
    try:
        if args.action == "run":
            return cmd_run(args)
    except DebateError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
