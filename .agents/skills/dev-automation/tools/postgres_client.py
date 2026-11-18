#!/usr/bin/env python3
"""Minimal PostgreSQL client over a raw socket — Python stdlib only.

Speaks the PostgreSQL frontend/backend protocol v3 well enough to authenticate and
run a simple text query, so `probe_db.py --engine postgres` works WITHOUT the `psql`
CLI — the same zero-dependency stance as probe_redis (RESP) / probe_kafka (HTTP) /
mysql_client (MySQL wire).

Auth supported (stdlib hashlib/hmac only):
  - SCRAM-SHA-256  (the modern PostgreSQL 10+ default)
  - MD5
  - Cleartext password
A server demanding GSS/SSPI/Kerberos raises PgError — use the psql CLI for those.

Scope / limits (it's a probe, not a driver):
  - Simple query protocol only; every cell comes back as str (or None for NULL).
  - No TLS. For sslmode=require, use the psql CLI path.
  - Optional `schema` sets search_path via a startup option (currentSchema=...).

Public API:
    cli = connect(host, port, user, password, db, schema=None, timeout=30)
    rows = cli.query("select ...")     # -> list[list[str|None]]
    cli.columns                        # column names of the last query
    cli.close()
"""

import base64
import hashlib
import hmac
import os
import socket
import struct

PROTOCOL_VERSION = 196608  # 3.0


class PgError(Exception):
    """Connection / auth / query error (carries the server message when available)."""


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


class PgClient:
    def __init__(self, host, port, user, password, db, schema=None, timeout=30):
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        self.user = user
        self.password = password or ""
        self.columns = []
        try:
            self._startup(user, db, schema)
        except Exception:
            self.close()
            raise

    # --- message framing: server msgs = 1 type byte + Int32 length (incl. itself) ---
    def _recv_exact(self, n: int) -> bytes:
        chunks, got = [], 0
        while got < n:
            b = self.sock.recv(n - got)
            if not b:
                raise PgError("connection closed by server")
            chunks.append(b)
            got += len(b)
        return b"".join(chunks)

    def _read_msg(self):
        mtype = self._recv_exact(1)
        length = struct.unpack("!I", self._recv_exact(4))[0]
        payload = self._recv_exact(length - 4) if length > 4 else b""
        return mtype, payload

    def _send(self, mtype: bytes, payload: bytes):
        self.sock.sendall(mtype + struct.pack("!I", len(payload) + 4) + payload)

    @staticmethod
    def _err(payload: bytes) -> str:
        # ErrorResponse: a sequence of (field-type byte, value\0); 'M' = message.
        fields = {}
        for part in payload.split(b"\x00"):
            if part:
                fields[chr(part[0])] = part[1:].decode(errors="replace")
        return fields.get("M", "unknown error") + (
            f" (SQLSTATE {fields['C']})" if "C" in fields else "")

    def _startup(self, user, db, schema):
        params = b"user\x00" + user.encode() + b"\x00database\x00" + (db or user).encode() + b"\x00"
        if schema:
            params += b"options\x00" + f"-c search_path={schema}".encode() + b"\x00"
        params += b"\x00"
        body = struct.pack("!I", PROTOCOL_VERSION) + params
        # Startup packet has NO type byte.
        self.sock.sendall(struct.pack("!I", len(body) + 4) + body)
        self._authenticate()
        self._await_ready()

    def _authenticate(self):
        while True:
            mtype, payload = self._read_msg()
            if mtype == b"E":
                raise PgError(self._err(payload))
            if mtype != b"R":
                raise PgError(f"unexpected message during auth: {mtype!r}")
            code = struct.unpack("!I", payload[:4])[0]
            if code == 0:           # AuthenticationOk
                return
            if code == 3:           # CleartextPassword
                self._send(b"p", self.password.encode() + b"\x00")
            elif code == 5:         # MD5Password
                salt = payload[4:8]
                self._send(b"p", self._md5(salt) + b"\x00")
            elif code == 10:        # SASL (SCRAM-SHA-256)
                mechs = [m for m in payload[4:].split(b"\x00") if m]
                if b"SCRAM-SHA-256" not in mechs:
                    raise PgError(f"no supported SASL mechanism (server offered {mechs})")
                self._scram_sha256()
                # _scram drives the exchange; loop continues to read AuthenticationOk.
            else:
                raise PgError(f"unsupported auth method code {code} "
                              f"(only cleartext/md5/scram-sha-256) — use the psql CLI")

    def _md5(self, salt: bytes) -> bytes:
        inner = hashlib.md5((self.password + self.user).encode()).hexdigest()
        return b"md5" + hashlib.md5(inner.encode() + salt).hexdigest().encode()

    def _scram_sha256(self):
        # client-first
        cnonce = base64.b64encode(os.urandom(18)).decode()
        client_first_bare = f"n=,r={cnonce}"
        initial = b"SCRAM-SHA-256\x00" + struct.pack("!I", len(b"n,," + client_first_bare.encode()))
        initial += b"n,," + client_first_bare.encode()
        self._send(b"p", initial)

        # server-first (AuthenticationSASLContinue, code 11)
        mtype, payload = self._read_msg()
        if mtype == b"E":
            raise PgError(self._err(payload))
        code = struct.unpack("!I", payload[:4])[0]
        if code != 11:
            raise PgError(f"expected SASLContinue, got auth code {code}")
        server_first = payload[4:].decode()
        attrs = dict(kv.split("=", 1) for kv in server_first.split(","))
        snonce, salt_b64, iters = attrs["r"], attrs["s"], int(attrs["i"])
        if not snonce.startswith(cnonce):
            raise PgError("SCRAM server nonce does not extend client nonce")
        salt = base64.b64decode(salt_b64)

        salted = hashlib.pbkdf2_hmac("sha256", self.password.encode(), salt, iters)
        client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        stored_key = hashlib.sha256(client_key).digest()
        client_final_bare = f"c=biws,r={snonce}"
        auth_message = f"{client_first_bare},{server_first},{client_final_bare}".encode()
        client_sig = hmac.new(stored_key, auth_message, hashlib.sha256).digest()
        proof = base64.b64encode(_xor(client_key, client_sig)).decode()
        self._send(b"p", f"{client_final_bare},p={proof}".encode())

        # server-final (AuthenticationSASLFinal, code 12) — verify ServerSignature.
        mtype, payload = self._read_msg()
        if mtype == b"E":
            raise PgError(self._err(payload))
        code = struct.unpack("!I", payload[:4])[0]
        if code != 12:
            raise PgError(f"expected SASLFinal, got auth code {code}")
        server_final = dict(kv.split("=", 1) for kv in payload[4:].decode().split(","))
        server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
        expected = base64.b64encode(hmac.new(server_key, auth_message, hashlib.sha256).digest()).decode()
        if server_final.get("v") != expected:
            raise PgError("SCRAM server signature mismatch (possible MITM / wrong server)")

    def _await_ready(self):
        # Drain ParameterStatus / BackendKeyData until ReadyForQuery ('Z').
        while True:
            mtype, payload = self._read_msg()
            if mtype == b"E":
                raise PgError(self._err(payload))
            if mtype == b"Z":       # ReadyForQuery
                return

    def query(self, sql: str):
        """Run a text query via the simple query protocol. Returns list[list[str|None]]."""
        self._send(b"Q", sql.encode() + b"\x00")
        rows = []
        self.columns = []
        error = None
        while True:
            mtype, payload = self._read_msg()
            if mtype == b"T":               # RowDescription
                self.columns = self._parse_row_desc(payload)
            elif mtype == b"D":             # DataRow
                rows.append(self._parse_data_row(payload))
            elif mtype == b"E":             # ErrorResponse
                error = self._err(payload)
            elif mtype == b"Z":             # ReadyForQuery -> done
                break
            # C (CommandComplete), I, S, N, etc. -> ignore
        if error:
            raise PgError(error)
        return rows

    @staticmethod
    def _parse_row_desc(payload):
        n = struct.unpack("!H", payload[:2])[0]
        cols, off = [], 2
        for _ in range(n):
            end = payload.index(b"\x00", off)
            cols.append(payload[off:end].decode(errors="replace"))
            off = end + 1 + 18          # name\0 + 18 bytes of field metadata
        return cols

    @staticmethod
    def _parse_data_row(payload):
        n = struct.unpack("!H", payload[:2])[0]
        cells, off = [], 2
        for _ in range(n):
            length = struct.unpack("!i", payload[off:off + 4])[0]
            off += 4
            if length == -1:            # SQL NULL
                cells.append(None)
            else:
                cells.append(payload[off:off + length].decode(errors="replace"))
                off += length
        return cells

    def close(self):
        try:
            self.sock.sendall(b"X" + struct.pack("!I", 4))  # Terminate (best-effort)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def connect(host, port, user, password, db, schema=None, timeout=30) -> PgClient:
    return PgClient(host, port, user, password, db, schema, timeout)
