"""Bound incoming write bodies before parsers or external AI services run."""

from starlette.responses import JSONResponse

MAX_WRITE_BODY_BYTES = 1024 * 1024
MAX_VOICE_TURN_BODY_BYTES = 8 * MAX_WRITE_BODY_BYTES


class RequestBodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"].upper() in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return

        limit = (MAX_VOICE_TURN_BODY_BYTES if scope["path"].endswith("/voice/turn")
                 else MAX_WRITE_BODY_BYTES)

        async def too_large():
            await JSONResponse({"detail": "Request body too large"}, status_code=413)(
                scope, receive, send
            )

        for name, value in scope["headers"]:
            if name.lower() == b"content-length":
                try:
                    if int(value) > limit:
                        await too_large()
                        return
                except ValueError:
                    pass  # The HTTP server owns malformed framing; count actual bytes below.

        messages = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self.app(scope, receive, send)
                return
            total += len(message.get("body", b""))
            if total > limit:
                await too_large()
                return
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay():
            if messages:
                return messages.pop(0)
            return await receive()

        await self.app(scope, replay, send)
