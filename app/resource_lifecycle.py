"""Outer ASGI finally closes leases even if a response never starts sending."""


class ResourceLifecycle:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})
        try:
            await self.app(scope, receive, send)
        finally:
            cancellation = state.get("render_cancellation")
            if cancellation is not None: cancellation.set()
            service, owner = state.get("render_service"), state.get("render_owner")
            if service is not None and owner is not None: service.cancel(owner)
            for lease in state.get("render_leases", ()):
                lease.close()
            dispatch = state.get("render_dispatch")
            if dispatch is not None: dispatch.finish(response=True)
