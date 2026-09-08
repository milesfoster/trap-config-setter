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
table.

`--varids` / `--target` (PLAN.md P1-4) and interrupt handling (P2-1). The
interrupt tests matter most: the finalize loop in `run_host` moved out of the
try block to make them possible, and the failure they guard against is silent -
an unexamined varid reported as `ok` would read as a converged fleet.

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


# ---------------------------------------------------------------------------
# P1-4: --varids and --target
# ---------------------------------------------------------------------------

def run_cli(argv):
    """main() with stdout captured. Returns (exit_code, stdout)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = ts.main(argv)
    return code, buffer.getvalue()


def cli_stderr(testcase, argv):
    """main() expected to exit via parser.error. Returns stderr."""
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        with testcase.assertRaises(SystemExit):
            ts.main(argv)
    return buffer.getvalue()


class VaridFilterTests(unittest.TestCase):
    """--varids scopes a run without hand-writing a schema file."""

    def test_selects_by_full_varid(self):
        code, out = run_cli(["--device", "vip100g", "--varids", "400.0.0@i",
                             "--list"])
        self.assertEqual(code, 0)
        self.assertIn("400.0.0@i", out)
        self.assertNotIn("400.0.1@i", out)

    def test_selects_by_bare_address(self):
        # The @suffix adds nothing - the type is derived from the varid.
        code, out = run_cli(["--device", "vip100g", "--varids", "850.18",
                             "--list"])
        self.assertEqual(code, 0)
        self.assertIn("850.18@i", out)

    def test_selects_across_groups(self):
        code, out = run_cli(["--device", "vip100g", "--varids",
                             "400.0.0@i,850.18@i", "--list"])
        self.assertEqual(code, 0)
        self.assertIn("400.0.0@i", out)
        self.assertIn("850.18@i", out)

    def test_space_separated_is_accepted(self):
        code, out = run_cli(["--device", "vip100g", "--varids",
                             "400.0.0@i 850.18@i", "--list"])
        self.assertEqual(code, 0)
        self.assertIn("850.18@i", out)

    def test_matches_an_expanded_input(self):
        # Runs after expansion, so a token can name one specific input. This is
        # the case a pre-expansion filter could not serve.
        code, out = run_cli(["--device", "vip100g", "--ports", "64",
                             "--varids", "400.31.0@i", "--list"])
        self.assertEqual(code, 0)
        self.assertIn("400.31.0@i", out)
        self.assertIn("port 32", out)

    def test_empties_groups_are_dropped_from_the_header(self):
        code, out = run_cli(["--device", "vip100g", "--varids", "400.0.0@i",
                             "--list"])
        self.assertEqual(code, 0)
        self.assertIn("across video", out.splitlines()[0])
        self.assertNotIn("audio", out.splitlines()[0])

    def test_unknown_varid_is_an_error(self):
        # Silently selecting nothing would be the worst outcome here: the run
        # would report a clean SUCCESS having written nothing.
        message = cli_stderr(self, ["--device", "vip100g", "--varids",
                                    "400.0.99@i", "--list"])
        self.assertIn("400.0.99@i", message)

    def test_unknown_varid_named_alongside_a_good_one(self):
        message = cli_stderr(self, ["--device", "vip100g", "--varids",
                                    "400.0.0@i,999.9.9@i", "--list"])
        self.assertIn("999.9.9@i", message)
        self.assertNotIn("400.0.0@i;", message)


class TargetOverrideTests(unittest.TestCase):
    """--target overrides the schema target in either direction."""

    def _targets(self, argv):
        code, out = run_cli(argv)
        self.assertEqual(code, 0)
        return out

    def test_overrides_to_true(self):
        # vip100g's video group targets 0; --target 1 must win.
        out = self._targets(["--device", "vip100g", "--varids", "400.0.0@i",
                             "--target", "1", "--list"])
        self.assertIn("-> 1", out)

    def test_accepts_the_exports_own_wording(self):
        out = self._targets(["--device", "vip100g", "--varids", "400.0.0@i",
                             "--target", "TRUE", "--list"])
        self.assertIn("-> 1", out)

    def test_overrides_to_false(self):
        # system targets 1; --target 0 must win.
        out = self._targets(["--device", "vip100g", "--varids", "850.18@i",
                             "--target", "0", "--list"])
        self.assertIn("-> 0", out)

    def test_force_false_is_an_alias_for_target_0(self):
        alias = self._targets(["--device", "vip100g", "--varids", "850.18@i",
                               "--force-false", "--list"])
        explicit = self._targets(["--device", "vip100g", "--varids", "850.18@i",
                                  "--target", "0", "--list"])
        self.assertEqual(
            [line.split("->")[1] for line in alias.splitlines() if "->" in line],
            [line.split("->")[1] for line in explicit.splitlines() if "->" in line],
        )

    def test_conflicting_flags_are_rejected(self):
        message = cli_stderr(self, ["--device", "vip100g", "--target", "1",
                                    "--force-false", "--list"])
        self.assertIn("mutually exclusive", message)

    def test_unusable_value_is_an_error_not_a_skip(self):
        message = cli_stderr(self, ["--device", "vip100g", "--varids",
                                    "400.0.0@i", "--target", "junk", "--list"])
        self.assertIn("not a usable value", message)


# ---------------------------------------------------------------------------
# P2-1: surviving Ctrl-C with a report
# ---------------------------------------------------------------------------

def make_traps(count, group="video", target=0):
    """A minimal trap set in the shape load_schema produces."""
    return [
        {
            "name": "param %d" % index, "group": group,
            "varid": "400.0.%d@i" % index, "fault_varid": "",
            "suffix": "i", "type": "integer",
            "target": target, "target_raw": str(target), "line": None,
            "per_port": False, "port_octet": 1, "port": None,
        }
        for index in range(count)
    ]


class InterruptingSetter(ts.TrapSetter):
    """A setter whose phases raise KeyboardInterrupt on demand.

    Ctrl-C cannot be delivered from a test, so the phases stand in for it: the
    interrupt surfaces from exactly where a real one would, inside _read or
    _write while a request is in flight.
    """

    def __init__(self, hosts, traps, interrupt_at=None, current=1, **options):
        ts.TrapSetter.__init__(self, hosts, traps, **options)
        self.interrupt_at = interrupt_at
        self.current = current
        self.reads = 0

    def _read(self, session, traps):
        self.reads += 1
        phase = "read" if self.reads == 1 else "verify"
        if self.interrupt_at == phase:
            raise KeyboardInterrupt()
        return dict(
            (t["varid"], {"value": self.current, "error": None}) for t in traps
        )

    def _write(self, session, traps):
        if self.interrupt_at == "write":
            raise KeyboardInterrupt()
        return dict(
            (t["varid"], {"value": t["target"], "error": None}) for t in traps
        )


class InterruptTests(unittest.TestCase):
    """An interrupted run must still report, and must not claim `ok`."""

    def setUp(self):
        # Discovery is covered elsewhere; stub it so no socket is opened.
        self._real_open = ts.HostSession.open

        def fake_open(session):
            session.proto = "https"
            session.endpoint = V15
            session.webeasy_version = "1.5"

        ts.HostSession.open = fake_open

    def tearDown(self):
        ts.HostSession.open = self._real_open

    def _run(self, interrupt_at, hosts=("10.0.0.1",), count=4, current=1):
        setter = InterruptingSetter(
            list(hosts), make_traps(count), interrupt_at=interrupt_at,
            current=current, delay=0,
        )
        return setter, setter.run()

    def test_interrupt_marks_the_host(self):
        _setter, results = self._run("write")
        self.assertTrue(results[0]["interrupted"])

    def test_interrupted_host_is_not_reported_unreachable(self):
        # It answered; it just did not finish. Conflating the two would send an
        # operator looking for a network fault.
        _setter, results = self._run("write")
        self.assertTrue(results[0]["reachable"])

    def test_interrupt_sets_the_setter_flag(self):
        setter, _results = self._run("write")
        self.assertTrue(setter.interrupted)

    def test_read_phase_interrupt_says_nothing_was_written(self):
        # Phase 1 only reads, so there is nothing to be partway through.
        _setter, results = self._run("read")
        self.assertIn("no write was issued", results[0]["error"])

    def test_write_phase_interrupt_does_not_claim_nothing_was_written(self):
        _setter, results = self._run("write")
        self.assertNotIn("no write was issued", results[0]["error"])

    def test_error_names_the_phase(self):
        _setter, results = self._run("verify")
        self.assertIn("verify", results[0]["error"])

    def test_unreached_varids_are_skipped_not_ok(self):
        # The whole point: calling an unexamined varid `ok` would report a
        # fleet as converged without having looked at it.
        _setter, results = self._run("write")
        statuses = set(t["status"] for t in results[0]["traps"])
        self.assertEqual(statuses, set(["skipped"]))

    def test_skipped_varids_say_a_write_may_have_gone_out(self):
        _setter, results = self._run("write")
        self.assertIn("not verified", results[0]["traps"][0]["detail"])

    def test_completed_run_still_reports_ok(self):
        # Regression guard: the finalize loop moved out of the try block, and
        # must behave exactly as before when nothing is interrupted.
        setter = InterruptingSetter(
            ["10.0.0.1"], make_traps(4, target=1), current=1, delay=0,
        )
        results = setter.run()
        self.assertEqual(
            set(t["status"] for t in results[0]["traps"]), set(["ok"]),
        )
        self.assertFalse(results[0]["interrupted"])

    def test_remaining_hosts_are_recorded_as_not_started(self):
        # Omitting them would silently shorten the recap.
        _setter, results = self._run("write", hosts=("a", "b", "c"))
        self.assertEqual(len(results), 3)
        self.assertTrue(results[1]["not_started"])
        self.assertTrue(results[2]["not_started"])

    def test_not_started_hosts_are_not_counted_unreachable(self):
        _setter, results = self._run("write", hosts=("a", "b"))
        totals = ts.summarize(results)
        self.assertEqual(totals["not_started"], 1)
        self.assertEqual(totals["unreachable"], 0)

    def test_totals_count_interrupted_hosts(self):
        _setter, results = self._run("write", hosts=("a", "b"))
        self.assertEqual(ts.summarize(results)["interrupted"], 1)

    def test_report_renders_an_interrupted_run(self):
        _setter, results = self._run("write", hosts=("a", "b"))
        text = ts.render_text_report({
            "meta": {
                "started": "-", "finished": "-", "duration_s": 0.0,
                "mode": "apply", "params_file": "-", "groups": ["video"],
                "group_counts": {"video": 4}, "trap_count": 4,
                "ports_note": "-", "chunk_size": 10, "delay": 0.0,
                "workers": 1, "retries": 1, "verify": True, "set_all": False,
                "warnings": [], "brief": False, "result": "INTERRUPTED",
            },
            "hosts": results,
            "totals": dict(ts.summarize(results)),
        })
        self.assertIn("INTERRUPTED", text)
        self.assertIn("NOT STARTED", text)
        self.assertIn("skipped", text)


class StatusVocabularyTests(unittest.TestCase):
    """The JSON report's shape must stay stable as statuses are added."""

    def test_skipped_is_in_the_status_order(self):
        self.assertIn("skipped", ts.STATUS_ORDER)

    def test_summarize_seeds_every_status(self):
        totals = ts.summarize([])
        for status in ts.STATUS_ORDER:
            self.assertEqual(totals[status], 0)

    def test_summarize_seeds_the_host_level_keys(self):
        totals = ts.summarize([])
        for key in ("unreachable", "interrupted", "not_started", "requests"):
            self.assertEqual(totals[key], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
