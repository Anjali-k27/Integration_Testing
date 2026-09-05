"""
app.py — mock LiteLLM-compatible backend.

Stands in for a real LiteLLM proxy so the cluster runs end to end
without provisioning real model provider keys. Speaks just enough of
the OpenAI chat-completions shape for the gateway's _handle_invoke to work.

Session 16.3 additions (see Session5/README.md "What's next — Session 16.3"):
  - time.sleep() inference-latency stub: a real model call blocks on
    network + GPU time, not asyncio.sleep(). Simulating it as a genuinely
    blocking call is what makes the single uvicorn worker's event loop
    starve under concurrency — the exact "sync call in an async handler"
    failure mode the load test is meant to surface. Without this, the mock
    returns in <1ms and no staged ramp will ever find a cliff.
  - /stats counter: lets a load test confirm authority under concurrency
    by checking this backend was never invoked for a blocked request,
    not just that the client received a 403 (the gap the README flagged:
    FakePhoenixTracer isn't wired to the real process).
"""
import random
import time

from fastapi import FastAPI, Request

app = FastAPI()

call_count = 0


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/stats")
async def stats():
    return {"call_count": call_count}


@app.post("/chat/completions")
async def chat_completions(request: Request):
    global call_count
    body = await request.json()
    messages = body.get("messages", [])
    user_content = messages[-1]["content"] if messages else ""

    call_count += 1
    time.sleep(random.uniform(0.1, 0.35))  # blocking — simulates real inference latency

    return {
        "id": "mock-cmpl-1",
        "object": "chat.completion",
        "model": body.get("model", "epoch-default"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": f"Mock reply to: {user_content}"},
                "finish_reason": "stop",
            }
        ],
    }