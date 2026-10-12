#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi-Device Folder Sync Server (a minimal Nutstore-like backend).

Pure Python standard library (http.server + sqlite3). No third-party deps.

Features:
  - Account register / login, token based auth
  - One shared folder per account, versioned file metadata
  - upload / download / delete with optimistic-lock conflict detection
  - Conflict is detected by base_version mismatch; client resolves by
    keeping a conflict copy under a new name.

Usage:
  python server.py --host 0.0.0.0 --port 8000 --data ./syncdata
"""

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Directory of this file: the mobile web client (mobile.html) lives next to
# the server so it can be served from the same origin (no CORS needed).
WEB_ROOT = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def safe_relpath(raw: str):
    """Validate a client supplied path and return a normalized relative path.

    Rejects absolute paths, '..' components, backslashes and drive letters
    to prevent traversal outside the per-user storage directory.
    """
    if not raw:
        raise ValueError("empty path")
    p = raw.replace("\\", "/")
    p = p.lstrip("/")
    parts = [c for c in p.split("/") if c not in ("", ".")]
    if not parts:
        raise ValueError("empty path")
    for c in parts:
        if c in ("..", "~") or ":" in c:
            raise ValueError("invalid path component: %r" % c)
    return "/".join(parts)


# ---------------------------------------------------------------------------
# storage layer
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        self.blob_root = os.path.join(self.data_dir, "blobs")
        os.makedirs(self.blob_root, exist_ok=True)
        self.conn = sqlite3.connect(
            os.path.join(self.data_dir, "sync.db"), check_same_thread=False
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    username TEXT PRIMARY KEY,
                    salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    token TEXT
                );
                CREATE TABLE IF NOT EXISTS tokens(
                    token TEXT PRIMARY KEY,
                    username TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(username);
                CREATE TABLE IF NOT EXISTS files(
                    username TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    PRIMARY KEY(username, path)
                );
                CREATE TABLE IF NOT EXISTS counters(
                    username TEXT PRIMARY KEY,
                    next_version INTEGER NOT NULL
                );
                """
            )
            self.conn.commit()

    # ---- users ----
    def register(self, username: str, password: str):
        if not username or not password:
            raise ValueError("username and password required")
        salt = secrets.token_hex(16)
        with self.lock:
            cur = self.conn.execute(
                "SELECT 1 FROM users WHERE username=?", (username,)
            )
            if cur.fetchone():
                raise KeyError("username already exists")
            self.conn.execute(
                "INSERT INTO users(username, salt, password_hash) VALUES(?,?,?)",
                (username, salt, hash_password(password, salt)),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO counters(username, next_version) VALUES(?,1)",
                (username,),
            )
            self.conn.commit()

    def login(self, username: str, password: str) -> str:
        with self.lock:
            cur = self.conn.execute(
                "SELECT salt, password_hash FROM users WHERE username=?",
                (username,),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError("user not found")
            if not hmac.compare_digest(
                row[1], hash_password(password, row[0])
            ):
                raise PermissionError("wrong password")
            token = secrets.token_hex(32)
            # 每个设备登录都产生一个新 token（会话池多值并存），
            # 这样同一账号的多个设备可以同时在线互不踢下线。
            self.conn.execute(
                "INSERT INTO tokens(token, username) VALUES(?,?)", (token, username)
            )
            self.conn.commit()
            return token

    def resolve_token(self, token: str) -> str | None:
        if not token:
            return None
        with self.lock:
            cur = self.conn.execute(
                "SELECT username FROM tokens WHERE token=?", (token,)
            )
            row = cur.fetchone()
            return row[0] if row else None

    # ---- files ----
    def list_files(self, username: str):
        with self.lock:
            cur = self.conn.execute(
                "SELECT path, size, mtime, sha256, version FROM files "
                "WHERE username=? ORDER BY path",
                (username,),
            )
            out = []
            for path, size, mtime, sha, version in cur.fetchall():
                out.append(
                    {
                        "path": path,
                        "size": size,
                        "mtime": mtime,
                        "sha256": sha,
                        "version": version,
                    }
                )
            return {"files": out}

    def get_file(self, username: str, path: str):
        with self.lock:
            cur = self.conn.execute(
                "SELECT size, mtime, sha256, version FROM files "
                "WHERE username=? AND path=?",
                (username, path),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "size": row[0],
                "mtime": row[1],
                "sha256": row[2],
                "version": row[3],
            }

    def upload(
        self, username: str, path: str, data: bytes,
        mtime: int, sha: str, base_version: int,
    ):
        """Optimistic-lock upload.

        Returns (status, payload):
          status "ok"     -> accepted, payload {version}
          status "conflict" -> payload {server: {...}}
        """
        path = safe_relpath(path)
        with self.lock:
            cur = self.conn.execute(
                "SELECT size, mtime, sha256, version FROM files "
                "WHERE username=? AND path=?",
                (username, path),
            )
            row = cur.fetchone()
            current_version = row[3] if row else 0

            if current_version != base_version:
                # stale base: the file changed on the server meanwhile
                server = (
                    {
                        "size": row[0],
                        "mtime": row[1],
                        "sha256": row[2],
                        "version": row[3],
                    }
                    if row
                    else None
                )
                return "conflict", {"server": server}

            actual_sha = sha256_hex(data)
            if actual_sha != sha:
                raise ValueError("sha256 mismatch")

            if row is not None and row[2] == sha:
                # identical content already stored -> idempotent no-op
                return "ok", {"version": row[3], "noop": True}

            cur = self.conn.execute(
                "SELECT next_version FROM counters WHERE username=?",
                (username,),
            )
            counter_row = cur.fetchone()
            next_version = counter_row[0] if counter_row else 1
            self.conn.execute(
                "UPDATE counters SET next_version=? WHERE username=?",
                (next_version + 1, username),
            )

            self._write_blob(username, path, data)
            self.conn.execute(
                "INSERT OR REPLACE INTO files"
                "(username, path, size, mtime, sha256, version) "
                "VALUES(?,?,?,?,?,?)",
                (username, path, len(data), mtime, sha, next_version),
            )
            self.conn.commit()
            return "ok", {"version": next_version, "noop": False}

    def delete(self, username: str, path: str, base_version: int):
        """Optimistic-lock delete. Returns (status, payload)."""
        path = safe_relpath(path)
        with self.lock:
            cur = self.conn.execute(
                "SELECT version FROM files WHERE username=? AND path=?",
                (username, path),
            )
            row = cur.fetchone()
            current_version = row[0] if row else 0
            if current_version != base_version:
                return "conflict", {"server": self._meta(username, path)}
            self.conn.execute(
                "DELETE FROM files WHERE username=? AND path=?",
                (username, path),
            )
            self._remove_blob(username, path)
            self.conn.commit()
            return "ok", {"ok": True}

    def _meta(self, username: str, path: str):
        cur = self.conn.execute(
            "SELECT size, mtime, sha256, version FROM files "
            "WHERE username=? AND path=?",
            (username, path),
        )
        row = cur.fetchone()
        return (
            {"size": row[0], "mtime": row[1], "sha256": row[2], "version": row[3]}
            if row
            else None
        )

    def _blob_path(self, username: str, path: str) -> str:
        return os.path.join(
            self.blob_root, username, *path.split("/")
        )

    def _write_blob(self, username: str, path: str, data: bytes):
        target = self._blob_path(username, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp" + secrets.token_hex(4)
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)

    def _remove_blob(self, username: str, path: str):
        target = self._blob_path(username, path)
        try:
            os.remove(target)
        except FileNotFoundError:
            pass

    def read_blob(self, username: str, path: str) -> bytes:
        target = self._blob_path(username, path)
        with open(target, "rb") as fh:
            return fh.read()


# ---------------------------------------------------------------------------
# http handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "DuSync/1.0"
    store: "Store | None" = None  # injected by main()

    # ---- plumbing ----
    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code: int, body: bytes, headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _read_raw(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length)

    def get_store(self) -> "Store":
        if self.store is None:
            raise RuntimeError("server store not initialized")
        return self.store

    def _require_user(self) -> str | None:
        store = self.get_store()
        token = self.headers.get("X-Token") or ""
        user = store.resolve_token(token)
        if not user:
            self._send_json(401, {"ok": False, "error": "invalid token"})
            return None
        return user

    def _serve_web(self, path: str):
        """Serve the mobile web client (same origin, no CORS needed)."""
        web = os.path.join(WEB_ROOT, "mobile.html")
        if not os.path.isfile(web):
            self._send_json(404, {"ok": False, "error": "mobile.html not found"})
            return
        with open(web, "rb") as fh:
            body = fh.read()
        self._send_bytes(
            200,
            body,
            {
                "Content-Type": "text/html; charset=utf-8",
                "Cache-Control": "no-store",
            },
        )

    def log_message(self, format, *args):  # quieter logs
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), format % args)
        )

    # ---- routes ----
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            store = self.get_store()
            if parsed.path == "/api/register":
                data = self._read_json()
                username = str(data.get("username") or "").strip()
                password = str(data.get("password") or "")
                store.register(username, password)
                token = store.login(username, password)
                self._send_json(200, {"ok": True, "token": token, "username": username})

            elif parsed.path == "/api/login":
                data = self._read_json()
                username = str(data.get("username") or "").strip()
                password = str(data.get("password") or "")
                token = store.login(username, password)
                self._send_json(200, {"ok": True, "token": token, "username": username})

            elif parsed.path == "/api/upload":
                user = self._require_user()
                if user is None:
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                path = qs.get("path", [""])[0]
                base_version = int(qs.get("version", ["0"])[0])
                mtime = int(self.headers.get("X-MTime", "0"))
                sha = self.headers.get("X-SHA256", "").strip()
                data = self._read_raw()
                status, payload = store.upload(
                    user, path, data, mtime, sha, base_version
                )
                if status == "conflict":
                    self._send_json(
                        409,
                        {"ok": False, "conflict": True, "server": payload["server"]},
                    )
                else:
                    self._send_json(200, {"ok": True, **payload})

            elif parsed.path == "/api/delete":
                user = self._require_user()
                if user is None:
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                path = qs.get("path", [""])[0]
                base_version = int(qs.get("version", ["0"])[0])
                status, payload = store.delete(user, path, base_version)
                if status == "conflict":
                    self._send_json(
                        409,
                        {"ok": False, "conflict": True, "server": payload["server"]},
                    )
                else:
                    self._send_json(200, {"ok": True})

            else:
                self._send_json(404, {"ok": False, "error": "not found"})

        except KeyError as exc:
            self._send_json(409, {"ok": False, "error": str(exc)})
        except PermissionError as exc:
            self._send_json(403, {"ok": False, "error": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - report to client
            self._send_json(500, {"ok": False, "error": "server error: %s" % exc})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            store = self.get_store()
            if parsed.path in ("/", "/index.html", "/mobile.html"):
                self._serve_web(parsed.path)
            elif parsed.path == "/api/list":
                user = self._require_user()
                if user is None:
                    return
                self._send_json(200, store.list_files(user))

            elif parsed.path == "/api/download":
                user = self._require_user()
                if user is None:
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                path = qs.get("path", [""])[0]
                meta = store.get_file(user, path)
                if meta is None:
                    self._send_json(404, {"ok": False, "error": "file not found"})
                    return
                body = store.read_blob(user, path)
                self._send_bytes(
                    200,
                    body,
                    {
                        "X-SHA256": meta["sha256"],
                        "X-MTime": meta["mtime"],
                        "X-Version": meta["version"],
                        "Content-Type": "application/octet-stream",
                    },
                )

            else:
                self._send_json(404, {"ok": False, "error": "not found"})

        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": "server error: %s" % exc})


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _is_no_console():
    try:
        return not sys.stdout or not sys.stdout.isatty()
    except Exception:
        return True


def main():
    parser = argparse.ArgumentParser(description="Multi-device folder sync server")
    parser.add_argument("--host", default="0.0.0.0")
    default_port = int(os.environ.get("PORT", "8000"))
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument("--data", default="./syncdata", help="data directory")
    parser.add_argument("--log-file", default="", help="log file path (default: DATA/server.log)")
    args = parser.parse_args()

    store = Store(args.data)
    Handler.store = store

    # redirect stdout/stderr to log file when running without console
    if _is_no_console():
        log_path = args.log_file or os.path.join(args.data, "server.log")
        os.makedirs(os.path.dirname(log_path) if os.path.dirname(log_path) else ".", exist_ok=True)
        sys.stdout = sys.stderr = open(log_path, "a", encoding="utf-8", buffering=1)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        "[dusync-server] listening on http://%s:%d  data=%s"
        % (args.host, args.port, store.data_dir),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dusync-server] stopped", flush=True)


if __name__ == "__main__":
    main()