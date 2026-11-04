"""Line rules of the security standard (security-audit/cookbook/standard.md — same ids).

A rule matches ONE line (regex) of files selected by glob; `unless` (regex) within the matched line and
the next 15 code lines cancels it (e.g. an XML parser factory hardened right after it is created). Severities:
high (exploitable / secret exposed) · medium (likely weakness, needs a fix or a reason) · low (hygiene) ·
info (inventory for a human / the agent audit — never fails a scan).

Offline only: nothing here talks to the network.
"""

import re

JAVA = ("*.java", "*.kt")
WEB = ("*.js", "*.jsx", "*.ts", "*.tsx", "*.vue", "*.html")
CONFIG = ("*.yml", "*.yaml", "*.properties", "*.env", ".env*", "*.json", "*.xml", "*.conf", "*.ini", "*.toml")
PROD_CONFIG = ("application.yml", "application.yaml", "application.properties", "application-prod*.yml",
               "application-prod*.yaml", "application-prod*.properties", "bootstrap-prod*.yml", "bootstrap.yml")
ANY = ("*",)


class Rule:
    def __init__(self, rid, severity, category, title, pattern, files, fix, *, unless=None, flags=re.I,
                 main_only=False, secret=False):
        self.id, self.severity, self.category, self.title, self.fix = rid, severity, category, title, fix
        self.pattern = re.compile(pattern, flags)
        self.files = files
        self.unless_src = unless or ""      # may contain {var}: the receiver captured by group "var" of pattern
        self.unless = re.compile(unless, re.I) if unless and "{var}" not in unless else None
        self.main_only = main_only          # skip test sources / fixtures
        self.secret = secret                # the matched text is a credential: always masked in reports

    def as_dict(self) -> dict:
        return {"id": self.id, "severity": self.severity, "category": self.category, "title": self.title,
                "fix": self.fix, "files": list(self.files)}


SECRET_KEYS = r"(?:password|passwd|pwd|secret|token|api[-_.]?key|access[-_.]?key|private[-_.]?key|" \
              r"base64[-_.]?secret|client[-_.]?secret|credentials?)"

RULES = [
    # ------------------------------------------------------------------ secrets
    Rule("SEC-001", "high", "secret", "Private key trong repo",
         r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----", ANY,
         "Xoá key khỏi repo VÀ lịch sử git, thu hồi/đổi key, nạp từ secret store / biến môi trường.", secret=True),
    Rule("SEC-002", "high", "secret", "Token dịch vụ dạng đã biết (GitLab/GitHub/AWS/Slack/Google/OpenAI/JWT…)",
         r"(?<![\w-])(?:glpat-[\w-]{20,}|gh[pousr]_[A-Za-z0-9]{36,}|AKIA[0-9A-Z]{16}|xox[abprs]-[\w-]{10,}|"
         r"AIza[\w-]{35}|sk-(?:proj-)?[A-Za-z0-9_-]{32,}|eyJ[\w-]{15,}\.eyJ[\w-]{15,}\.[\w-]{15,})", ANY,
         "Thu hồi token ngay (đã lộ), xoá khỏi code + lịch sử, đọc từ biến môi trường / vault.",
         flags=0, secret=True),
    Rule("SEC-004", "medium", "secret", "Secret gán cứng trong code",
         r"\b\w*(?:[Pp]assword|PASSWORD|[Pp]asswd|[Ss]ecret|SECRET|[Tt]oken|TOKEN|[Aa]pi[Kk]ey|API_KEY|"
         r"[Pp]rivate[Kk]ey|PRIVATE_KEY)\s*=\s*\"(?!(?:change(?:me|it)|your[-_]|example|dummy|xxx|\*+|\$\{))"
         r"[^\"\s]{6,}\"", JAVA + WEB,
         "Đọc từ cấu hình/biến môi trường; không để giá trị thật trong code.", flags=0, main_only=True,
         secret=True),
    Rule("SEC-005", "high", "secret", "Hash mật khẩu mặc định của JHipster (admin/admin, user/user)",
         r"\$2a\$10\$(?:gSAhZrxMllrbgj/kkK9UceBPpChGWJA7SYIb1Mqo\.n5aNLq1/oRrC|"
         r"VEjxo0jq2YG9Rbk2HmX9S\.k1uZBGYUHdUcid3g/vfiEl7lwWgOH/K)", ANY,
         "Đổi mật khẩu tài khoản mặc định / xoá user mẫu khỏi changelog liquibase chạy ở prod.", flags=0),

    # ------------------------------------------------------------------ generated / leftover code
    Rule("GEN-001", "medium", "leftover", "Controller test/demo/debug nằm trong code chạy thật",
         r"\bclass\s+\w*?(?:Test|Demo|Debug|Sample|Dummy|Mock|Playground|Scratch)(?=[A-Z])\w*"
         r"(?:Resource|Controller|Endpoint|Api)\b", JAVA,
         "Xoá class, hoặc chuyển vào src/test / profile dev.", flags=0, main_only=True),
    Rule("GEN-002", "low", "leftover", "printStackTrace / System.out trong code chạy thật",
         r"\.printStackTrace\(\s*\)|System\.(?:out|err)\.print", JAVA,
         "Dùng logger (không in stacktrace ra client / stdout).", flags=0, main_only=True),
    Rule("GEN-003", "medium", "leftover", "TODO/FIXME liên quan bảo mật còn sót",
         r"(?://|#|/\*|\*|<!--)\s*(?:TODO|FIXME|XXX|HACK)\b.*(?:\bauth\w*|\bpermission|\bquyền\b|\bsecurity\b|"
         r"bảo mật|\bbypass|xác thực|\bjwt\b|\bcsrf\b|\bxss\b|\bsanitiz|\bencrypt|\bsecret|\bpassword|\bacl\b|"
         r"\bidor\b|\bbola\b)", JAVA + WEB + CONFIG,
         "Hoàn tất hoặc xoá; ghi ticket nếu còn nợ."),
    Rule("GEN-005", "medium", "leftover", "Endpoint hoặc cờ 'test/debug' bật trong code chạy thật",
         r"@(?:Get|Post|Put|Delete|Patch|Request)Mapping\(\s*(?:value\s*=\s*|path\s*=\s*)?\"[^\"]*/"
         r"(?:test|debug|demo|tmp|temp|dev|internal-test)(?:/|\")", JAVA,
         "Xoá endpoint thử nghiệm hoặc giới hạn cho ADMIN + profile dev.", main_only=True),

    # ------------------------------------------------------------------ configuration
    Rule("CFG-005", "medium", "config", "CORS cho phép mọi origin",
         r"allowedOrigins\(\s*\"\*\"|addAllowedOrigin\(\s*\"\*\"|allowedOriginPatterns\(\s*\"\*\"",
         JAVA, "Liệt kê origin cụ thể; không dùng * cùng allow-credentials."),

    # ------------------------------------------------------------------ injection / dangerous APIs (Java)
    Rule("INJ-001", "medium", "injection", "Câu SQL/JPQL nối chuỗi với biến",
         r"(?:create(?:Native)?Query|jdbcTemplate\.\w+|prepareStatement|executeQuery|executeUpdate|\.execute)\("
         r"\s*(?:\"[^\"]*\"\s*\+\s*(?![A-Z][A-Z0-9_]*\b)(?!\")|[a-z]\w*\s*\+\s*\")", JAVA,
         "Dùng tham số bind (:name / ?) hoặc Criteria/QueryDSL; tên bảng/cột động phải lấy từ whitelist.",
         flags=0, main_only=True),
    Rule("INJ-004", "high", "injection", "Câu SQL/JPQL nối chuỗi với giá trị giống input người dùng",
         r"(?:create(?:Native)?Query|jdbcTemplate\.\w+|prepareStatement|executeQuery|executeUpdate|\.execute)\("
         r"[^;]*\+\s*(?:\w+\.get)?(?:\w*[Ss]earch|\w*[Kk]eyword|\w*[Ff]ilter|[Ss]ort\w*|[Oo]rder[Bb]y|\w*[Qq]uery\b|"
         r"\w*[Pp]aram\w*|request\.\w+|\w*[Tt]erm\b|\w*[Tt]ext\b|\w*[Ii]nput\w*)", JAVA,
         "Không bao giờ nối input vào SQL: bind tham số; sort/order theo whitelist cột.", flags=0, main_only=True),
    Rule("INJ-002", "medium", "injection", "Chạy lệnh hệ điều hành",
         r"Runtime\.getRuntime\(\)\.exec\(|new\s+ProcessBuilder\(", JAVA,
         "Tránh nếu có thể; nếu cần: whitelist lệnh, truyền mảng tham số, không ghép input.", flags=0,
         main_only=True),
    Rule("INJ-003", "medium", "injection", "SpEL / script động từ chuỗi",
         r"\.parseExpression\(|ScriptEngineManager\(|GroovyShell\(", JAVA,
         "Không đánh giá biểu thức từ dữ liệu người dùng; dùng SimpleEvaluationContext.", flags=0, main_only=True),
    Rule("PATH-001", "medium", "path", "Đường dẫn file ghép từ dữ liệu vào",
         r"new\s+File\(\s*[^\")]*\+|Paths\.get\([^)]*(?:request|param|name|fileName|filename|path)\w*\s*[,)]|"
         r"Path\.of\([^)]*(?:request|param|fileName|filename)", JAVA,
         "Chuẩn hoá + kiểm đường dẫn nằm trong thư mục cho phép (normalize().startsWith(base)).",
         main_only=True),
    Rule("SSRF-001", "low", "ssrf", "Gọi URL lấy từ biến (khả năng SSRF)",
         r"new\s+URL\(\s*[a-z]\w*\s*\)|(?:restTemplate|webClient|httpClient)\.\w+\(\s*[a-z]\w*(?:Url|URL|Uri|URI)\b",
         JAVA, "Chỉ gọi host trong whitelist; không cho người dùng chỉ định URL đích.", main_only=True),
    Rule("XXE-001", "medium", "injection", "XML parser chưa tắt DTD/entity ngoài",
         r"(?:\b(?P<var>\w+)\s*=\s*)?"
         r"(?:DocumentBuilderFactory|SAXParserFactory|XMLInputFactory|TransformerFactory|SchemaFactory)"
         r"\.newInstance\(", JAVA,
         "Bật FEATURE_SECURE_PROCESSING, disallow-doctype-decl, tắt external entities.",
         # protective setting on THAT factory variable with the protective value (Codex security R2 #5)
         unless=r"\b{var}\.setFeature\([^,;]*(?:FEATURE_SECURE_PROCESSING|disallow-doctype-decl)[^,;]*,\s*"
                r"(?:true|Boolean\.TRUE)\s*\)|"
                r"\b{var}\.setAttribute\([^,;]*ACCESS_EXTERNAL_(?:DTD|SCHEMA|STYLESHEET)[^,;]*,\s*\"\"\s*\)|"
                r"\b{var}\.setProperty\([^,;]*(?:SUPPORT_DTD|IS_SUPPORTING_EXTERNAL_ENTITIES)[^,;]*,\s*"
                r"(?:false|Boolean\.FALSE)\s*\)",
         flags=0, main_only=True),
    Rule("DESER-001", "high", "deserialization", "Deserialize kiểu Java / Jackson default typing",
         r"new\s+ObjectInputStream\(|enableDefaultTyping\(|activateDefaultTyping\(|@JsonTypeInfo\(\s*use\s*=\s*"
         r"JsonTypeInfo\.Id\.CLASS", JAVA, "Không deserialize dữ liệu không tin cậy theo kiểu lớp; dùng DTO cố định.",
         flags=0, main_only=True),
    Rule("CRYPTO-001", "medium", "crypto", "Thuật toán băm/mã hoá yếu",
         r"MessageDigest\.getInstance\(\s*\"(?:MD5|SHA-?1)\"|Cipher\.getInstance\(\s*\"(?:DES|RC4|[^\"]*/ECB/)",
         JAVA, "Dùng SHA-256+/bcrypt cho mật khẩu, AES/GCM cho mã hoá.", main_only=True),
    Rule("CRYPTO-002", "low", "crypto", "java.util.Random (không an toàn cho token/mật khẩu)",
         r"new\s+Random\(|Math\.random\(", JAVA, "Dùng SecureRandom cho mọi giá trị bí mật.", flags=0,
         main_only=True),
    Rule("TLS-001", "high", "crypto", "Bỏ qua kiểm chứng chỉ / hostname trong code",
         r"NoopHostnameVerifier|TrustAllStrategy|TrustSelfSignedStrategy|setHostnameVerifier\([^)]*->\s*true|"
         r"ALLOW_ALL_HOSTNAME_VERIFIER|InsecureTrustManagerFactory", JAVA,
         "Dùng truststore có CA nội bộ thay vì tắt kiểm chứng.", flags=0, main_only=True),
    Rule("AUTH-001", "medium", "authn", "@CrossOrigin mở cho mọi nguồn",
         r"@CrossOrigin(?:\(\s*\)|\s*$|\(\s*(?:origins\s*=\s*)?\"\*\")", JAVA,
         "Bỏ @CrossOrigin, cấu hình CORS tập trung với origin cụ thể.", flags=0, main_only=True),
    Rule("AUTH-002", "info", "authn", "CSRF tắt (bình thường với API JWT stateless — xác nhận không dùng cookie session)",
         r"csrf\(\)\s*\.disable\(\)|csrf\(\s*\w+\s*->\s*\w+\.disable\(\)\)|AbstractHttpConfigurer::disable",
         JAVA, "Nếu xác thực bằng cookie: bật CSRF.", flags=0, main_only=True),
    Rule("LOG-001", "medium", "logging", "Ghi log giá trị nhạy cảm",
         r"\blog(?:ger)?\.(?:trace|debug|info|warn|error)\(\s*\"[^\"]*\"\s*,[^;]*?\b\w*(?:[Pp]assword|PASSWORD|"
         r"[Ss]ecret|SECRET|[Tt]oken|TOKEN|[Oo]tp\b|OTP\b|[Aa]uthorization|[Cc]ookie)"
         r"(?!\w*(?:Uri|Url|URI|URL|Name|Type|Prefix|Id|Count|Length|Header|Enabled|Configured|Expir\w*|Hash))"
         r"\w*\b(?!\s*(?:!=|==|\.isEmpty|\.length|\.substring|\.isBlank|\?)|\(\))", JAVA,
         "Không log secret/token; mask trước khi log.", flags=0, main_only=True),

    # ------------------------------------------------------------------ web front-end
    Rule("XSS-001", "medium", "xss", "Chèn HTML thô (XSS)",
         r"\.innerHTML\s*=|dangerouslySetInnerHTML|\bv-html\b|bypassSecurityTrust\w*\(|\[innerHTML\]", WEB,
         "Hiển thị text, hoặc sanitize (DOMPurify / DomSanitizer) trước khi chèn.", main_only=True),
    Rule("JS-001", "medium", "injection", "eval / new Function",
         r"\beval\(|new\s+Function\(", WEB, "Không đánh giá chuỗi thành code.", flags=0, main_only=True),
    Rule("JS-002", "low", "authn", "Token lưu trong localStorage",
         r"localStorage\.setItem\(\s*['\"][^'\"]*(?:token|jwt|auth)", WEB,
         "Cân nhắc cookie HttpOnly; nếu buộc dùng storage thì chống XSS chặt.", main_only=True),
]
BY_ID = {r.id: r for r in RULES}

class KeyRule:
    """A rule on a CONFIG KEY: YAML / .properties files are flattened to `a.b.c = value` (scan.py), the
    full dotted key must match `key` and the value `value`. `prod_only`: only application(-prod).*,
    bootstrap(-prod).* — the files that reach production."""

    def __init__(self, rid, severity, category, title, key, value, fix, *, prod_only=False, secret=False):
        self.id, self.severity, self.category, self.title, self.fix = rid, severity, category, title, fix
        self.key, self.value = re.compile(key, re.I), re.compile(value, re.I)
        self.prod_only, self.secret = prod_only, secret
        self.files = ("application*.yml", "application*.yaml", "application*.properties", "bootstrap*.yml") \
            if prod_only else CONFIG

    def as_dict(self) -> dict:
        return {"id": self.id, "severity": self.severity, "category": self.category, "title": self.title,
                "fix": self.fix, "files": list(self.files)}


# a value that is NOT a real secret: empty, a placeholder / env reference, an obvious dummy
NOT_A_SECRET = re.compile(r"^\s*$|^\$\{|^<.*>$|^(?:null|none|~|changeit|xxx+|\*+|your[-_].*|example.*|dummy)$|"
                          r"^ENC\(", re.I)
# a dependency version range — only exempt inside dependency manifests (package.json…), never a password
VERSION_RANGE = re.compile(r"^[\^~>=<]*\d+(?:\.[\dx*]+)+(?:[-+][\w.]+)?$", re.I)

KEY_RULES = [
    KeyRule("SEC-003", "high", "secret", "Mật khẩu / secret ghi thẳng trong file cấu hình",
            r"(?:^|[._-])" + SECRET_KEYS + r"$", r".{4,}",
            "Thay bằng ${ENV_VAR} / Spring Cloud Config / vault; đổi giá trị đã commit vì đã lộ.", secret=True),
    KeyRule("GEN-004", "medium", "leftover", "Liquibase context faker / dữ liệu mẫu ở cấu hình chạy thật",
            r"liquibase\.contexts?$", r"\bfaker\b",
            "Không bật context faker ở prod; xoá changelog dữ liệu giả.", prod_only=True),
    KeyRule("CFG-001", "high", "config", "Actuator mở mọi endpoint",
            r"management\.endpoints\.web\.exposure\.include$", r"^\s*['\"]?\*", "Chỉ mở health,info(,prometheus) "
            "và bảo vệ /management/** bằng ROLE_ADMIN.", prod_only=True),
    KeyRule("CFG-002", "high", "config", "H2 console bật",
            r"h2\.console\.enabled$", r"^\s*true\b", "Tắt h2 console ở mọi profile ngoài dev.", prod_only=True),
    KeyRule("CFG-003", "medium", "config", "Swagger / API docs bật ở cấu hình chạy thật",
            r"(?:springdoc\.(?:api-docs|swagger-ui)|jhipster\.api-docs|springfox[\w.]*)\.enabled$|"
            r"spring\.profiles\.(?:active|include)$", r"^\s*true\b|\bapi-docs\b|\bswagger\b",
            "Tắt api-docs ở prod, hoặc bảo vệ bằng ROLE_ADMIN.", prod_only=True),
    KeyRule("CFG-004", "low", "config", "show-sql bật ở cấu hình chạy thật",
            r"(?:jpa\.)?show[-_]sql$", r"^\s*true\b", "Tắt show-sql ở prod (lộ dữ liệu vào log).", prod_only=True),
    KeyRule("CFG-006", "low", "config", "Log DEBUG/TRACE ở cấu hình chạy thật",
            r"^logging\.level(?:\.[\w.-]+)?$", r"^\s*['\"]?(?:DEBUG|TRACE)\b",
            "Dùng INFO/WARN ở prod (log DEBUG thường chứa dữ liệu nhạy cảm).", prod_only=True),
    KeyRule("CFG-007", "medium", "config", "Tắt kiểm chứng chỉ TLS",
            r"(?:verify[-_]?ssl|ssl[-_]?verify|verify[-_]?certs?)$",
            r"^\s*['\"]?false\b", "Bật kiểm chứng; dùng truststore nội bộ cho CA riêng."),
    KeyRule("CFG-009", "medium", "config", "Cờ 'insecure' bật: bỏ kiểm chứng chỉ TLS",
            r"(?:insecure[-_]?skip[-_]?tls[-_]?verify|insecure[-_]?ssl|tls[-_]?insecure|(?:^|\.)insecure)$",
            r"^\s*['\"]?true\b", "Tắt cờ insecure; dùng truststore nội bộ cho CA riêng."),
    KeyRule("CFG-008", "medium", "config", "CORS cho phép mọi origin",
            r"cors\.allowed[-_]?origins?(?:-patterns)?$", r"^\s*['\"]?\*",
            "Liệt kê origin cụ thể; không dùng * cùng allow-credentials."),
]
KEY_BY_ID = {r.id: r for r in KEY_RULES}


# rules applied to lines ADDED in git history (credentials survive deletion in history)
HISTORY_RULES = ("SEC-001", "SEC-002", "SEC-005")   # + the SEC-003 key rule on added config lines

# endpoint-level findings produced by the Spring analyzer (spring.py) — listed here for the catalog
ENDPOINT_RULES = [
    {"id": "AUTHZ-001", "severity": "medium", "category": "authz",
     "title": "Endpoint công khai (permitAll) — xác nhận có chủ đích và có cơ chế xác thực khác",
     "fix": "Nếu không cần công khai: bỏ khỏi permitAll; nếu cần: kiểm token/chữ ký riêng, rate-limit."},
    {"id": "AUTHZ-002", "severity": "high", "category": "authz",
     "title": "Endpoint không khớp rule bảo vệ nào trong SecurityConfiguration",
     "fix": "Thêm rule authenticated()/hasAuthority cho đường dẫn, hoặc rule chặn cuối anyRequest().authenticated()."},
    {"id": "AUTHZ-003", "severity": "info", "category": "authz",
     "title": "Endpoint nhận id đối tượng, chỉ yêu cầu đăng nhập — cần kiểm quyền sở hữu (BOLA/IDOR)",
     "fix": "Kiểm người gọi có quyền trên đúng đối tượng (thuộc project/tenant/người tạo) trước khi đọc/ghi."},
    {"id": "AUTHZ-004", "severity": "medium", "category": "authz",
     "title": "Endpoint ghi/xoá (POST/PUT/PATCH/DELETE) công khai",
     "fix": "Endpoint thay đổi dữ liệu không được permitAll trừ khi có xác thực riêng (webhook có chữ ký…)."},
]
