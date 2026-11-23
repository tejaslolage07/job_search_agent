"""Multi-user front door for hosting. Logged-out visitors only see the landing page; an account needs the shared invitation code to sign up.
Each user gets a private workspace (own database, profile, resumes, schedules) served by its own dashboard app; this module only routes."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import AsyncExitStack, closing
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs

from .. import service
from .landing import render

COOKIE = "jsa_session"
SESSION_SECONDS = 30 * 24 * 3600
EMAIL_RE = re.compile(r"^[^@\s]{1,100}@[^@\s]{1,100}\.[^@\s]{2,}$")
MAX_BODY = 16 * 1024
FAIL_LIMIT, FAIL_WINDOW = 10, 15 * 60


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    return f"scrypt${salt.hex()}${hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32).hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got, bytes.fromhex(digest))
    except ValueError:
        return False


class Accounts:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "accounts.db"
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw TEXT NOT NULL, created_at TEXT NOT NULL)")
            c.commit()

    def create(self, email: str, password: str) -> int | None:
        with closing(sqlite3.connect(self.path)) as c:
            try:
                cur = c.execute("INSERT INTO users(email, pw, created_at) VALUES (?,?,?)", (email, hash_password(password), datetime.now(UTC).isoformat()))
            except sqlite3.IntegrityError:
                return None
            c.commit()
            return int(cur.lastrowid)

    def verify(self, email: str, password: str) -> int | None:
        with closing(sqlite3.connect(self.path)) as c:
            row = c.execute("SELECT id, pw FROM users WHERE email = ?", (email,)).fetchone()
        if row is None:
            check_password(password, "scrypt$" + "00" * 16 + "$" + "00" * 32)  # same work whether or not the account exists
            return None
        return int(row[0]) if check_password(password, row[1]) else None

    def ids(self) -> list[int]:
        with closing(sqlite3.connect(self.path)) as c:
            return [int(r[0]) for r in c.execute("SELECT id FROM users ORDER BY id")]

    def exists(self, uid: int) -> bool:
        with closing(sqlite3.connect(self.path)) as c:
            return c.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is not None


class Throttle:
    """Failed sign-up / sign-in attempts per client address (stops guessing the invitation code or a password)."""

    def __init__(self) -> None:
        self.fails: dict[str, list[float]] = {}

    def _recent(self, ip: str) -> list[float]:
        recent = [t for t in self.fails.get(ip, []) if time.time() - t < FAIL_WINDOW]
        self.fails[ip] = recent
        return recent

    def allowed(self, ip: str) -> bool:
        return len(self._recent(ip)) < FAIL_LIMIT

    def fail(self, ip: str) -> None:
        self._recent(ip).append(time.time())


def sign(secret: bytes, uid: int, now: float | None = None) -> str:
    payload = base64.urlsafe_b64encode(f"{uid}:{int((now or time.time()) + SESSION_SECONDS)}".encode()).decode()
    return f"{payload}.{hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()}"


def unsign(secret: bytes, token: str, now: float | None = None) -> int | None:
    try:
        payload, mac = token.rsplit(".", 1)
        if not hmac.compare_digest(mac, hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()):
            return None
        uid, exp = base64.urlsafe_b64decode(payload.encode()).decode().split(":")
        return int(uid) if int(exp) > (now or time.time()) else None
    except (ValueError, UnicodeDecodeError):
        return None


class Gateway:
    def __init__(self, root: Path, master_code: str, secret: str, defaults: Path, user_app: Callable[[service.Home], Any],
                 secure_cookie: bool = True) -> None:
        self.root, self.master, self.secret, self.defaults = Path(root), master_code.encode(), secret.encode(), Path(defaults)
        self.make_app, self.secure = user_app, secure_cookie
        self.accounts, self.throttle = Accounts(self.root), Throttle()
        self.apps: dict[int, Any] = {}
        self.stack = AsyncExitStack()
        self.lock = asyncio.Lock()

    # ---- per-user dashboards -------------------------------------------------------------------------------------
    def home_for(self, uid: int) -> service.Home:
        home = service.Home(self.root / "users" / str(uid))
        home.root.mkdir(parents=True, exist_ok=True)
        if not home.config.exists() and self.defaults.is_dir():
            shutil.copytree(self.defaults, home.config)
        return home

    async def app_for(self, uid: int) -> Any:
        async with self.lock:
            if uid not in self.apps:
                app = self.make_app(self.home_for(uid))
                await self.stack.enter_async_context(app.router.lifespan_context(app))  # runs the user's scheduler for as long as we are up
                self.apps[uid] = app
            return self.apps[uid]

    # ---- ASGI ----------------------------------------------------------------------------------------------------
    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":
            return await self._lifespan(receive, send)
        if scope["type"] != "http":
            return await send({"type": "websocket.close", "code": 1008})
        method, path = scope["method"], scope["path"]
        if path == "/healthz":
            return await self._reply(send, 200, b"ok", "text/plain")
        if method == "POST" and path in ("/signup", "/login"):
            return await self._auth(scope, receive, send, path)
        if path == "/logout":
            return await self._redirect(send, "/", cookie=self._cookie("", 0))
        uid = self._session_user(scope)
        if uid is None:
            if method == "GET" and path == "/":
                return await self._reply(send, 200, render(self._query(scope, "e")).encode(), "text/html; charset=utf-8")
            if path.startswith("/api/"):
                return await self._reply(send, 401, b'{"detail":"Sign in required"}', "application/json")
            return await self._redirect(send, "/")
        if method == "GET" and path in ("/login", "/signup"):
            return await self._redirect(send, "/")
        if method == "PUT" and path == "/api/llm/key":  # the key is the host's, shared; users cannot read or replace it
            return await self._reply(send, 403, b'{"detail":"The AI key is managed by the host."}', "application/json")
        app = await self.app_for(uid)
        if method == "GET" and path == "/api/llm":
            return await app(scope, receive, self._scrub_llm(send))
        await app(scope, receive, send)

    async def _lifespan(self, receive: Callable, send: Callable) -> None:
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                try:
                    for uid in self.accounts.ids():  # so every user's schedules keep firing after a restart
                        await self.app_for(uid)
                except Exception as exc:  # noqa: BLE001
                    return await send({"type": "lifespan.startup.failed", "message": str(exc)})
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await self.stack.aclose()
                return await send({"type": "lifespan.shutdown.complete"})

    # ---- sign up / sign in ---------------------------------------------------------------------------------------
    async def _auth(self, scope: dict, receive: Callable, send: Callable, path: str) -> None:
        ip = (scope.get("client") or ("?", 0))[0]
        if not self.throttle.allowed(ip):
            return await self._redirect(send, "/?e=slow")
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if len(body) > MAX_BODY:
                return await self._reply(send, 413, b"too large", "text/plain")
            if not msg.get("more_body"):
                break
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
        email, password = form.get("email", "").strip().lower(), form.get("password", "")
        if path == "/login":
            uid = self.accounts.verify(email, password) if len(password) <= 200 else None
            if uid is None:
                self.throttle.fail(ip)
                return await self._redirect(send, "/?e=login")
        else:
            if not hmac.compare_digest(form.get("master_code", "").encode(), self.master):
                self.throttle.fail(ip)
                return await self._redirect(send, "/?e=code")
            if not EMAIL_RE.match(email):
                return await self._redirect(send, "/?e=email")
            if not 8 <= len(password) <= 200:
                return await self._redirect(send, "/?e=pw")
            uid = self.accounts.create(email, password)
            if uid is None:
                return await self._redirect(send, "/?e=exists")
            self.home_for(uid)
        await self._redirect(send, "/", cookie=self._cookie(sign(self.secret, uid), SESSION_SECONDS))

    # ---- helpers -------------------------------------------------------------------------------------------------
    def _cookie(self, value: str, max_age: int) -> str:
        return f"{COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if self.secure else "")

    def _session_user(self, scope: dict) -> int | None:
        for k, v in scope["headers"]:
            if k == b"cookie":
                morsel = SimpleCookie(v.decode("latin-1")).get(COOKIE)
                uid = unsign(self.secret, morsel.value) if morsel else None
                return uid if uid is not None and self.accounts.exists(uid) else None
        return None

    @staticmethod
    def _query(scope: dict, key: str) -> str | None:
        vals = parse_qs(scope.get("query_string", b"").decode("latin-1")).get(key)
        return vals[0] if vals else None

    @staticmethod
    async def _reply(send: Callable, status: int, body: bytes, ctype: str, extra: list[tuple[bytes, bytes]] | None = None) -> None:
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", ctype.encode()), (b"cache-control", b"no-store"),
                                                                                    (b"x-content-type-options", b"nosniff"), *(extra or [])]})
        await send({"type": "http.response.body", "body": body})

    async def _redirect(self, send: Callable, to: str, cookie: str | None = None) -> None:
        await self._reply(send, 303, b"", "text/plain", [(b"location", to.encode())] + ([(b"set-cookie", cookie.encode())] if cookie else []))

    @staticmethod
    def _scrub_llm(send: Callable) -> Callable:
        """Hide the shared key's first/last characters and the server path from /api/llm."""
        start: dict = {}
        chunks: list[bytes] = []

        async def wrapped(msg: dict) -> None:
            if msg["type"] == "http.response.start":
                start.update(msg)
            elif msg["type"] == "http.response.body":
                chunks.append(msg.get("body", b""))
                if msg.get("more_body"):
                    return
                body = b"".join(chunks)
                try:
                    data = json.loads(body)
                    data["key_hint"], data["env_file"] = None, None
                    body = json.dumps(data).encode()
                except ValueError:
                    pass
                headers = [(k, v) for k, v in start["headers"] if k != b"content-length"] + [(b"content-length", str(len(body)).encode())]
                await send({**start, "headers": headers})
                await send({"type": "http.response.body", "body": body})

        return wrapped
