#!/usr/bin/env python3
"""Minimal MySQL/MariaDB client over a raw socket — Python stdlib only.

Speaks just enough of the MySQL client/server protocol (protocol 41) to log in
with `mysql_native_password` and run a text query (COM_QUERY), so `probe_db.py`
can hit MySQL WITHOUT the `mysql` CLI being installed — same zero-dependency
stance as probe_redis (RESP socket) and probe_kafka (HTTP). This keeps the whole
stack-verify toolkit "stdlib + token, runs on any machine".

Scope / limits (by design — this is a probe, not a driver):
  - Auth: `mysql_native_password` only (the MySQL 5.7 default and MariaDB default).
    A server that forces `caching_sha2_password` full-auth (typical MySQL 8 with a
    fresh user, over a non-TLS socket) needs RSA/TLS we don't implement -> raises
    MySQLError with a clear message. Fast-auth-OK path for caching_sha2 is handled.
  - Text protocol only (COM_QUERY). Every cell comes back as a str (or None for
    SQL NULL) — exactly what probe_db's assertions expect.
  - No TLS. Connection strings here use useSSL=false; for TLS use the CLI path.

Public API:
    cli = connect(host, port, user, password, db=None, timeout=30)
    rows = cli.query("select ...")     # -> list[list[str|None]]
    cli.columns                        # column names of the last query
    cli.close()
"""

import hashlib
import socket
import struct

# Capability flags (only the ones we set/need).
CLIENT_LONG_PASSWORD = 0x00000001
CLIENT_LONG_FLAG = 0x00000004
CLIENT_CONNECT_WITH_DB = 0x00000008
CLIENT_PROTOCOL_41 = 0x00000200
CLIENT_TRANSACTIONS = 0x00002000
CLIENT_SECURE_CONNECTION = 0x00008000
CLIENT_PLUGIN_AUTH = 0x00080000

COM_QUERY = 0x03


class MySQLError(Exception):
    """Connection / auth / query error (carries the server message when available)."""


def _scramble_native(password: str, salt: bytes) -> bytes:
    """mysql_native_password: SHA1(pw) XOR SHA1(salt + SHA1(SHA1(pw)))."""
    if not password:
        return b""
    pw = password.encode()
    s1 = hashlib.sha1(pw).digest()
    s2 = hashlib.sha1(s1).digest()
    s3 = hashlib.sha1(salt + s2).digest()
    return bytes(a ^ b for a, b in zip(s1, s3))


def _lenc_int(buf: bytes, off: int):
    """Decode a length-encoded integer. Returns (value, new_offset)."""
    first = buf[off]
    if first < 0xFB:
        return first, off + 1
    if first == 0xFC:
        return int.from_bytes(buf[off + 1:off + 3], "little"), off + 3
    if first == 0xFD:
        return int.from_bytes(buf[off + 1:off + 4], "little"), off + 4
    if first == 0xFE:
        return int.from_bytes(buf[off + 1:off + 9], "little"), off + 9
    # 0xFB = NULL marker — caller handles it before reaching here.
    return None, off + 1


class MySQLClient:
    def __init__(self, host, port, user, password, db=None, timeout=30):
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        self._seq = 0
        self.columns = []
        try:
            self._handshake(user, password or "", db)
        except Exception:
            self.close()
            raise

    # --- packet framing: 3-byte little-endian length + 1-byte sequence id ---
    def _recv_exact(self, n: int) -> bytes:
        chunks = []
        got = 0
        while got < n:
            b = self.sock.recv(n - got)
            if not b:
                raise MySQLError("connection closed by server")
            chunks.append(b)
            got += len(b)
        return b"".join(chunks)

    def _read_packet(self) -> bytes:
        header = self._recv_exact(4)
        length = header[0] | (header[1] << 8) | (header[2] << 16)
        self._seq = header[3]
        return self._recv_exact(length)

    def _write_packet(self, payload: bytes):
        self._seq = (self._seq + 1) & 0xFF
        header = struct.pack("<I", len(payload))[:3] + bytes([self._seq])
        self.sock.sendall(header + payload)

    def _write_command(self, payload: bytes):
        self._seq = -1  # commands start a new sequence at 0
        self._write_packet(payload)

    @staticmethod
    def _err(payload: bytes) -> str:
        code = int.from_bytes(payload[1:3], "little")
        msg = payload[3:]
        if msg[:1] == b"#":  # protocol-41: '#' + 5-byte SQLSTATE + message
            msg = msg[6:]
        return f"MySQL error {code}: {msg.decode(errors='replace')}"

    def _handshake(self, user, password, db):
        pkt = self._read_packet()
        if pkt[:1] == b"\xff":
            raise MySQLError(self._err(pkt))
        # Initial handshake (protocol 10).
        off = 1
        off = pkt.index(b"\x00", off) + 1           # skip server version (null-term)
        off += 4                                     # connection id
        salt = pkt[off:off + 8]; off += 8            # auth-plugin-data part 1
        off += 1                                     # filler
        off += 2                                     # capability flags (lower) — ignored
        if len(pkt) > off:
            off += 1                                 # charset
            off += 2                                 # status flags
            off += 2                                 # capability flags (upper)
            auth_len = pkt[off]; off += 1            # length of auth-plugin-data
            off += 10                                # reserved
            part2 = max(13, auth_len - 8)
            salt += pkt[off:off + part2 - 1]         # drop the trailing null
            off += part2
            plugin = b""
            if off < len(pkt):
                end = pkt.index(b"\x00", off) if b"\x00" in pkt[off:] else len(pkt)
                plugin = pkt[off:end]
        else:
            plugin = b"mysql_native_password"

        if plugin and plugin != b"mysql_native_password" and plugin != b"caching_sha2_password":
            raise MySQLError(f"unsupported auth plugin '{plugin.decode(errors='replace')}' "
                             f"(only mysql_native_password is implemented)")

        auth = _scramble_native(password, salt[:20])
        self._send_handshake_response(user, auth, db)

        resp = self._read_packet()
        resp = self._handle_auth_followups(resp, password, salt)
        if resp[:1] == b"\xff":
            raise MySQLError(self._err(resp))
        # 0x00 OK -> authenticated.

    def _send_handshake_response(self, user, auth, db):
        caps = (CLIENT_PROTOCOL_41 | CLIENT_SECURE_CONNECTION | CLIENT_PLUGIN_AUTH
                | CLIENT_LONG_PASSWORD | CLIENT_LONG_FLAG | CLIENT_TRANSACTIONS)
        if db:
            caps |= CLIENT_CONNECT_WITH_DB
        body = struct.pack("<I", caps)
        body += struct.pack("<I", 16 * 1024 * 1024)   # max packet size
        body += bytes([45])                            # charset: utf8mb4_general_ci
        body += b"\x00" * 23                           # reserved
        body += user.encode() + b"\x00"
        body += bytes([len(auth)]) + auth              # secure-connection: length-prefixed
        if db:
            body += db.encode() + b"\x00"
        body += b"mysql_native_password\x00"
        self._write_packet(body)

    def _handle_auth_followups(self, resp, password, salt):
        # AuthSwitchRequest (0xfe): server asks to switch auth plugin.
        if resp[:1] == b"\xfe":
            off = 1
            end = resp.index(b"\x00", off)
            plugin = resp[off:end]
            new_salt = resp[end + 1:].rstrip(b"\x00")
            if plugin != b"mysql_native_password":
                raise MySQLError(f"server requested auth switch to '{plugin.decode(errors='replace')}' "
                                 f"(unsupported; use the mysql CLI for this server)")
            self._write_packet(_scramble_native(password, new_salt[:20]))
            resp = self._read_packet()
        # caching_sha2_password fast-auth marker: 0x01 0x03 = success, 0x01 0x04 = full auth needed.
        if resp[:1] == b"\x01":
            if len(resp) >= 2 and resp[1] == 0x04:
                raise MySQLError("server requires caching_sha2_password full auth "
                                 "(needs TLS/RSA) — use the mysql CLI for this account")
            resp = self._read_packet()  # 0x03 fast-auth-ok -> followed by OK
        return resp

    def query(self, sql: str):
        """Run a text query. Returns rows as list[list[str|None]]; sets self.columns."""
        self._write_command(bytes([COM_QUERY]) + sql.encode())
        first = self._read_packet()
        if first[:1] == b"\xff":
            raise MySQLError(self._err(first))
        if first[:1] == b"\x00":           # OK packet — non-SELECT, no result set
            self.columns = []
            return []
        if first[:1] == b"\xfb":           # LOCAL INFILE — not supported
            raise MySQLError("LOCAL INFILE response not supported")

        column_count, _ = _lenc_int(first, 0)
        self.columns = [self._read_column_name() for _ in range(column_count)]
        self._read_eof_if_present()        # EOF after column defs

        rows = []
        while True:
            pkt = self._read_packet()
            if pkt[:1] == b"\xfe" and len(pkt) < 9:   # EOF -> end of rows
                break
            if pkt[:1] == b"\xff":
                raise MySQLError(self._err(pkt))
            rows.append(self._parse_row(pkt, column_count))
        return rows

    def _read_column_name(self):
        """Column definition packet (protocol 41); we only keep the column name."""
        pkt = self._read_packet()
        off = 0
        for _ in range(4):                  # catalog, schema, table, org_table
            off = self._skip_lenc_str(pkt, off)
        n, off = _lenc_int(pkt, off)        # name length
        name = pkt[off:off + n].decode(errors="replace")
        return name

    @staticmethod
    def _skip_lenc_str(buf, off):
        n, off = _lenc_int(buf, off)
        return off + (n or 0)

    @staticmethod
    def _parse_row(pkt, column_count):
        cells = []
        off = 0
        for _ in range(column_count):
            if pkt[off] == 0xFB:            # NULL
                cells.append(None)
                off += 1
            else:
                n, off = _lenc_int(pkt, off)
                cells.append(pkt[off:off + n].decode(errors="replace"))
                off += n
        return cells

    def _read_eof_if_present(self):
        pkt = self._read_packet()
        if not (pkt[:1] == b"\xfe" and len(pkt) < 9):
            # Not an EOF — server uses CLIENT_DEPRECATE_EOF; this packet is the first
            # row. We didn't negotiate that flag, so this shouldn't happen; raise to
            # surface protocol drift rather than silently dropping a row.
            raise MySQLError("unexpected packet where column-def EOF was expected")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def connect(host, port, user, password, db=None, timeout=30) -> MySQLClient:
    return MySQLClient(host, port, user, password, db, timeout)
