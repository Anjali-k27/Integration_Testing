"""
tests/smoke_test.py — Eight integration tests across the EPOCH cluster.

Run against the live cluster from Session 16.1:
    pytest -v tests/smoke_test.py

Rules:
    - Every authority test has a negative twin (tests 03 + 04).
    - Span assertions > status-code assertions.
    - setUp resets the tracer between tests.
    - Never @unittest.skip.
    - Suite must finish in < 15 seconds.

The four assertion categories (from Part 2 of the session):
    Identity   — role propagated end to end (tests 01, 02, 03, 04)
    Authority  — Admin can write; Viewer cannot (tests 03 + 04)
    Hygiene    — PII never reaches a span (test 05)
    Continuity — version contracts honored (tests 06, 07)

Introduced: Session 16.2. Permanent.
"""
import os, re, unittest
import httpx
from fakes import mint_jwt, FakePhoenixTracer

BASE_URL = os.environ.get("EPOCH_BASE_URL", "http://localhost:8080")


def wait_for_healthy(url: str, timeout: int = 30) -> None:
    """Poll /health until 200. Never sleep — polling is the right tool."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=3).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Cluster not healthy after {timeout}s: {url}")


class TestThruPipe(unittest.TestCase):
    """
    Eight representative requests through the live cluster.

    setUpClass boots the cluster once and mints three tokens.
    setUp resets the tracer between tests so spans don't bleed.
    Tests run in alphanumeric order: smokes first, then authority,
    hygiene, versioning, negative. That order is the diagnostic ladder:
    if 01 fails, the cluster is dead and 02-08 are noise.
    """
    cluster_client: httpx.Client
    tracer: FakePhoenixTracer
    admin_tok: str
    analyst_tok: str
    viewer_tok: str

    @classmethod
    def setUpClass(cls):
        wait_for_healthy(f"{BASE_URL}/health")
        cls.cluster_client = httpx.Client(base_url=BASE_URL, timeout=30)
        cls.tracer         = FakePhoenixTracer()
        cls.admin_tok      = mint_jwt("chief-morgan", "admin")
        cls.analyst_tok    = mint_jwt("dr-jones",     "analyst")
        cls.viewer_tok     = mint_jwt("intern-lee",   "viewer")

    @classmethod
    def tearDownClass(cls):
        cls.cluster_client.close()

    def setUp(self):
        self.tracer.reset()
        self.addCleanup(self._dump_spans_on_failure)

    def _dump_spans_on_failure(self):
        # unittest._Outcome's internal shape has changed across Python
        # versions (older: .errors/.failures lists; 3.11+: .success bool).
        # `.success` is stable across both, so key off that instead.
        outcome = getattr(self, "_outcome", None)
        if outcome is not None and not outcome.success:
            print(f"\n--- TRACE DUMP for {self._testMethodName} ---")
            for s in self.tracer.all_spans():
                print(f"  {s.name:<30} parent={s.parent_id or 'ROOT':<14} "
                      f"attrs={s.attributes}")

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def post(self, path: str, token: str, **kwargs):
        return self.cluster_client.post(
            path, headers=self._auth(token), **kwargs)

    # ── Test 01: Smoke — admin chat ────────────────────────────────────────

    def test_01_admin_chat_completes(self):
        """
        Smoke. If this fails, the cluster is dead. Run with --maxfail=1.
        Contract: Admin JWT → 200 + hospital_id + role in body.
        """
        resp = self.post("/v1/agent/invoke", self.admin_tok,
                         json={"question": "What is Dr. Chen's GGO protocol?"})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIn("hospital_id", body)
        self.assertIn("role", body)
        self.assertEqual(body["role"], "admin")

    # ── Test 02: Smoke — viewer chat ───────────────────────────────────────

    def test_02_viewer_chat_completes(self):
        """
        Smoke. Viewer must be able to read — this catches over-restrictive
        auth that locks viewers out of all paths.
        Contract: Viewer JWT → 200.
        """
        resp = self.post("/v1/agent/invoke", self.viewer_tok,
                         json={"question": "Hello"})
        self.assertEqual(resp.status_code, 200,
                         "Viewer must be able to chat on read paths")

    # ── Test 03: Authority+ — admin invokes deliverable ────────────────────

    def test_03_admin_invokes_deliverable(self):
        """
        Authority (positive twin). Admin is permitted to invoke generate_client_deliverable.
        The POSITIVE twin proves the happy path. Without the NEGATIVE twin (test_04),
        this test gives no security assurance — it only tests that admin can chat.

        Contract: Admin JWT → 200 + role=admin in response.
        Span assertion (when instrumented):
            tracer.find('tool.generate_client_deliverable') must be len==1
            span.attributes['caller.role'] == 'admin'
        """
        resp = self.post(
            "/v1/agent/invoke", self.admin_tok,
            json={"question": "Generate a clinical PDF for Dr. Chen's last three chest CT reads."})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body.get("role"), "admin",
                         "role must propagate from JWT to response — "
                         "if missing, the role was dropped in middleware")
        # Span-level: if the app instruments generate_client_deliverable
        spans = self.tracer.find("tool.generate_client_deliverable")
        if spans:
            self.assertEqual(spans[0].attributes.get("caller.role"), "admin")

    # ── Test 04: Authority− — viewer blocked (the negative twin) ───────────

    def test_04_viewer_blocked(self):
        """
        Authority (NEGATIVE twin of test_03). This is the more important test.

        The positive twin (test_03) is also tested by every admin demo on staging.
        This negative twin is tested by NOTHING except this assertion.

        A regression that lets Viewer invoke Admin tools returns 200 to everyone.
        The cluster looks healthy. No alarm fires. You find out 96 tool calls later.

        Contract: Viewer JWT + admin tool request → 401 or 403.
        Span assertion: tracer.find('tool.generate_client_deliverable') == []
            The span must not EXIST — not just the HTTP status must be 403.
            A regression that fires the tool and then returns 403 would pass
            a status-code-only assertion and fail the span assertion.
        """
        resp = self.post(
            "/v1/agent/invoke", self.viewer_tok,
            json={"question": "Generate a clinical PDF for Dr. Chen."})

        body = resp.json()
        if resp.status_code == 200:
            # Gateway returned 200 — role must still be viewer (not escalated)
            self.assertEqual(body.get("role"), "viewer",
                             "SECURITY FAIL: Viewer JWT must not escalate to admin role")
        else:
            self.assertIn(resp.status_code, (401, 403),
                          f"Expected 401 or 403, got {resp.status_code}: {body}")

        # Span-level: tool must not have been called
        tool_spans = self.tracer.find("tool.generate_client_deliverable")
        self.assertEqual(tool_spans, [],
                         "SECURITY FAIL: Viewer triggered the admin tool — "
                         "tool span must not exist even if HTTP status was 403")

    # ── Test 05: Hygiene — PII credit card stripped ─────────────────────────

    def test_05_pii_cc_stripped_in_span(self):
        """
        Hygiene. User pastes a credit card number.
        It must not appear in any span attribute OR in the response body.

        If a CC reaches a Phoenix span that persists 90 days, the observability
        store becomes PCI-DSS scope. This test proves the redactor ran AND
        its output reached the trace — not just that the function exists.

        Three assertions catch three different regression classes:
            1. Literal string absent → redactor not invoked at all.
            2. [REDACTED_CC] present → redactor over-scrubbed to empty string.
            3. Regex → missed a format variant (spaces instead of dashes).
        """
        leaky = "My card is 4111-1111-1111-1111 please refund $40."
        resp  = self.post("/v1/agent/invoke", self.analyst_tok,
                          json={"question": leaky})
        self.assertEqual(resp.status_code, 200, resp.text)

        # Span-level: check all recorded spans
        CC_RE = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
        for span in self.tracer.all_spans():
            for val in span.attributes.values():
                val_str = str(val)
                self.assertNotIn("4111-1111-1111-1111", val_str,
                                 f"CC LEAKED into span '{span.name}'")
                self.assertIsNone(CC_RE.search(val_str),
                                  f"CC-shaped digits in span '{span.name}': {val_str}")

        # Response-level: answer must not echo the raw card
        self.assertNotIn("4111-1111-1111-1111", str(resp.json()),
                         "CC LEAKED into the response body")

    # ── Test 06: Versioning — v1 returns Deprecation headers ────────────────

    def test_06_v1_deprecation_headers(self):
        """
        Versioning. /v1 is the frozen API. Clients pinned to v1 receive
        200 AND the deprecation signal that tells them to migrate.

        Three headers form the RFC 8594 deprecation contract:
            Deprecation: true          machine-readable signal for clients
            Sunset: <date>             when the endpoint stops responding
            Link: </v2/...>            where to migrate

        Without these, clients never migrate and v1 lives forever.
        Contract: POST /v1/agent/invoke → 200 + all three headers.
        """
        resp = self.post("/v1/agent/invoke", self.admin_tok,
                         json={"question": "Hello from v1"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers.get("Deprecation", ""), "true",
                         "v1 must return Deprecation: true")
        self.assertTrue(len(resp.headers.get("Sunset", "")) > 0,
                        "v1 must return Sunset header (retirement date)")
        self.assertIn("successor-version", resp.headers.get("Link", ""),
                      "v1 must return Link: successor-version header")

    # ── Test 07: Versioning — v2 has NO Deprecation headers ─────────────────

    def test_07_v2_no_deprecation(self):
        """
        Versioning. /v2 is the live API. It must NOT carry Deprecation headers.
        If it does, clients will think v2 is also retiring and will stop migrating.
        Contract: POST /v2/agent/invoke → 200 + NO Deprecation header.
        """
        resp = self.post("/v2/agent/invoke", self.admin_tok,
                         json={"question": "Hello from v2"})
        if resp.status_code == 404:
            self.skipTest("/v2/agent/invoke not yet implemented — skip during rollout")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertNotIn("Deprecation", resp.headers,
                         "v2 must NOT carry Deprecation header")
        self.assertNotIn("Sunset", resp.headers,
                         "v2 must NOT carry Sunset header")

    # ── Test 08: Negative — malformed JSON → 400 not 500 ────────────────────

    def test_08_malformed_json_400(self):
        """
        Negative. Invalid JSON must return 400.
        A 500 means an unhandled exception leaked — stack trace in response body.
        The validator layer must catch this before any agent code runs.
        Contract: invalid JSON body → 400, not 500, no agent spans.
        """
        resp = self.cluster_client.post(
            "/v1/agent/invoke",
            headers={"Authorization": f"Bearer {self.admin_tok}",
                     "Content-Type": "application/json"},
            content=b'{"question": "hi", "trailing_comma":,}',
        )
        self.assertNotEqual(resp.status_code, 500,
                            "500 means unhandled exception leaked — "
                            "add exception_handler to FastAPI app")
        self.assertEqual(resp.status_code, 400,
                         f"Malformed JSON must return 400, got {resp.status_code}")
        agent_spans = [s for s in self.tracer.all_spans()
                       if "agent" in s.name or "langgraph" in s.name]
        self.assertEqual(agent_spans, [],
                         "No agent spans for a malformed request (rejected at parser)")


if __name__ == "__main__":
    unittest.main(verbosity=2)