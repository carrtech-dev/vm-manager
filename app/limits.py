"""Reject oversized bodies while receiving, before multipart parsing fills temporary storage."""

from starlette.responses import JSONResponse


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        limit = 32 * 1024**3 + 1024**2 if scope.get("path") == "/api/isos" else 1024**2
        headers = dict(scope.get("headers", []))
        try:
            advertised = int(headers.get(b"content-length", b"0"))
        except ValueError:
            advertised = limit + 1
        if advertised > limit:
            return await JSONResponse(
                {"detail": "Request body exceeds upload limit"}, status_code=413
            )(scope, receive, send)
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                from starlette.exceptions import HTTPException

                raise HTTPException(413, "Request body exceeds upload limit")
            return message

        return await self.app(scope, bounded_receive, send)
