#!/usr/bin/env python3
"""Offline test suite for trapSetter. No device, no network.

    python3 test_trapSetter.py            # or: python3 -m unittest -v test_trapSetter

`unittest` rather than pytest, and %-formatting rather than f-strings, to keep
the tests runnable wherever the module itself runs (Python 3.6).

Covered so far
--------------
Endpoint discovery (`_check_endpoint`) and the auth path it selects. This is
the transport decision trapSetter duplicates from ptpMon, so it is the one most
likely to drift: it was pinned to WebEasy 1.5 alone until 2026-09-04, which
made every 1.6 device unreachable. These tests are the regression guard on that
table, and matter more than usual because `extras/` is gitignored and this code
has no git history to fall back on.

Still wanted (PLAN.md P3-1): `_coerce_target`, `_to_comparable`, `parse_export`,
`expand_ports`, `detect_per_port`, `map_response`, and the export ->
build_schema -> load_schema round-trip. Each drops in as a new TestCase below.
"""

import contextlib
import io
import os
import sys
import unittest

# Import the module under test from its own directory, so the suite runs from
# any working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

import trapSetter as ts


CGI = "/cgi-bin/cfgjsonrpc"
V16 = "/v.1.6/php/datas/cfgjsonrpc.php"
V15 = "/v.1.5/php/datas/cfgjsonrpc.php"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResponse(object):
    def __init__(self, status):
        self.status_code = status
        # requests treats anything under 400 as ok.
        self.ok = status < 400
        self.url = ""


class FakeSession(object):
    """Answers 200 for the paths in `available`, 404 for everything else.

    Records every path probed, in order, so a test can assert not just which
    endpoint won but that the ones before it were tried and the ones after it
    were not.
    """

    def __init__(self, available=(), raise_on=(), status=404):
        self.available = list(available)
        self.raise_on = list(raise_on)
        self.status = status
        self.calls = []

    def _answer(self, url):
        # "http://host/a/b" -> "/a/b"; a bare "http://host" -> "/", which is
        # what _check_proto probes.
        remainder = url.split("//", 1)[1]
        _host, slash, tail = remainder.partition("/")
        path = "/" + tail if slash else "/"
        self.calls.append(path)
        if path in self.raise_on:
            raise requests.ConnectionError("connection refused")
        return FakeResponse(200 if path in self.available else self.status)

    def get(self, url, **kwargs):
        return self._answer(url)

    def head(self, url, **kwargs):
        return self._answer(url)


def make_session(available=(), raise_on=(), status=404, pin=None):
    """A HostSession wired to a FakeSession, with an optional version pin."""
    setter = ts.TrapSetter([], [], webeasy_version=pin)
    host = ts.HostSession(setter, "10.0.0.1")
    host.session = FakeSession(available, raise_on, status)
    return host


# ---------------------------------------------------------------------------

class EndpointTableTests(unittest.TestCase):
    """The table itself, which the probe order depends on."""

    def test_newest_first(self):
        versions = [v for v, _path in ts._DELEGATE_ENDPOINTS]
        self.assertEqual(
            versions, sorted(versions, key=float, reverse=True),
            "_DELEGATE_ENDPOINTS must stay newest-first: probe order is "
            "load-bearing",
        )

    def test_ordered_sequence_not_a_dict(self):
        # Dict ordering is an implementation detail on 3.6, which this targets.
        self.assertIsInstance(ts._DELEGATE_ENDPOINTS, list)

    def test_paths_are_version_stamped(self):
        for version, path in ts._DELEGATE_ENDPOINTS:
            self.assertIn(version, path)


class CgiEndpointTests(unittest.TestCase):
    """The older session/login path wins outright when present."""

    def test_cgi_wins(self):
        host = make_session([CGI, V16, V15])
        self.assertEqual(host._check_endpoint("http"), (CGI, None))

    def test_cgi_short_circuits_delegate_probing(self):
        host = make_session([CGI, V16, V15])
        host._check_endpoint("http")
        self.assertEqual(host.session.calls, [CGI])

    def test_cgi_selects_cookie_auth(self):
        host = make_session([CGI])
        host.endpoint, host.webeasy_version = host._check_endpoint("http")
        self.assertFalse(host.uses_basic_auth)

    def test_non_200_falls_through_to_delegate(self):
        # A 403 on cfgjsonrpc is not a working endpoint; keep probing.
        host = make_session([V15], status=403)
        self.assertEqual(host._check_endpoint("http"), (V15, "1.5"))

    def test_connection_error_falls_through_to_delegate(self):
        host = make_session([V16], raise_on=(CGI,))
        self.assertEqual(host._check_endpoint("http"), (V16, "1.6"))


class DelegateEndpointTests(unittest.TestCase):
    """Version-stamped delegate paths, probed newest first."""

    def test_16_only_device_resolves(self):
        # The regression this suite exists for: 1.6-only devices were
        # unreachable while the 1.5 path was the only one known.
        host = make_session([V16])
        self.assertEqual(host._check_endpoint("http"), (V16, "1.6"))

    def test_15_only_device_still_resolves(self):
        host = make_session([V15])
        self.assertEqual(host._check_endpoint("http"), (V15, "1.5"))

    def test_15_only_device_probes_16_first(self):
        host = make_session([V15])
        host._check_endpoint("http")
        self.assertEqual(host.session.calls, [CGI, V16, V15])

    def test_newest_wins_when_both_answer(self):
        host = make_session([V16, V15])
        self.assertEqual(host._check_endpoint("http"), (V16, "1.6"))

    def test_delegate_selects_basic_auth(self):
        host = make_session([V16])
        host.endpoint, host.webeasy_version = host._check_endpoint("http")
        self.assertTrue(host.uses_basic_auth)

    def test_auth_path_keys_off_version_not_the_literal_path(self):
        # A future table row must not need a change in uses_basic_auth.
        host = make_session([])
        host.endpoint, host.webeasy_version = ("/v.9.9/php/datas/x.php", "9.9")
        self.assertTrue(host.uses_basic_auth)


class VersionPinTests(unittest.TestCase):
    """--webeasy-version narrows the probe without locking anything out."""

    def test_pin_selects_that_version(self):
        host = make_session([V16, V15], pin="1.5")
        self.assertEqual(host._check_endpoint("http"), (V15, "1.5"))

    def test_pin_skips_other_versions(self):
        host = make_session([V16, V15], pin="1.5")
        host._check_endpoint("http")
        self.assertNotIn(V16, host.session.calls)

    def test_pin_does_not_lock_out_cgi(self):
        # Pinning a delegate version must never strand an older device in the
        # same fleet: the cgi probe still runs first.
        host = make_session([CGI], pin="1.6")
        self.assertEqual(host._check_endpoint("http"), (CGI, None))

    def test_unpinned_probes_every_version(self):
        host = make_session([])
        with self.assertRaises(ts.RpcError):
            host._check_endpoint("http")
        self.assertEqual(host.session.calls, [CGI, V16, V15])


class EndpointFailureTests(unittest.TestCase):
    """Exhaustion errors carry the per-path status, which is the actionable bit."""

    def _message(self, **kwargs):
        host = make_session([], **kwargs)
        try:
            host._check_endpoint("http")
        except ts.RpcError as exc:
            return str(exc)
        self.fail("expected RpcError when no endpoint answers")

    def test_raises_when_nothing_answers(self):
        host = make_session([])
        self.assertRaises(ts.RpcError, host._check_endpoint, "http")

    def test_message_names_cgi_and_every_delegate_path(self):
        message = self._message()
        self.assertIn(CGI, message)
        self.assertIn(V16, message)
        self.assertIn(V15, message)

    def test_message_carries_status_per_path(self):
        # 404 everywhere means an unknown WebEasy version to add to the table.
        self.assertIn("404", self._message())

    def test_401_everywhere_is_legible_as_credentials(self):
        self.assertIn("401", self._message(status=401))

    def test_message_names_the_pin_when_it_matched_nothing(self):
        # Only reachable if a pin is ever allowed off-table; the CLI validates
        # against it, so this guards the belt-and-braces branch.
        host = make_session([])
        host.setter.delegate_endpoints = []
        try:
            host._check_endpoint("http")
        except ts.RpcError as exc:
            self.assertIn("--webeasy-version", str(exc))
        else:
            self.fail("expected RpcError with no delegate paths to probe")


class CliVersionTests(unittest.TestCase):
    """The pin is validated at startup, against the table."""

    def test_choices_come_from_the_table(self):
        parser = ts.build_parser()
        args = parser.parse_args(
            ["--hosts", "10.0.0.1", "--webeasy-version", "1.6"]
        )
        self.assertEqual(args.webeasy_version, "1.6")

    def test_unknown_version_is_rejected(self):
        parser = ts.build_parser()
        # argparse prints usage to stderr on the way out; swallow it so the
        # suite's own output stays readable.
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    ["--hosts", "10.0.0.1", "--webeasy-version", "1.4"]
                )
        self.assertIn("invalid choice", captured.getvalue())

    def test_default_is_unpinned(self):
        parser = ts.build_parser()
        args = parser.parse_args(["--hosts", "10.0.0.1"])
        self.assertIsNone(args.webeasy_version)


class SetterPinTests(unittest.TestCase):
    """TrapSetter turns the pin into the list HostSession probes."""

    def test_unset_probes_all(self):
        setter = ts.TrapSetter([], [])
        self.assertEqual(setter.delegate_endpoints, ts._DELEGATE_ENDPOINTS)

    def test_pin_narrows_to_one(self):
        setter = ts.TrapSetter([], [], webeasy_version="1.6")
        self.assertEqual(setter.delegate_endpoints, [("1.6", V16)])

    def test_empty_string_is_treated_as_unset(self):
        setter = ts.TrapSetter([], [], webeasy_version="")
        self.assertEqual(setter.delegate_endpoints, ts._DELEGATE_ENDPOINTS)

    def test_table_is_copied_not_aliased(self):
        # A mutation here must not leak into the module-level table.
        setter = ts.TrapSetter([], [])
        setter.delegate_endpoints.append(("0.1", "/nope"))
        self.assertNotIn(("0.1", "/nope"), ts._DELEGATE_ENDPOINTS)


class HostRecordTests(unittest.TestCase):
    """The report record surfaces which firmware each host resolved to."""

    def test_unreachable_host_reports_null_version(self):
        setter = ts.TrapSetter(["10.0.0.1"], [], timeout=(0.01, 0.01))
        setter.session = FakeSession([], raise_on=(CGI,))
        result = setter.run_host("10.0.0.1")
        self.assertFalse(result["reachable"])
        self.assertIsNone(result["webeasy_version"])

    def test_record_always_has_the_version_key(self):
        # The JSON report's shape must be stable whether or not discovery got
        # far enough to fill it in.
        setter = ts.TrapSetter(["10.0.0.1"], [], timeout=(0.01, 0.01))
        setter.session = FakeSession([], raise_on=(CGI,))
        result = setter.run_host("10.0.0.1")
        self.assertIn("webeasy_version", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
