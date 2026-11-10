#!/usr/bin/env python3
"""Minimal WebSocket client (RFC 6455) over stdlib socket+ssl — no pip.

Just enough to talk to TChat's SocketCluster realtime endpoint:
connect with a Sec-WebSocket-Protocol subprotocol (the JWT), send/recv text
frames, auto-reply to control pings. Not a general-purpose WS library.

Internal module — imported by send.py.
"""

import base64
import os
import socket
import ssl
import struct
import sys
import urllib.parse


class WSError(Exception):
    pass


class WebSocket:
    def __init__(self, url, subprotocols=None, origin=None, timeout=10, verify=True):
        u = urllib.parse.urlparse(url)
        self.host = u.hostname
        self.port = u.port or (443 if u.scheme == "wss" else 80)
        self.path = u.path or "/"
        if u.query:
            self.path += "?" + u.query
        self._buf = b""

        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                print("[WARN] WS SSL verification disabled", file=sys.stderr)
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
        raw.settimeout(timeout)
        self.sock = raw
        self._handshake(subprotocols, origin)

    def _handshake(self, subprotocols, origin):
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if subprotocols:
            lines.append("Sec-WebSocket-Protocol: " + ", ".join(subprotocols))
        if origin:
            lines.append(f"Origin: {origin}")
        lines += ["User-Agent: tchat-skill/1.0", "", ""]
        self.sock.sendall("\r\n".join(lines).encode())

        # read response headers
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("connection closed during handshake")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise WSError(f"handshake failed: {status} :: {head.decode(errors='replace')[:300]}")
        self._buf = rest  # any framed bytes already received

    # ── frame I/O ────────────────────────────────────────────────
    def send_text(self, text: str):
        payload = text.encode("utf-8")
        fin_op = 0x80 | 0x1  # FIN + text
        header = struct.pack("!B", fin_op)
        mask_bit = 0x80
        n = len(payload)
        if n < 126:
            header += struct.pack("!B", mask_bit | n)
        elif n < 65536:
            header += struct.pack("!B", mask_bit | 126) + struct.pack("!H", n)
        else:
            header += struct.pack("!B", mask_bit | 127) + struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        """Return next text-frame payload (str). Auto-handles ping/pong/close.
        Returns None on close."""
        while True:
            b0, b1 = self._read_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            data = self._read_exact(length)
            if masked:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))

            if opcode == 0x8:           # close
                return None
            if opcode == 0x9:           # ping -> pong
                self._send_control(0xA, data)
                continue
            if opcode == 0xA:           # pong
                continue
            if opcode in (0x1, 0x0):    # text / continuation
                return data.decode("utf-8", "replace")
            if opcode == 0x2:           # binary -> hand back as repr
                return data
            # ignore others

    def _send_control(self, opcode, data=b""):
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(struct.pack("!BB", 0x80 | opcode, 0x80 | len(data)) + mask + masked)

    def close(self):
        try:
            self._send_control(0x8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
