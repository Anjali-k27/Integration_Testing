"""
tests/fakes.py
FakePhoenixTracer + JWT minting for the smoke suite.

FakePhoenixTracer:
    Records spans into a list. Same API as the real OTel SDK.
    find(name) returns matching spans. reset() clears between tests.
    parent_id links build the trace tree — continuity assertions use this.

mint_jwt:
    Real HS256 JWT using PyJWT. Same SECRET + claims shape as epoch_auth.py.
    The FastAPI gateway verifies these with JWT_SECRET injected via env_file.
    If JWT_SECRET drifts between .env and fakes.py, test_01 fails with 401
    and the drift is caught immediately — correct behavior.
"""
import time, uuid
import jwt   # pip install PyJWT

JWT_SECRET   = "epoch-demo-secret-rotate-via-kms-in-prod"
JWT_ALG      = "HS256"
JWT_ISSUER   = "https://idp.epoch.internal"
JWT_AUDIENCE = "epoch-gateway.internal"


def mint_jwt(sub: str, role: str, hospital_id: str = "hospital_a", ttl: int = 3600) -> str:
    """
    Real HS256 JWT with the same claims that epoch_auth.verify_jwt() requires.
    Introduced: Session 16.2. Permanent.
    """
    now = int(time.time())
    payload = {
        "sub": sub, "role": role, "hospital_id": hospital_id,
        "iss": JWT_ISSUER, "aud": JWT_AUDIENCE,
        "iat": now, "exp": now + ttl,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


class FakeSpan:
    """One recorded span. Mirrors the OTel SDK Span API."""
    def __init__(self, name: str, parent=None):
        self.span_id   = uuid.uuid4().hex[:12]
        self.parent_id = parent.span_id if parent else None
        self.name      = name
        self.attributes: dict = {}
        self.start     = time.time()
        self.end       = None

    def set(self, key: str, value) -> None:
        self.attributes[key] = value


class FakePhoenixTracer:
    """
    In-memory span store. Same API as the real OTel tracer.

    The parent_id chain builds the trace tree:
        root span (parent_id=None) → child spans (parent_id=root.span_id)
    Continuity tests assert every span shares one root trace.

    Introduced: Session 16.2. Permanent.
    """
    def __init__(self):
        self.spans: list = []
        self._stack: list = []

    def start_span(self, name: str):
        parent = self._stack[-1] if self._stack else None
        span = FakeSpan(name, parent)
        self._stack.append(span)
        self.spans.append(span)
        return span

    def end_span(self, span) -> None:
        span.end = time.time()
        if self._stack and self._stack[-1] is span:
            self._stack.pop()

    def find(self, name: str) -> list:
        """Return all spans matching name. Returns [] if none — never raises."""
        return [s for s in self.spans if s.name == name]

    def all_spans(self) -> list:
        return list(self.spans)

    def reset(self) -> None:
        """Clear between tests. Call in setUp so spans don't bleed."""
        self.spans.clear()
        self._stack.clear()

    def root_spans(self) -> list:
        return [s for s in self.spans if s.parent_id is None]