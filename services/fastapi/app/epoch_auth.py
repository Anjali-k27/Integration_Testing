"""
epoch_auth.py — JWT verification for the EPOCH gateway.

Claims contract shared with tests/fakes.py::mint_jwt. Any drift in
JWT_SECRET, JWT_ISSUER, or JWT_AUDIENCE between here and the test
fixtures fails closed (401), not open.
"""
import os
import jwt

JWT_SECRET   = os.environ.get("JWT_SECRET", "epoch-demo-secret-rotate-via-kms-in-prod")
JWT_ALG      = "HS256"
JWT_ISSUER   = "https://idp.epoch.internal"
JWT_AUDIENCE = "epoch-gateway.internal"

REQUIRED_CLAIMS = ("sub", "role", "hospital_id")


def verify_jwt(auth_header: str) -> dict:
    """Verify a `Bearer <jwt>` header and return its claims. Raises ValueError on any failure."""
    if not auth_header or not auth_header.startswith("Bearer "):
        raise ValueError("missing bearer token")
    token = auth_header.split(" ", 1)[1]
    try:
        claims = jwt.decode(
            token, JWT_SECRET, algorithms=[JWT_ALG],
            audience=JWT_AUDIENCE, issuer=JWT_ISSUER,
        )
    except jwt.PyJWTError as e:
        raise ValueError(f"invalid token: {e}")

    for field in REQUIRED_CLAIMS:
        if field not in claims:
            raise ValueError(f"missing claim: {field}")
    return claims