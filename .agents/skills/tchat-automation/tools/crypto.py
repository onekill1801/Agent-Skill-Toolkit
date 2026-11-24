#!/usr/bin/env python3
"""TChat — giải mã tin nhắn E2E (beatchat) bằng RSA private key của bạn.

Scheme (đã reverse-engineer & xác minh từ web client beatchat):
  • Nội dung tin: base64. Nếu dài = kích thước modulus (256B với RSA-2048) →
    RSA-OAEP(SHA-256) decrypt trực tiếp ra UTF-8. Nếu DÀI HƠN (tin lớn) → 256B đầu
    RSA-OAEP ra payload [aesKey:32][iv:12][extra], phần sau + extra = AES-GCM ciphertext.
  • Private key của bạn được gói 2 lớp:
    - Trong IndexedDB `user_keys[<user>_private]`: AES-GCM, password = `clientKey`
      (/user/me). Plaintext = base64(pkcs8 DER).
    - Trong blob QR-login (`a.privateKey`): bỏ 21 byte đầu, rồi AES-GCM password =
      secretkey bạn nhập trên UI.
  • Lớp AES-GCM (`ci`): blob = [iv:12][ciphertext‖tag][salt:16]; key =
    PBKDF2-HMAC-SHA256(password, salt, 300000 vòng, 32 byte).

⚠️ Đây là dữ liệu riêng tư của chính bạn, giải mã bằng key của chính bạn (được ủy
quyền). Private key/PEM lưu ở work/secrets/ (gitignored) — KHÔNG commit, không log.

Dùng:
  # 1) Mở private key từ giá trị IndexedDB (dán từ Console trình duyệt) → lưu PEM
  python crypto.py unwrap-indexeddb --value '<base64 hoặc {"value":...}>' [--client-key K] --save
  # 2) Hoặc mở từ blob QR-login + secretkey
  python crypto.py unwrap-qr --blob '<base64>' --secretkey '<secretkey>' --save
  # 3) Giải mã một content đơn lẻ
  python crypto.py decrypt --content '<base64 ciphertext>'
  # 4) Giải mã lịch sử một hội thoại (tự nhận diện tin mã hoá)
  python crypto.py conv <group_id> [--limit N]
"""

import argparse
import base64
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client   # noqa: E402
import config   # noqa: E402

from cryptography.hazmat.primitives import hashes, serialization          # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding             # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM            # noqa: E402
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC          # noqa: E402

PBKDF2_ITERS = 300_000
_B64_RE = re.compile(r"^[A-Za-z0-9+/=]+$")


def _repo_root() -> str:
    search = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if os.path.isfile(os.path.join(search, ".env")) or os.path.isdir(os.path.join(search, ".git")):
            return search
        parent = os.path.dirname(search)
        if parent == search:
            break
        search = parent
    return os.getcwd()


def _key_path() -> str:
    p = config.get("FCHAT_PRIVATE_KEY_PATH") if hasattr(config, "get") else ""
    return p or os.path.join(_repo_root(), "work", "secrets", "fchat_private.pem")


# ── AES-GCM layer (ci): [iv12][ct+tag][salt16], key = PBKDF2(password, salt) ──
def _derive(password: bytes, salt: bytes) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                      iterations=PBKDF2_ITERS).derive(password)


def aes_gcm_open(blob: bytes, password: bytes) -> bytes:
    iv, ct, salt = blob[:12], blob[12:-16], blob[-16:]
    return AESGCM(_derive(password, salt)).decrypt(iv, ct, None)


def _inner_to_private_key(inner: bytes):
    """inner (plaintext sau AES-GCM) → private key. App lưu base64(pkcs8 DER),
    nhưng chấp nhận cả PEM/DER cho chắc."""
    s = inner.decode("utf-8", "replace").strip()
    body = re.sub(r"-----[^-]+-----", "", s)
    body = re.sub(r"\s+", "", body)
    for loader, arg in ((serialization.load_der_private_key, base64.b64decode(body + "===")),):
        try:
            return loader(arg, password=None)
        except Exception:  # noqa: BLE001
            pass
    # thử PEM nguyên bản
    return serialization.load_pem_private_key(s.encode(), password=None)


def unwrap_indexeddb(value_b64: str, client_key: str):
    blob = base64.b64decode(value_b64 + "===")
    return _inner_to_private_key(aes_gcm_open(blob, client_key.encode()))


def unwrap_qr(qr_blob_b64: str, secretkey: str):
    o = base64.b64decode(qr_blob_b64 + "===")[21:]     # G2: bỏ 21 byte header
    return _inner_to_private_key(aes_gcm_open(o, secretkey.encode()))


# ── message content (Mt): RSA-OAEP-SHA256, hoặc hybrid RSA+AES-GCM ──
def _rsa_oaep(priv, data: bytes) -> bytes:
    return priv.decrypt(data, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                           algorithm=hashes.SHA256(), label=None))


def decrypt_content(content_b64: str, priv) -> str:
    data = base64.b64decode(content_b64 + "===")
    ksize = priv.key_size // 8
    if len(data) == ksize:                              # R2: RSA-OAEP trực tiếp
        return _rsa_oaep(priv, data).decode("utf-8", "replace")
    payload = _rsa_oaep(priv, data[:ksize])             # O2/N2: hybrid
    aeskey, iv, extra = payload[:32], payload[32:44], payload[44:]
    return AESGCM(aeskey).decrypt(iv, data[ksize:] + extra, None).decode("utf-8", "replace")


def looks_encrypted(text) -> bool:
    """Ciphertext beatchat: base64 thuần, không khoảng trắng, độ dài bội số 4 và
    >= 344 (256B). Plaintext thường có dấu cách/độ dài lẻ."""
    if not isinstance(text, str) or len(text) < 344 or " " in text:
        return False
    return bool(_B64_RE.match(text)) and len(text) % 4 == 0


# ── key persistence ────────────────────────────────────────────────
def save_key(priv, path=None) -> str:
    path = path or _key_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pem = priv.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    with open(path, "wb") as f:
        f.write(pem)
    return path


def load_key(path=None):
    path = path or _key_path()
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def maybe_decrypt(content, priv) -> str:
    """Giải mã nếu trông là ciphertext & có key; lỗi/không phải → trả nguyên văn."""
    if not priv or not looks_encrypted(content):
        return content
    try:
        return decrypt_content(content, priv)
    except Exception:  # noqa: BLE001
        return content


# Cache private key toàn tiến trình để các luồng (messages/listen/group_watch) gọi
# decrypt_if_needed() rẻ, không load PEM mỗi tin. Không có key → trả nguyên văn.
_KEY = None
_KEY_TRIED = False


def cached_key():
    global _KEY, _KEY_TRIED
    if not _KEY_TRIED:
        _KEY_TRIED = True
        try:
            _KEY = load_key()
        except Exception:  # noqa: BLE001
            _KEY = None
    return _KEY


def decrypt_if_needed(content) -> str:
    return maybe_decrypt(content, cached_key())


def _client_key(explicit=None) -> str:
    if explicit:
        return explicit
    ck = config.get("FCHAT_CLIENT_KEY") if hasattr(config, "get") else ""
    if ck:
        return ck
    r = client.api_get("/user/me")
    return (r or {}).get("clientKey") or ""


def _extract_value(raw: str) -> str:
    """Chấp nhận base64 thuần, hoặc JSON {"value":"..."} từ Console."""
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            return json.loads(raw).get("value") or raw
        except ValueError:
            pass
    return raw


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="crypto.py")
    sub = p.add_subparsers(dest="cmd")

    u1 = sub.add_parser("unwrap-indexeddb")
    u1.add_argument("--value", help="giá trị IndexedDB (base64 hoặc JSON record); '-' = đọc stdin")
    u1.add_argument("--client-key", default=None)
    u1.add_argument("--save", action="store_true")

    u2 = sub.add_parser("unwrap-qr")
    u2.add_argument("--blob", required=True)
    u2.add_argument("--secretkey", required=True)
    u2.add_argument("--save", action="store_true")

    d = sub.add_parser("decrypt")
    d.add_argument("--content", required=True)
    d.add_argument("--key-file", default=None)

    c = sub.add_parser("conv")
    c.add_argument("group_id")
    c.add_argument("--limit", type=int, default=20)
    c.add_argument("--key-file", default=None)

    a = p.parse_args()
    try:
        if a.cmd == "unwrap-indexeddb":
            raw = sys.stdin.read() if a.value in (None, "-") else a.value
            priv = unwrap_indexeddb(_extract_value(raw), _client_key(a.client_key))
            print(json.dumps({"ok": True, "key_size": priv.key_size,
                              "saved": save_key(priv) if a.save else None}))
        elif a.cmd == "unwrap-qr":
            priv = unwrap_qr(a.blob, a.secretkey)
            print(json.dumps({"ok": True, "key_size": priv.key_size,
                              "saved": save_key(priv) if a.save else None}))
        elif a.cmd == "decrypt":
            priv = load_key(a.key_file)
            if not priv:
                print(json.dumps({"error": True, "message": f"chưa có key ({_key_path()}) — chạy unwrap-* --save trước"})); sys.exit(1)
            print(decrypt_content(a.content, priv))
        elif a.cmd == "conv":
            priv = load_key(a.key_file)
            if not priv:
                print(json.dumps({"error": True, "message": f"chưa có key ({_key_path()})"})); sys.exit(1)
            r = client.api_get(f"/message-query/group/{a.group_id}/message", {"limit": a.limit})
            me = (client.api_get("/user/me") or {}).get("id")
            for m in sorted(r.get("regulars") or [], key=lambda x: x.get("messageIdInc") or 0):
                if m.get("type") != "TEXT":
                    continue
                who = "Tôi" if m.get("senderId") == me else ((m.get("user") or {}).get("displayName") or "?")
                print(f"[{who}] {maybe_decrypt(m.get('content') or '', priv)}")
        else:
            print(__doc__)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": True, "message": f"{type(e).__name__}: {e}"}))
        sys.exit(1)
