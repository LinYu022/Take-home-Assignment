"""Keep render/PNG leases alive through successful send or client disconnect."""
from fastapi.responses import Response, JSONResponse, StreamingResponse


class HeldResponse(Response):
    def __init__(self, *args, leases=(), **kwargs):
        super().__init__(*args, **kwargs); self.leases = leases

    async def __call__(self, scope, receive, send):
        try: await super().__call__(scope, receive, send)
        finally:
            for lease in self.leases: lease.close()


class HeldJSONResponse(JSONResponse):
    def __init__(self, *args, leases=(), **kwargs):
        super().__init__(*args, **kwargs); self.leases = leases

    async def __call__(self, scope, receive, send):
        try: await super().__call__(scope, receive, send)
        finally:
            for lease in self.leases: lease.close()


class HeldStreamResponse(StreamingResponse):
    def __init__(self, *args, leases=(), **kwargs):
        super().__init__(*args, **kwargs); self.leases = leases

    async def __call__(self, scope, receive, send):
        try: await super().__call__(scope, receive, send)
        finally:
            for lease in self.leases: lease.close()
