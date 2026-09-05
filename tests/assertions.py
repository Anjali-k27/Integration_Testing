"""
tests/assertions.py — Reusable assertion predicates.

Treat this file with the same code-review bar as application code.
A bug here gives false confidence on every commit.

Usage:
    from assertions import assert_no_pii, assert_role_propagated
    assert_no_pii(span.attributes.get('input.user_message',''))
    assert_role_propagated(span, expected_role='admin')

Introduced: Session 16.2. Permanent.
"""
import re, unittest

_CC_PATTERN  = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
_SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def assert_no_pii(text: str, context: str = "") -> None:
    """Assert text has no CC or SSN patterns. Add patterns as product grows."""
    tc = unittest.TestCase(); tc.maxDiff = None
    tc.assertIsNone(_CC_PATTERN.search(text),
                    f"CC-shaped digits in {context}: {text[:80]}")
    tc.assertIsNone(_SSN_PATTERN.search(text),
                    f"SSN-shaped digits in {context}: {text[:80]}")


def assert_role_propagated(span, expected_role: str) -> None:
    """Assert span.attributes['caller.role'] matches expected_role."""
    tc = unittest.TestCase()
    tc.assertEqual(span.attributes.get("caller.role"), expected_role,
                   f"Span '{span.name}' has role="
                   f"{span.attributes.get('caller.role')!r}, "
                   f"expected {expected_role!r}. Role dropped in middleware?")


def assert_deprecation_headers(headers: dict) -> None:
    """Assert all three RFC 8594 deprecation headers are present."""
    tc = unittest.TestCase()
    tc.assertEqual(headers.get("Deprecation",""), "true",
                   "Missing Deprecation: true on deprecated endpoint")
    tc.assertTrue(len(headers.get("Sunset","")) > 0,
                  "Missing Sunset header")
    tc.assertIn("successor-version", headers.get("Link",""),
                "Missing Link: successor-version header")


def assert_no_deprecation_headers(headers: dict) -> None:
    """Assert no deprecation headers on a live (v2) endpoint."""
    tc = unittest.TestCase()
    tc.assertNotIn("Deprecation", headers,
                   "v2 must NOT carry Deprecation header")
    tc.assertNotIn("Sunset", headers,
                   "v2 must NOT carry Sunset header")