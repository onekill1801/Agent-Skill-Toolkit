# SSH fan-out tới máy LAN

## Registry — work/hosts.json
Chỉ host trong file này mới gọi được (allowlist). Mẫu: `hosts.sample.json` trong skill.

```json
{
  "may-build": { "host": "192.168.1.20", "user": "chung", "port": 22 },
  "nas":       { "host": "192.168.1.30", "user": "admin", "key": "~/.ssh/id_ed25519" },
  "win-test":  { "host": "192.168.1.40", "user": "chung", "shell": "powershell" }
}
```
- `host` bắt buộc. `user/port/key` tùy chọn (`port` mặc định 22, `key` để chỉ định private key cụ thể).
- File sống ở `work/` (đã gitignore) → không commit thông tin máy nội bộ.

## Auth — key-based (đã có, theo xác nhận)
Tool gọi `ssh` hệ thống với `-o BatchMode=yes` → **không** rơi vào prompt mật khẩu (host chưa setup key
sẽ lỗi ngay thay vì treo). `StrictHostKeyChecking=accept-new` tự thêm host key lần đầu.

## Dùng
```
cd .claude/skills/remote-control/tools
python ssh_exec.py list                          # liệt kê hosts
python ssh_exec.py ping may-build                # echo ok qua SSH (kiểm kết nối)
python ssh_exec.py run may-build "df -h"          # chạy (read → tự do)
python ssh_exec.py run may-build "systemctl restart app" --dry-run   # xem argv, không chạy
python ssh_exec.py run may-build "systemctl restart app"             # write → cần duyệt nếu qua bridge
python ssh_exec.py classify "rm -rf /var/log"     # kiểm phân loại rủi ro
```
Trả JSON: `{host, address, command, risk, returncode, stdout, stderr}` (hoặc `{error, message}`).

## Qua Telegram
Nhắn tự nhiên, agent tự gọi `ssh_exec.py`:
- *"ssh may-build chạy df -h"* → read, chạy luôn, trả kết quả.
- *"restart service app trên may-build"* → write → thẻ ✅/❌ hiện trên Telegram, chờ bạn bấm.
- *"xóa thư mục /tmp/cache trên nas"* → nếu khớp pattern danger → ⚠️ + chờ duyệt.

## Windows làm target
Bật **OpenSSH Server** trên máy đích (Settings → Optional features → OpenSSH Server, rồi
`Start-Service sshd`). Lệnh gửi tới chạy qua shell mặc định của sshd (thường `cmd`); để dùng
PowerShell, gửi `powershell -Command "<...>"` hoặc đặt default shell của sshd sang PowerShell.

## Mở rộng
- Chạy 1 lệnh trên NHIỀU host: hiện chạy tuần tự từng alias. Cần song song thì gọi `ssh_exec.run`
  trong vòng lặp/thread, hoặc nhờ agent fan-out — nhưng mỗi host vẫn qua cùng cơ chế phân loại + duyệt.
