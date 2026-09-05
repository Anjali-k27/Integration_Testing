# EPOCH Cluster — Session 16.2

The automated handshake: `tests/smoke_test.py` fires eight representative
requests at a live, running EPOCH gateway and asserts on identity, authority,
hygiene, and versioning — the same four categories any client of this API
actually depends on. This directory is a self-contained two-service cluster
(`fastapi` gateway + a mock LiteLLM-compatible backend) built specifically so
the suite has something real to run against without provisioning GPUs,
Postgres, Redis, or live model-provider keys.

This README has been validated end-to-end against this exact directory: cold
`docker compose up -d --build` → `validate.sh` → `pytest -v tests/smoke_test.py`
→ all three [deliberate bug exercises](#deliberate-bug-exercises) reproduced
and reverted, run with no manual waiting in between. See
[Verified state](#verified-state).

---

## Architecture

```
                    ┌─────────────┐
  host:8080  ───►   │   fastapi   │  gateway — JWT auth, PII redaction, authority gate
                    └──────┬──────┘
                           │ container-name DNS: http://litellm:4000
                           ▼
                    ┌─────────────┐
                    │   litellm   │  mock OpenAI-compatible backend (services/mock_llm)
                    └─────────────┘
```

Only `fastapi` publishes a host port. `litellm` here is a small FastAPI stub
(`services/mock_llm/app.py`), not the real `ghcr.io/berriai/litellm` image —
it exists purely so `_handle_invoke` has something to call without needing
`GEMINI_API_KEY` / `OPENAI_API_KEY` or a GPU. Host port is **8080**, not the
usual 8000 — a separate EPOCH cluster from an earlier session already holds
8000 on this machine, and this project intentionally does not share ports,
a network, or any state with that one.

`services/fastapi/app/main.py` exposes:

| Route | Notes |
|---|---|
| `GET /health` | liveness probe |
| `POST /v1/agent/invoke` | frozen — adds `Deprecation: true`, `Sunset`, `Link` headers |
| `POST /v2/agent/invoke` | live — same handler, no deprecation headers |

Both versions share one `_handle_invoke`: verify JWT → parse body (400 on
malformed JSON, never a raw 500) → redact PII from the prompt → gate
admin-only tool keywords (`"clinical pdf"`, `"deliverable"`) behind
`role == "admin"` → call the mock LLM → redact PII from the answer → return
`{status, hospital_id, role, answer}`.

---

## Prerequisites

```bash
docker --version          # Docker Engine 24+
docker compose version    # Compose v2 — no hyphen
python3 -m venv .venv && source .venv/bin/activate
pip install pytest httpx PyJWT
```

Verified against Docker Engine 27.3.1 / Compose v2.30.3 on this machine.

---

## Cloning the repo

This directory is the git root itself — `docker-compose.yml`, `services/`,
`tests/`, `.github/`, etc. all live at the top level, with **no wrapper
subfolder to `cd` into**. That's deliberate, not incidental: this project
previously had everything nested one level deeper, in an `epoch-cluster/`
subfolder, which is exactly the kind of layout that causes two real problems
on clone — see [Verified state](#verified-state) for how that was found and
fixed. If you're setting up the remote for the first time:

```bash
# from this directory, one time
git init
git add .
git commit -m "EPOCH cluster — Session 16.2"
git remote add origin <your-remote-url>
git push -u origin main
```

Once it has a remote, anyone else picks it up the normal way, and lands
directly in a working tree with no extra `cd`:

```bash
git clone <your-remote-url>
cd <the-directory-git-created>   # docker-compose.yml is right here
```

`.env` and `.venv/` are git-ignored (see `.gitignore`) — **a fresh clone has
no secrets in it.** Recreate `.env` yourself before the cluster will boot;
the version in this directory already has working demo defaults (JWT secret
matching `tests/fakes.py`, no real provider keys needed since `litellm` here
is a mock). `COMPOSE_PROJECT_NAME=epoch-cluster` is set inside `.env` itself
— container names (`epoch-cluster-fastapi-1`, etc.) stay stable no matter
what you name the folder you cloned into, since Compose's default naming
(directory basename) would otherwise silently change per-clone.

---

## Quick start

```bash
# from this directory
docker compose up -d --build
bash validate.sh
pytest -v tests/smoke_test.py
```

Expected `validate.sh` output:

```
Checking fastapi gateway...
  fastapi: healthy
Cluster is up.
```

Expected `pytest` output:

```
collected 8 items

smoke_test.py::TestThruPipe::test_01_admin_chat_completes        PASSED
smoke_test.py::TestThruPipe::test_02_viewer_chat_completes       PASSED
smoke_test.py::TestThruPipe::test_03_admin_invokes_deliverable   PASSED
smoke_test.py::TestThruPipe::test_04_viewer_blocked              PASSED
smoke_test.py::TestThruPipe::test_05_pii_cc_stripped_in_span     PASSED
smoke_test.py::TestThruPipe::test_06_v1_deprecation_headers      PASSED
smoke_test.py::TestThruPipe::test_07_v2_no_deprecation           PASSED
smoke_test.py::TestThruPipe::test_08_malformed_json_400          PASSED

8 passed in 0.2s
```

### Everyday commands

```bash
docker compose ps                 # status + health of both services
docker compose logs -f fastapi    # tail the gateway
docker compose down               # stop + remove containers
docker compose build fastapi && docker compose up -d fastapi   # apply a code change
```

---

## The eight tests

| # | Test | Category | Pass criteria |
|---|------|----------|----------------|
| 1 | `test_01_admin_chat_completes` | Smoke | 200 + `hospital_id` + `role` in body |
| 2 | `test_02_viewer_chat_completes` | Smoke | 200 (read-only permitted) |
| 3 | `test_03_admin_invokes_deliverable` | Authority+ | 200 + `role=admin` propagated |
| 4 | `test_04_viewer_blocked` | Authority− | 401/403, or 200 with role NOT escalated |
| 5 | `test_05_pii_cc_stripped_in_span` | Hygiene | No CC digits in spans or response body |
| 6 | `test_06_v1_deprecation_headers` | Versioning | `Deprecation: true` + `Sunset` + `Link` |
| 7 | `test_07_v2_no_deprecation` | Versioning | `Deprecation` absent from headers |
| 8 | `test_08_malformed_json_400` | Negative | 400, never 500 |

Tests 3 and 4 are the required positive/negative twin pair for the
authority category — see [Known gotchas](#known-gotchas-found-during-this-sanity-pass)
below for a real limitation found in test 4 while validating this.

---

## Fixed bugs in the mission's reference code

Two bugs in the smoke-test code as originally specified would have stopped
the suite from ever running or reporting correctly. Both are fixed in this
directory:

1. **`test_08_malformed_json_400` passed `headers=` twice** to the same
   `.post()` call (once via a helper, once inline for `Content-Type`) — a
   `SyntaxError: keyword argument repeated`, which fails collection for the
   entire file, not just that one test. Fixed by merging both headers into
   a single dict.
2. **`_dump_spans_on_failure` read `self._outcome.errors` / `.errors`.failures`**,
   which was `unittest._Outcome`'s shape on Python ≤3.10. On this machine's
   Python 3.12, `_Outcome` only exposes `.success` (a bool) — the original
   code raised `AttributeError` on every single failing test, which then
   *masked the real assertion failure* in the reported traceback. Fixed by
   keying off `.success`, which is stable across both shapes.

---

## Known gotchas (found during this sanity pass)

1. **The span-level assertions in `test_04` and `test_05` are structurally
   inert in this scaffold.** `FakePhoenixTracer` lives in the *test process*
   — it is never fed real spans from the `fastapi` container, because
   nothing in `main.py` calls back into the test's in-memory tracer (it
   couldn't; they're separate OS processes). Every `tracer.find(...)` call
   in the suite returns `[]` unconditionally, and every span-based assertion
   passes trivially regardless of what the container actually did.

   This was **not** a hypothetical concern — it was reproduced directly. With
   `test_04` present and the authority gate in `main.py` fully disabled
   (`if False and requires_admin_tool(...)`, permitting Viewer to invoke the
   admin-only tool and receive a real `200`), the full suite still reported
   **8 passed**. `test_04`'s HTTP branch only fires when the status code
   isn't 200, and its role check only catches a bug that also *escalates*
   the role field — neither is true for "permitted through with the correct
   role." The span assertion, which is the one comment in the test file that
   explicitly promises to catch exactly this ("the span must not exist even
   if HTTP status was 403"), cannot catch it here because it isn't wired to
   anything real.

   **Practical implication:** in this two-process scaffold, `test_04` only
   reliably catches an authority regression that *also* changes the HTTP
   status code away from 200. A regression that quietly widens what triggers
   a 200 — as opposed to what a 200 contains — needs either a real Phoenix
   collector the test can query, or an assertion on the mock LLM backend
   actually being called (which this mock doesn't currently log), to be
   caught. Session 16.1 does not yet wire real OTel/Phoenix into `main.py`;
   until it does, treat `test_04`'s span assertions as documentation of
   intent, not as working coverage.

2. **PII redaction is two separate call sites, and only one is exercised by
   the mock LLM round-trip.** `redact_pii()` runs on the way in (over the
   prompt, before it's sent anywhere) and `redact_json()` runs on the way
   out (over whatever the LLM backend returned). Because the mock backend
   only ever echoes what it was sent, disabling *just* the inbound
   `redact_pii()` call still passes `test_05` — the outbound `redact_json()`
   scrubs the mock's echo before it reaches the response body, even though
   the raw card number was, in a real deployment, already sent to a
   third-party model provider and potentially logged there. Reproducing a
   real, response-body-visible leak (as in
   [Exercise 3](#deliberate-bug-exercises) below) requires disabling *both*
   call sites, not just one — a useful reminder that "PII never reaches
   the response" and "PII never reaches an upstream LLM" are two different
   guarantees, and this suite only directly tests the first.

---

## Deliberate bug exercises

Three intentional breakages, run against this exact directory, observed, and
reverted — the cluster is currently in the clean, working state (`main.py`
and `smoke_test.py` were diffed byte-for-byte against a pre-exercise backup
after reverting; both are identical).

| # | Bug | Change | Observed result | Lesson |
|---|-----|--------|------------------|--------|
| 1 | Role-strip | `services/fastapi/app/main.py`: after `claims = verify_jwt(auth)`, add `claims = {k: v for k, v in claims.items() if k != "role"}` | **7 of 8 failed** (`test_01` failed first with `500`, since `claims["role"]` is read unconditionally later in the same request; only `test_08` — which never reaches that code path — stayed green) | A single stripped claim breaks every downstream consumer of it at once. Because `role` is read directly (not `.get()`) in the response body, this fails loud and immediately rather than silently — the diagnostic ladder (01 fails ⇒ stop, don't debug 02–08) worked exactly as intended. |
| 2 | Missing negative twin, then authority bypass | Disabled `test_04` (renamed the method so pytest wouldn't collect it) → **7/7 green**. Restored `test_04`, then disabled the authority gate in `main.py` (`if False and requires_admin_tool(...)`) so Viewer can invoke the admin-only tool and gets a real `200` | **8/8 green — with `test_04` present and collected.** See [Known gotchas](#known-gotchas-found-during-this-sanity-pass) #1: the span assertion that was supposed to catch this is inert in this scaffold, and the HTTP/role checks don't fire for a bypass that returns 200 without escalating the role field. | The mission's stated lesson — "a regression that lets Viewer invoke Admin tools returns 200 to everyone, no alarm fires" — is not just a hypothetical here. It's exactly what was reproduced, and it slipped past the negative twin itself, not just past a suite missing one. A negative-twin test is only as strong as what it can actually observe; an assertion on a tracer nothing feeds is not coverage. |
| 3 | PII leak in response body | `services/fastapi/app/main.py`: removed `redact_pii()` on the inbound prompt **and** commented out `redact_json(answer)` on the outbound response | **1 of 8 failed** — `test_05` only, with the raw card number visible directly in the assertion failure: `'4111-1111-1111-1111' unexpectedly found in "...content': 'Mock reply to: My card is 4111-1111-1111-1111 please refund $40.'..."` | Exactly the intended blast radius: disabling PII hygiene breaks only the hygiene test, nothing else — which is what makes a scoped hygiene regression easy to miss without a dedicated test for it. Also confirms both redaction call sites were actually necessary to reproduce a real leak (see gotcha #2 above) — removing only one of them was silently absorbed by the other. |

Revert each change after reproducing it — this is already done; no bug is
currently present in `services/fastapi/app/main.py` or `tests/smoke_test.py`.

### Verified state

The following was run end-to-end against this directory as a full sanity
check:

```
docker compose build fastapi && docker compose up -d fastapi
bash validate.sh                          # fastapi: healthy
pytest -v tests/smoke_test.py             # 8 passed
# ... three bug exercises above, each rebuilt, observed, reverted ...
diff <pre-exercise backup> main.py        # identical
diff <pre-exercise backup> smoke_test.py  # identical
docker compose build fastapi && docker compose up -d fastapi
bash validate.sh && pytest -v tests/smoke_test.py   # 8 passed, again
```

Result: cluster healthy, suite green, both files confirmed byte-identical
to their pre-exercise state.

### Flattening the directory (found during this sanity pass)

This project previously lived one level deeper, in an `epoch-cluster/`
subfolder inside the git root. That nesting causes two concrete problems for
anyone cloning the repo, both reproduced directly against this project
before being fixed:

1. **Every command in this README (and the CI workflow) assumes
   `docker-compose.yml` is right where you land after `cd` into the clone.**
   With the extra subfolder, a first-time clone following the Quick Start
   commands verbatim from the repo root fails immediately —
   `docker compose up -d --build` reports no `docker-compose.yml` found —
   because the file was actually one `cd epoch-cluster` further in, a step
   the README never told anyone to take.
2. **The CI workflow silently pinned the old nesting into automation.**
   `.github/workflows/smoke.yml` had `working-directory: epoch-cluster` on
   every step. Flattening the tree without also fixing those three lines
   would have made local runs work while CI kept failing (or worse, kept
   silently checking out and running the wrong path if a differently-named
   subfolder was ever reintroduced) — this was caught and fixed as part of
   the same pass, not left as a follow-up.

Fixed with:

```
docker compose down                       # stop the nested-layout containers
mv epoch-cluster/.[!.]* epoch-cluster/* .  # move everything, dotfiles included, up one level
rmdir epoch-cluster
# .github/workflows/smoke.yml: removed all three `working-directory: epoch-cluster` lines
docker compose up -d --build && bash validate.sh && pytest -v tests/smoke_test.py
```

Re-run from the new flat location: cluster healthy, **8 passed**, identical
to the pre-flatten result — the move changed nothing about behavior, only
where the files live.

A related, smaller issue surfaced in the same pass: Compose derives its
default project name from the **containing folder's name** when none is
set. Before this fix, containers came up as `session5-fastapi-1` (named
after this machine's folder) rather than `epoch-cluster-fastapi-1` — so two
people cloning the same repo into differently-named folders would get
different container names, breaking any tooling, alias, or muscle-memory
`docker exec <name>` built around one of them. Fixed by setting
`COMPOSE_PROJECT_NAME=epoch-cluster` directly in `.env` (and `.env.ci`),
verified by tearing down, rebuilding, and confirming `docker compose ps`
reports `epoch-cluster-fastapi-1` / `epoch-cluster-litellm-1` regardless of
the folder name.

No `.gitignore` existed before this pass either — a fresh `git add .` would
have committed `.env` (secrets), `.venv/`, `__pycache__/`, and
`.pytest_cache/`. Added one covering all four.

---

## Directory structure

Flat at the repo root — no wrapper folder to `cd` into after cloning:

```
.
├── .env                          # secrets — demo defaults, matches tests/fakes.py
├── .env.ci                       # CI environment, no real secrets
├── .gitignore                    # .env, .venv/, __pycache__/, .pytest_cache/
├── docker-compose.yml            # 2 services: fastapi (host:8080) + litellm (mock)
├── validate.sh                   # health check
├── README.md                     # this file
├── .github/workflows/smoke.yml   # CI: boot cluster, run smoke suite
│
├── services/
│   ├── fastapi/
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── app/
│   │       ├── main.py           # /health, /v1|/v2 agent/invoke
│   │       └── epoch_auth.py     # verify_jwt()
│   │
│   └── mock_llm/                 # stands in for the real LiteLLM proxy
│       ├── Dockerfile
│       ├── requirements.txt
│       └── app.py
│
└── tests/
    ├── fakes.py                  # mint_jwt() + FakePhoenixTracer
    ├── assertions.py             # reusable assertion predicates
    └── smoke_test.py             # the eight tests
```

---

## Final checklist

- [x] `tests/fakes.py` — JWT claims match `epoch_auth.py` exactly (shared secret, issuer, audience)
- [x] `tests/smoke_test.py` — 8 tests, ordered smoke → authority → hygiene → versioning → negative
- [x] Every authority test has its negative twin (03 + 04)
- [x] `test_08` asserts 400, never 500, on malformed JSON
- [x] `/v1/agent/invoke` carries `Deprecation` + `Sunset` + `Link`; `/v2/agent/invoke` carries neither
- [x] No `@unittest.skip` anywhere
- [x] Suite runs in well under 15 seconds (~0.2s)
- [x] Two reference-code bugs found and fixed (duplicate `headers=` kwarg; `_outcome` API drift)
- [x] All three deliberate bug exercises reproduced, observed, and reverted
- [x] Flat at the repo root — no `epoch-cluster/` wrapper subfolder, no extra `cd` after cloning
- [x] `.github/workflows/smoke.yml` has no stale `working-directory: epoch-cluster` references
- [x] `COMPOSE_PROJECT_NAME=epoch-cluster` pinned in `.env`/`.env.ci` — container names stable across clones
- [x] `.gitignore` present — `.env`, `.venv/`, `__pycache__/`, `.pytest_cache/` won't be committed
- [x] One real coverage gap found and documented (span assertions inert without real Phoenix wiring)
- [x] Cluster running, `validate.sh` green, `pytest` green, both source files confirmed unmodified from clean state

---

## What's next — Session 16.3

Per the handoff notes for this suite:

- **Keep this cluster running** (`docker compose up -d`) between sessions —
  16.3 wraps the same eight request patterns from `tests/smoke_test.py` into
  a **Locust `HttpUser`** and drives them at **50× concurrency**, rather than
  the one-shot pytest run used here. Re-use `mint_jwt()` and the same
  `EPOCH_BASE_URL` (`http://localhost:8080` for this directory) instead of
  re-deriving token minting from scratch.
- Before starting 16.3, re-run `bash validate.sh` and
  `pytest -v tests/smoke_test.py` to confirm the cluster is picked up from a
  known-good state — don't assume it's still healthy just because it was
  last time you looked.
- Carry the [known gotcha](#known-gotchas-found-during-this-sanity-pass)
  about `test_04`'s inert span assertions into 16.3's load test design: a
  50×-concurrency authority check that only asserts on HTTP status codes
  will have the exact same blind spot demonstrated here. If 16.3 wants to
  actually verify Viewer can't invoke admin tools under load — not just that
  requests return *a* status code — it needs either a real span sink the
  load test can query afterward, or an assertion the mock LLM backend itself
  was never called for a blocked request (e.g. a request counter in
  `services/mock_llm/app.py`).
- This mock `litellm` backend (`services/mock_llm/`) has no request logging
  or per-model routing — if 16.3 wants to assert "the tool call never
  reached the backend" rather than only "the client got a 403," that logging
  needs to be added first.

Beyond 16.3, the spec doesn't yet document later sessions in this track —
treat anything past 16.3 as unconfirmed until its own materials show up.
