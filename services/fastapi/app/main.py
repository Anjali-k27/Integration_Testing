"""
main.py — EPOCH gateway.

Routes:
    GET  /health              liveness probe
    POST /v1/agent/invoke     frozen, carries Deprecation/Sunset/Link headers
    POST /v2/agent/invoke     live, no deprecation headers

Both versions share _handle_invoke: verify JWT -> parse body -> redact PII
-> enforce admin-only tools -> call the LLM backend -> redact the answer
-> return {status, hospital_id, role, answer}.
"""
import json
import os
import re

import httpx
from epoch_auth import verify_jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

LITELLM_URL        = os.environ.get("LITELLM_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-epoch-demo-master-key")

CC_PATTERN  = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# Prompts that route to the admin-only generate_client_deliverable tool.
ADMIN_ONLY_KEYWORDS = ("clinical pdf", "deliverable")


def redact_pii(text: str) -> str:
    text = CC_PATTERN.sub("[REDACTED_CC]", text)
    text = SSN_PATTERN.sub("[REDACTED_SSN]", text)
    return text


def redact_json(obj):
    if isinstance(obj, str):
        return redact_pii(obj)
    if isinstance(obj, dict):
        return {k: redact_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_json(v) for v in obj]
    return obj


def requires_admin_tool(prompt: str) -> bool:
    p = prompt.lower()
    return any(kw in p for kw in ADMIN_ONLY_KEYWORDS)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/agent/invoke")
async def agent_invoke_v1(request: Request):
    """v1 — frozen. Returns deprecation headers so clients know to migrate."""
    resp = await _handle_invoke(request)
    resp.headers["Deprecation"] = "true"
    resp.headers["Sunset"]      = "Sat, 31 Dec 2026 23:59:59 GMT"
    resp.headers["Link"]        = '</v2/agent/invoke>; rel="successor-version"'
    return resp


@app.post("/v2/agent/invoke")
async def agent_invoke_v2(request: Request):
    """v2 — live. No deprecation headers."""
    return await _handle_invoke(request)


async def _handle_invoke(request: Request) -> JSONResponse:
    auth = request.headers.get("Authorization", "")
    try:
        claims = verify_jwt(auth)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=401)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)

    prompt = redact_pii(body.get("prompt") or body.get("question", ""))

    if requires_admin_tool(prompt) and claims["role"] != "admin":
        return JSONResponse(
            {"error": "forbidden: admin role required for this tool", "role": claims["role"]},
            status_code=403,
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            llm_resp = await client.post(
                f"{LITELLM_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"},
                json={"model": "epoch-default",
                      "messages": [{"role": "user", "content": prompt}]},
            )
            answer = llm_resp.json()
        except Exception as e:
            return JSONResponse({"error": f"litellm:{e}"}, status_code=502)

    answer = redact_json(answer)

    return JSONResponse({
        "status": 200,
        "hospital_id": claims["hospital_id"],
        "role": claims["role"],
        "answer": answer,
    })