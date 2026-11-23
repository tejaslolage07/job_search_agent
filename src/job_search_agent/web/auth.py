"""HTTP Basic login for a hosted dashboard (pure ASGI middleware, covers every route including the event stream)."""
from __future__ import annotations

import base64
import hmac


class BasicAuthMiddleware:
    def __init__(self, app, username: str, password: str, open_paths: tuple[str, ...] = ("/healthz",)) -> None:  # type: ignore[no-untyped-def]
        self.app, self.open_paths = app, open_paths
        self._user, self._pass = username.encode(), password.encode()

    def _ok(self, headers: list[tuple[bytes, bytes]]) -> bool:
        for k, v in headers:
            if k == b"authorization" and v[:6].lower() == b"basic ":
                try:
                    user, _, pw = base64.b64decode(v[6:], validate=True).partition(b":")
                except ValueError:
                    return False
                # evaluate both so timing does not reveal which one was wrong
                return hmac.compare_digest(user, self._user) & hmac.compare_digest(pw, self._pass)
        return False

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] not in ("http", "websocket") or scope.get("path") in self.open_paths:
            if scope["type"] == "http" and scope.get("path") == "/healthz":
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"ok"})
                return
            await self.app(scope, receive, send)
            return
        if self._ok(scope["headers"]):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"www-authenticate", b'Basic realm="job_search_agent", charset="UTF-8"'), (b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"Login required"})
