#!/usr/bin/env python3
"""
trapSetter - bulk trap/notify configuration writer for Evertz WebEasy devices.

A command-line counterpart to ptpMon. Where ptpMon *reads* PTP state from the
`cfgjsonrpc` web service, trapSetter *writes* trap-enable varids to a target
value across a fleet, then produces an Ansible-style report of what changed,
what was already correct, and what failed.

Parameter source
----------------
The varids and their target values are not hardcoded. At runtime they come from
the JSON reference schema at `trap-params/<device>.json`, in which each group is
a flat name -> varid mapping:

    {
      "device": "vip100g",
      "port_count": null,
      "groups": {
        "video":  {"target": 0, "per_port": true, "port_octet": 1,
                   "params": {"Loss of Video": "400.0.0@i", ...}},
        "system": {"target": 1, "params": {"NTP Error": "850.18@i", ...}}
      }
    }

The target sits once per group because that is how these notify sections are
configured in practice - a whole section is enabled or disabled together. A
group whose members disagree carries a "target_overrides" object of
name -> value, which wins over the group target for those names.

A parameter's RPC type is derived from its own varid suffix (@i -> integer), not
stored in the schema, so there is one less field to keep in sync and a group
holding mixed types needs no special handling.

Per-port expansion
------------------
An export describes exactly one input. On a device with many inputs the same
parameter repeats per input in the port octet:

    530.0.0@i   channel 1 audio loss, input 1
    530.1.0@i   channel 1 audio loss, input 2

`port_count` at the top of the schema is that input count, and it is optional:

    null (or absent, or 1)  base varids only - the export as written
    64                      every per_port group replicated across 64 inputs

`--ports N` overrides it per run. Only groups marked `per_port` expand, which
is what keeps chassis-level traps from being multiplied: on the vip100g,
port_count 64 takes video 4 -> 256 and audio 64 -> 4096 while system stays at
27, for 4379 parameters. Expansion is port-major - every parameter for input 1,
then input 2 - so a run interrupted partway leaves the inputs it reached fully
configured rather than leaving all 64 half-done.

`--build-schema` guesses `per_port` per group: it requires every varid in the
group to be three octets with an identical middle octet, the signature of one
input exported at index 0. The guess is written into the schema so it is
visible and correctable rather than re-inferred each run.

`port_octet` (default 1) says which octet to rewrite. It must have an index
octet after it - a per-port varid is <base>.<port>.<index>. Rewriting a
trailing octet would walk across unrelated parameters instead of across inputs,
so that case is refused with a warning and the parameter is left unexpanded.

Mind the pacing cost: 4379 parameters is ~1314 requests per host, or about 11
minutes at the default 0.5s delay. The startup banner states the estimate, and
--brief keeps the report to just the rows that were not already correct.

Schemas are generated from the device's exported Notify page, not written by
hand:

    python3 trapSetter.py --device vip100g --build-schema

That reads `trap-params/<device>.csv` (or the older tab-separated `.txt`),
writes the JSON, and immediately reloads it to confirm it round-trips to an
identical parameter set. Both export shapes are accepted: varid cells are
recognised by shape rather than column position, so the `.txt` flavour that
also carries a Control Fault Value column parses without a separate code path.
Only the *Control Trap Value* is ever written.

Section headers in an export ("Video Notify") carry no varids; they open a
group whose key is the header's first word lowercased - so `--groups
video,audio` works for any device, including groups this module has never seen.

The schema is the file to hand-edit when a device needs a varid added, removed,
or given a different target. Text exports are conversion input only; if no
schema exists yet the text file is still read directly, with a note on stderr.

Run phases, per host - this is what makes the report trustworthy:

    1. GET  every targeted varid          -> current value
    2. SET  only the varids that differ   -> skipped entirely under --check
    3. GET  the varids just written       -> authoritative verification

Because step 3 re-reads the device, "changed" in the report means the device
confirmed the new value, not merely that the RPC returned 200. Step 1 makes the
run idempotent: a varid already at its target is reported `ok` and never
written, so a re-run against a converged fleet issues no writes at all.

Every request is chunked (default 10 parameters, the WebEasy server's
comfortable ceiling) and paced (default 0.5s between requests to the same
device) to avoid overloading the target's web service.

Usage
-----
    # Dry run first - reports what would change, writes nothing
    python3 trapSetter.py --device vip100g --hosts 172.17.223.93 --check

    # Apply, one host at a time, report to file
    python3 trapSetter.py --device vip100g --hosts-file fleet.txt \
        --user root --password evertz --report run.txt --json run.json

    # Apply to all 64 inputs, not just the base varids
    python3 trapSetter.py --device vip100g --hosts 172.17.223.93 --ports 64 --check

    # Convert a new device export into the JSON reference schema
    python3 trapSetter.py --device vip100g --build-schema

    # Just validate the parameter set - no network at all
    python3 trapSetter.py --device vip100g --list

Exit status is 0 when every targeted varid on every host ended at its target
value, and 1 if anything failed or a host was unreachable.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor

import requests
import urllib3
from requests.adapters import HTTPAdapter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# The WebEasy server degrades quickly under wide parameter sets. 10 is the
# safe ceiling and is enforced, not merely defaulted.
MAX_CHUNK_SIZE = 10
DEFAULT_CHUNK_SIZE = 10
DEFAULT_DELAY_S = 0.5
DEFAULT_WORKERS = 1
DEFAULT_RETRIES = 1
DEFAULT_TIMEOUT = (3, 10)

# The older session/login endpoint. Probed first, and the only path that
# authenticates with a cookie rather than per-request Basic auth.
_CGI_ENDPOINT = "/cgi-bin/cfgjsonrpc"

# WebEasy delegate endpoints, newest firmware first. _check_endpoint probes
# these in order when _CGI_ENDPOINT is absent, so a mixed-firmware fleet
# resolves itself with no configuration. Add a row as new WebEasy versions
# ship - nothing else in this module keys off the literal path.
#
# Kept in sync with ptpMon's _DELEGATE_ENDPOINTS; that module is upstream for
# every transport decision here.
#
# A list of pairs rather than a dict: probe order is load-bearing and dict
# ordering is only an implementation detail on Python 3.6, which this targets.
_DELEGATE_ENDPOINTS = [
    ("1.6", "/v.1.6/php/datas/cfgjsonrpc.php"),
    ("1.5", "/v.1.5/php/datas/cfgjsonrpc.php"),
]

# A varid is a dotted address plus a type suffix: 400.0.0@i, 782.1@s
_VARID_RE = re.compile(r"^\d+(?:\.\d+)*@([a-z])$")

# varid suffix -> the "type" string the JSON-RPC service expects.
_TYPE_BY_SUFFIX = {
    "i": "integer",
    "d": "integer",
    "s": "string",
    "b": "boolean",
    "f": "float",
}

# Which dotted octet carries the port/input index. On this device family a
# per-port varid is <base>.<port>.<index>, so the middle octet. Overridable per
# group in the schema for a device that numbers them elsewhere.
DEFAULT_PORT_OCTET = 1

_TRUTHY = {"true", "t", "yes", "y", "on", "enable", "enabled", "1"}
_FALSEY = {"false", "f", "no", "n", "off", "disable", "disabled", "0"}

# Per-varid outcomes, ordered as the recap table prints them. "skipped"
# appears only in an interrupted run: the varid was never reached, which
# must not be reported as "ok".
STATUS_ORDER = (
    "ok", "changed", "would-change", "failed", "unresolved", "skipped",
)


# ---------------------------------------------------------------------------
# Params file parsing
# ---------------------------------------------------------------------------

def _coerce_target(raw, suffix):
    """Turn a "Trap Value to Set" cell into the value to send, or None.

    None means the row carries no usable target; the caller surfaces that as a
    parse warning rather than dropping it silently, so a typo in the export is
    visible in the report instead of invisible in the diff.
    """
    text = raw.strip()
    if not text:
        return None

    if suffix in ("i", "b", "d", "f"):
        low = text.lower()
        if low in _TRUTHY:
            return 1
        if low in _FALSEY:
            return 0
        try:
            return int(text, 0)
        except ValueError:
            pass
        if suffix == "f":
            try:
                return float(text)
            except ValueError:
                return None
        return None

    return text


def _to_comparable(value, suffix):
    """Normalize a device-reported value for comparison against a target.

    Firmware is inconsistent about whether an @i control reads back as 0, "0"
    or false, so every flavour folds to an int before comparing. None means
    "no comparable value" and never compares equal to a target.
    """
    if value is None:
        return None

    if suffix in ("i", "b", "d"):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        text = str(value).strip()
        if not text:
            return None
        low = text.lower()
        if low in _TRUTHY:
            return 1
        if low in _FALSEY:
            return 0
        try:
            return int(text, 0)
        except ValueError:
            return None

    if suffix == "f":
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    text = str(value).strip()
    return text if text else None


def _read_text(path):
    """Read a device export, tolerating the encodings Excel actually emits.

    The CSV exports arrive cp1252: the filler cells in a section-header row are
    non-breaking spaces (0xA0), which is not valid UTF-8 and hard-fails a plain
    open(). Tried in order so a genuinely UTF-8 file is never mangled.
    """
    with open(path, "rb") as handle:
        raw = handle.read()

    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    return raw.decode("utf-8", "replace")


def _iter_export_rows(path):
    """Yield (lineno, cells) from a .csv or tab-separated .txt export.

    Cells are stripped of whitespace and of the non-breaking spaces that pad
    section-header rows, so a header row reduces to just its name.
    """
    text = _read_text(path)

    if path.lower().endswith(".csv"):
        # csv.reader handles the quoted heading cell containing a newline.
        rows = csv.reader(io.StringIO(text))
    else:
        rows = (line.split("\t") for line in text.splitlines())

    for lineno, row in enumerate(rows, 1):
        yield lineno, [cell.replace("\xa0", " ").strip() for cell in row]


def parse_export(path):
    """Parse a WebEasy trap export (.csv or .txt) into (traps, warnings).

    Column-position agnostic, because the exports disagree: the original .txt
    carries both a Control Trap Value and a Control Fault Value column, while
    the .csv keeps only the trap column. Varid cells are recognised by shape -
    the first is the trap varid, a second (if any) is the fault varid, and the
    target is the trailing non-varid cell. Both shapes parse with no per-format
    branch, and an export that adds a column still works.

    This is the conversion path, used by --build-schema. Runtime reads JSON.
    """
    traps = []
    warnings = []
    group = "ungrouped"

    for lineno, cells in _iter_export_rows(path):
        if not cells:
            continue

        name = cells[0]
        populated = [cell for cell in cells[1:] if cell]

        if not name and not populated:
            continue

        # A name with nothing beside it is a section header, not a trap.
        if name and not populated:
            group = name.split()[0].lower()
            continue

        varids = [cell for cell in cells[1:] if _VARID_RE.match(cell)]

        if not varids:
            # The export's own wrapped column headings land here, as does any
            # genuinely malformed row. Only complain about the latter.
            if len(populated) >= 2 and any("@" in cell for cell in populated):
                warnings.append(
                    "%s line %d: no recognisable varid for %r"
                    % (os.path.basename(path), lineno, name)
                )
            continue

        varid = varids[0]
        suffix = _VARID_RE.match(varid).group(1)

        # The target is the last populated cell that is not itself a varid.
        trailing = [cell for cell in cells[1:] if cell and cell not in varids]
        target_raw = trailing[-1] if trailing else ""
        target = _coerce_target(target_raw, suffix)

        if target is None:
            warnings.append(
                "%s line %d: no usable target value (%r) for %r [%s] - skipped"
                % (os.path.basename(path), lineno, target_raw, name, varid)
            )
            continue

        traps.append({
            "name": name,
            "group": group,
            "varid": varid,
            "fault_varid": varids[1] if len(varids) > 1 else "",
            "suffix": suffix,
            "type": _TYPE_BY_SUFFIX.get(suffix, "string"),
            "target": target,
            "target_raw": target_raw,
            "line": lineno,
            "per_port": False,
            "port_octet": DEFAULT_PORT_OCTET,
            "port": None,
        })

    # An export describes one input; per-port expansion is a schema concern.
    return traps, warnings, {"port_count": None}


def build_schema(traps, device, source):
    """Turn parsed export rows into the JSON reference schema.

    Each group is a flat {name: varid} dict - the schema is meant to be read
    and hand-edited, so the mapping an operator cares about is not buried under
    per-parameter objects. The target value sits once per group because that is
    how these pages are actually configured: a whole notify section is enabled
    or disabled together. A group whose rows disagree gets per-parameter
    entries in "target_overrides" rather than a silently wrong group target.
    """
    groups = OrderedDict()
    per_port = detect_per_port(traps)

    for trap in traps:
        group = groups.setdefault(
            trap["group"],
            OrderedDict([("target", None), ("params", OrderedDict())]),
        )
        group["params"][trap["name"]] = trap["varid"]

    for name, group in groups.items():
        if per_port.get(name):
            group["per_port"] = True
            group["port_octet"] = DEFAULT_PORT_OCTET
        members = [t for t in traps if t["group"] == name]
        majority = Counter(t["target"] for t in members).most_common(1)[0][0]
        group["target"] = majority

        overrides = OrderedDict(
            (t["name"], t["target"]) for t in members if t["target"] != majority
        )
        if overrides:
            group["target_overrides"] = overrides

    # port_count is the one knob an operator is expected to set by hand, so it
    # goes at the top of the file. null means "base varids only" - the export
    # describes a single input, and nothing infers an input count from it.
    return OrderedDict([
        ("device", device),
        ("source", os.path.basename(source)),
        ("generated", time.strftime("%Y-%m-%dT%H:%M:%S")),
        ("port_count", None),
        ("param_count", len(traps)),
        ("groups", groups),
    ])


def load_schema(path):
    """Load the JSON reference schema into (traps, warnings).

    Returns the same internal trap shape parse_export does, so everything
    downstream is identical whichever source was used. The RPC "type" is
    re-derived from each varid's own suffix rather than stored in the schema -
    one less field to keep in sync, and a group holding mixed types just works.
    """
    with open(path, "r") as handle:
        try:
            schema = json.load(handle, object_pairs_hook=OrderedDict)
        except ValueError as exc:
            raise ValueError("%s is not valid JSON: %s" % (path, exc))

    groups = schema.get("groups")
    if not isinstance(groups, dict):
        raise ValueError("%s has no 'groups' object" % path)

    traps = []
    warnings = []

    for group_name, group in groups.items():
        if not isinstance(group, dict) or "params" not in group:
            warnings.append(
                "group %r has no 'params' object - skipped" % group_name
            )
            continue

        group_target = group.get("target")
        overrides = group.get("target_overrides") or {}
        group_per_port = bool(group.get("per_port"))
        group_octet = int(group.get("port_octet", DEFAULT_PORT_OCTET))

        for name, varid in group["params"].items():
            match = _VARID_RE.match(str(varid).strip())
            if not match:
                warnings.append(
                    "%s/%s: %r is not a varid - skipped"
                    % (group_name, name, varid)
                )
                continue

            suffix = match.group(1)
            raw = overrides.get(name, group_target)

            # A schema may state a target as a JSON number (0) or as the
            # export's own wording ("FALSE"); accept either.
            if isinstance(raw, bool):
                target = int(raw)
            elif isinstance(raw, (int, float)):
                target = raw
            else:
                target = _coerce_target("" if raw is None else str(raw), suffix)

            if target is None:
                warnings.append(
                    "%s/%s: no usable target (%r) - skipped"
                    % (group_name, name, raw)
                )
                continue

            traps.append({
                "name": name,
                "group": group_name,
                "varid": str(varid).strip(),
                "fault_varid": "",
                "suffix": suffix,
                "type": _TYPE_BY_SUFFIX.get(suffix, "string"),
                "target": target,
                "target_raw": str(raw),
                "line": None,
                "per_port": group_per_port,
                "port_octet": group_octet,
                "port": None,
            })

    meta = {"port_count": schema.get("port_count")}

    return traps, warnings, meta


def load_params(path):
    """Load a params source, dispatching on extension.

    Returns (traps, warnings, meta); meta carries the schema-level port_count.
    """
    if path.lower().endswith(".json"):
        return load_schema(path)
    return parse_export(path)


def _set_octet(varid, octet, value):
    """Return varid with one dotted octet replaced, type suffix preserved."""
    address, _, suffix = varid.partition("@")
    parts = address.split(".")
    parts[octet] = str(value)
    return "%s@%s" % (".".join(parts), suffix)


def detect_per_port(traps):
    """Guess which groups are per-port, for --build-schema.

    A group qualifies when every member is a three-octet varid AND they all
    share the same middle octet - that is the signature of one input's worth of
    parameters exported at index 0. It deliberately rejects the system group,
    whose varids are a mix of four-octet network-port addresses and two-octet
    chassis addresses with a varying middle octet.

    A guess, written visibly into the schema so it can be corrected by hand
    rather than re-inferred on every run.
    """
    per_port = {}

    for trap in traps:
        per_port.setdefault(trap["group"], []).append(trap["varid"])

    detected = {}
    for group, varids in per_port.items():
        addresses = [v.partition("@")[0].split(".") for v in varids]
        three_octet = all(len(a) == 3 for a in addresses)
        one_index = len({a[DEFAULT_PORT_OCTET] for a in addresses}) == 1
        detected[group] = bool(three_octet and one_index)

    return detected


def expand_ports(traps, port_count):
    """Replicate per-port traps across port_count inputs.

    Returns (traps, warnings). A port_count of None, 0 or 1 is a no-op and
    leaves names untouched, so an unexpanded run reports exactly as before.

    Ports are displayed 1-based because that is how the front panel and the
    WebEasy page label inputs, while the varid octet is 0-based: display port 1
    is octet 0. Only groups marked per_port expand; everything else passes
    through once, which is what keeps chassis-level system traps from being
    multiplied 64 times.
    """
    if not port_count or port_count <= 1:
        return traps, []

    expanded = []
    warnings = []

    # Group first appearance decides output order, so the report keeps its
    # video / audio / system sequence.
    order = []
    by_group = {}
    for trap in traps:
        if trap["group"] not in by_group:
            by_group[trap["group"]] = []
            order.append(trap["group"])
        by_group[trap["group"]].append(trap)

    for group in order:
        members = by_group[group]

        if not any(trap.get("per_port") for trap in members):
            expanded.extend(members)
            continue

        expandable = []
        for trap in members:
            if not trap.get("per_port"):
                expanded.append(trap)
                continue

            octet = trap.get("port_octet", DEFAULT_PORT_OCTET)
            octet_count = len(trap["varid"].partition("@")[0].split("."))

            # The port octet must have at least one octet after it: a per-port
            # varid is <base>.<port>.<index>. Rewriting the *last* octet would
            # silently walk across unrelated parameters instead of across
            # inputs - marking the chassis group per-port would turn
            # 850.2@i (CPU Usage) into 850.0/850.1/850.2, three different
            # traps. Distinct varids, so the collision check below cannot see
            # it; this is the only thing standing between a bad hand-edit and
            # writing to the wrong parameters on real hardware.
            if octet >= octet_count - 1:
                warnings.append(
                    "%s/%s: varid %s has no index octet after octet %d - left "
                    "unexpanded (a per-port varid needs <base>.<port>.<index>)"
                    % (trap["group"], trap["name"], trap["varid"], octet)
                )
                expanded.append(trap)
                continue

            expandable.append(trap)

        # Port-major: every parameter for input 1, then input 2. Param-major
        # would mean a run interrupted at 60% left all 64 inputs
        # half-configured; this way the inputs it reached are complete.
        for port in range(port_count):
            for trap in expandable:
                clone = dict(trap)
                clone["varid"] = _set_octet(
                    trap["varid"], trap.get("port_octet", DEFAULT_PORT_OCTET),
                    port,
                )
                clone["port"] = port + 1
                clone["name"] = "%s (port %d)" % (trap["name"], port + 1)
                expanded.append(clone)

    # A bad port_octet can map two different parameters onto one varid, which
    # would otherwise show up as two report rows fighting over one value.
    owners = {}
    collisions = []
    for trap in expanded:
        previous = owners.get(trap["varid"])
        if previous is not None and previous != trap["name"]:
            collisions.append((trap["varid"], previous, trap["name"]))
        owners[trap["varid"]] = trap["name"]

    for varid, first, second in collisions[:5]:
        warnings.append(
            "expansion collision: %s is claimed by both %r and %r"
            % (varid, first, second)
        )
    if len(collisions) > 5:
        warnings.append(
            "expansion collision: %d further duplicate varids"
            % (len(collisions) - 5)
        )

    return expanded, warnings


def chunked(items, size):
    """Yield successive size-length slices of items."""
    for start in range(0, len(items), size):
        yield items[start:start + size]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class RpcError(Exception):
    """A chunk-level RPC failure: transport, HTTP, or malformed JSON."""


def _brief_error(exc):
    """Condense a requests/urllib3 exception to one report-legible clause.

    urllib3 nests the real cause inside a retry-pool message several layers
    deep, which is unreadable in a per-host report line. The innermost OS
    clause is the only part an operator acts on.
    """
    if isinstance(exc, requests.Timeout):
        return "timed out"

    text = " ".join(str(exc).split())
    match = re.search(r"\[(?:WinError|Errno) -?\d+\][^\"')]*", text)
    if match:
        return match.group(0).strip(" .")

    if isinstance(exc, requests.ConnectionError):
        return "connection failed"

    return text[:120]


class HostSession:
    """One device's transport: proto/endpoint discovery, login, paced RPC.

    Discovery and login happen once per host rather than once per request.
    A trap run issues ~30 requests per host, and re-logging-in for each of
    them would triple the load this module exists to keep low.
    """

    def __init__(self, setter, host):
        self.setter = setter
        self.host = host
        self.session = setter.session
        self.proto = None
        self.endpoint = None
        # None until discovery settles, and stays None for the cgi-bin path.
        # This, not the endpoint literal, is what decides the auth path.
        self.webeasy_version = None
        self.cookie = None
        self.request_count = 0
        # Wall clock of the last completed request, so the inter-request gap
        # is measured from completion - a slow RPC does not get to eat the
        # pacing delay it was supposed to be followed by.
        self._last_request = None

    # -- discovery ---------------------------------------------------------

    def _check_proto(self):
        """HTTP or HTTPS? Try HTTP and follow redirects, fall back to HTTPS."""
        try:
            response = self.session.head(
                "http://%s" % self.host,
                timeout=self.setter.timeout,
                allow_redirects=True,
            )
            return "https" if response.url.startswith("https://") else "http"
        except requests.RequestException:
            try:
                response = self.session.head(
                    "https://%s" % self.host, timeout=self.setter.timeout
                )
                if response.ok:
                    return "https"
                raise RpcError(
                    "HTTPS probe returned %s" % response.status_code
                )
            except requests.RequestException as exc:
                raise RpcError(
                    "could not connect over HTTP or HTTPS (%s)" % _brief_error(exc)
                )

    def _check_endpoint(self, proto):
        """Which RPC endpoint does this generation of firmware expose?

        Returns (path, version). version is None for the older cgi-bin
        session/login path and the WebEasy version string for a delegate path,
        which is what uses_basic_auth keys off.

        Probes cfgjsonrpc directly: device generations disagree on 404 vs 200
        for the /cgi-bin/ directory even when cfgjsonrpc works, so the
        directory status is not a reliable gate.
        """
        try:
            response = self.session.get(
                "%s://%s%s" % (proto, self.host, _CGI_ENDPOINT),
                timeout=self.setter.timeout,
            )
            if response.status_code == 200:
                return _CGI_ENDPOINT, None
        except requests.RequestException:
            pass

        # Newer firmware serves the delegate endpoint under a version-stamped
        # path (WebEasy 1.5, 1.6, ...). Probe them newest-first and take the
        # first that answers, rather than asking the operator which firmware
        # each box runs: the version travels with the device, not with the
        # device type, so one fleet routinely spans both. Costs one extra HEAD
        # per host only when cfgjsonrpc is absent.
        tried = []
        for version, path in self.setter.delegate_endpoints:
            try:
                response = self.session.head(
                    "%s://%s%s" % (proto, self.host, path),
                    timeout=self.setter.timeout,
                )
            except requests.RequestException as exc:
                tried.append("%s (%s)" % (path, _brief_error(exc)))
                continue

            if response.ok:
                return path, version

            tried.append("%s (%s)" % (path, response.status_code))

        # Every candidate exhausted. The per-path statuses are the useful part:
        # a 401 everywhere means bad credentials, a 404 everywhere means an
        # unknown WebEasy version to add to _DELEGATE_ENDPOINTS.
        raise RpcError(
            "no usable endpoint: %s unavailable, %s"
            % (_CGI_ENDPOINT,
               ", ".join(tried) or "no delegate paths to probe (--webeasy-version)")
        )

    @property
    def uses_basic_auth(self):
        """Newer firmware authenticates per-request on the delegate endpoint.

        Keyed on whether discovery settled on a version-stamped delegate path,
        not on an equality test against one literal - so a new row in
        _DELEGATE_ENDPOINTS needs no change here.
        """
        return self.webeasy_version is not None

    def _login(self):
        """Establish a WebEasy session cookie for the cfgjsonrpc endpoint.

        The cookie is read off Set-Cookie and echoed back by hand rather than
        through session.cookies, so concurrent hosts do not race on a shared jar.
        """
        response = self.session.get(
            "%s://%s/login.php" % (self.proto, self.host),
            timeout=self.setter.timeout,
        )
        set_cookie = response.headers.get("Set-Cookie")
        if not set_cookie:
            raise RpcError("login.php returned no Set-Cookie header")
        return set_cookie.split(";")[0]

    def open(self):
        """Discover transport and authenticate. Raises RpcError on failure."""
        self.proto = self.setter.proto or self._check_proto()
        self.endpoint, self.webeasy_version = self._check_endpoint(self.proto)
        if not self.uses_basic_auth:
            self.cookie = self._login()

    # -- request pacing ----------------------------------------------------

    def _pace(self):
        """Sleep out the remainder of the inter-request delay, if any.

        Also the checkpoint at which a worker thread notices the run was
        interrupted. KeyboardInterrupt is delivered to the main thread
        only, so without this a --workers run would keep issuing requests
        to every remaining host after Ctrl-C.
        """
        if self.setter.interrupted:
            raise KeyboardInterrupt("run interrupted")
        if self._last_request is None:
            return
        remaining = self.setter.delay - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)

    # -- rpc ---------------------------------------------------------------

    def _post(self, method, parameters):
        url = "%s://%s%s" % (self.proto, self.host, self.endpoint)
        headers = {
            "Content-type": "application/x-www-form-urlencoded; charset=UTF-8"
        }
        auth = None

        if self.uses_basic_auth:
            # Per-request auth, not session.auth - the latter is one mutable
            # attribute on a shared session and would race across hosts.
            auth = self.setter.basic_auth
        else:
            headers["Cookie"] = self.cookie + "; webeasy-loggedin=true"

        body = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": {"parameters": parameters},
            "id": 1,
        })

        self._pace()
        try:
            response = self.session.post(
                url,
                headers=headers,
                data=body,
                auth=auth,
                timeout=self.setter.timeout,
            )
        except requests.RequestException as exc:
            raise RpcError("%s request failed: %s" % (method, _brief_error(exc)))
        finally:
            self.request_count += 1
            self._last_request = time.monotonic()

        if not response.ok:
            raise RpcError("HTTP %s from %s" % (response.status_code, url))

        try:
            return json.loads(response.text)
        except ValueError as exc:
            raise RpcError("malformed JSON response (%s)" % exc)

    def rpc(self, method, parameters):
        """POST one chunk, retrying transient failures.

        A stale session cookie is the one failure worth handling specially:
        re-login once and replay the chunk. Everything else just retries.
        """
        attempts = self.setter.retries + 1
        last_error = None

        for attempt in range(attempts):
            try:
                payload = self._post(method, parameters)
            except RpcError as exc:
                last_error = exc
                if attempt + 1 < attempts and not self.uses_basic_auth:
                    try:
                        self.cookie = self._login()
                    except RpcError:
                        pass
                continue

            if isinstance(payload, dict) and payload.get("error"):
                # A top-level error is the service rejecting the whole call
                # (bad method, auth); retrying a malformed call is pointless.
                raise RpcError("service error: %s" % payload["error"])

            return payload

        raise RpcError(str(last_error))


def map_response(sent, payload):
    """Map an RPC reply back onto the varids that were sent.

    Returns {varid: {"value": ..., "error": ...}}. Keyed on the echoed id, with
    positional fallback for firmware that omits it on error - an errored
    parameter that cannot be attributed would otherwise vanish from the report.
    """
    mapped = {}
    parameters = []

    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, dict):
            parameters = result.get("parameters") or []

    unattributed = []
    for index, entry in enumerate(parameters):
        if not isinstance(entry, dict):
            continue
        varid = entry.get("id")
        record = {"value": entry.get("value"), "error": entry.get("error")}
        if varid:
            mapped[varid] = record
        else:
            unattributed.append((index, record))

    for index, record in unattributed:
        if index < len(sent):
            mapped.setdefault(sent[index]["id"], record)

    return mapped


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

class TrapSetter:
    """Applies a parsed trap spec across a fleet and records the outcome."""

    def __init__(self, hosts, traps, **options):
        self.hosts = list(hosts)
        self.traps = list(traps)

        self.chunk_size = min(
            int(options.get("chunk_size") or DEFAULT_CHUNK_SIZE), MAX_CHUNK_SIZE
        )
        self.delay = float(options.get("delay", DEFAULT_DELAY_S))
        self.workers = max(1, int(options.get("workers") or DEFAULT_WORKERS))
        self.retries = max(0, int(options.get("retries", DEFAULT_RETRIES)))
        self.timeout = options.get("timeout") or DEFAULT_TIMEOUT
        self.proto = options.get("proto") or ""

        # A pinned version narrows the delegate probe to that one path; unset
        # probes all of them, newest first. Either way the cgi-bin probe still
        # runs first, so pinning never locks out an older device in the same
        # fleet. The CLI validates the value against the table, so an unknown
        # version fails at startup rather than silently probing nothing.
        version = options.get("webeasy_version") or ""
        if version:
            self.delegate_endpoints = [
                (v, path) for v, path in _DELEGATE_ENDPOINTS if v == version
            ]
        else:
            self.delegate_endpoints = list(_DELEGATE_ENDPOINTS)

        self.check = bool(options.get("check"))
        self.verify = bool(options.get("verify", True))
        self.set_all = bool(options.get("set_all"))

        credentials = options.get("credentials") or {}
        self.basic_auth = next(iter(credentials.items())) if credentials else None

        self.progress = options.get("progress")

        # Set once Ctrl-C has been seen. Read by _pace on worker threads,
        # which never receive the signal themselves.
        self.interrupted = False

        # Shared pooled session - keep-alive removes a TLS handshake per
        # request, which matters most here: this module makes ~30 sequential
        # requests to the same device.
        self.session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=max(self.workers, 8),
            pool_maxsize=max(self.workers, 8),
            max_retries=0,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.verify = False

    # -- phases ------------------------------------------------------------

    def _read(self, session, traps):
        """GET the given traps in chunks. Returns {varid: {value, error}}."""
        values = {}
        for chunk in chunked(traps, self.chunk_size):
            sent = [{"id": t["varid"], "type": t["type"]} for t in chunk]
            try:
                values.update(map_response(sent, session.rpc("get", sent)))
            except RpcError as exc:
                # Chunk-level failure attaches to every varid in the chunk, so
                # a dropped request is never mistaken for a converged value.
                for trap in chunk:
                    values[trap["varid"]] = {"value": None, "error": str(exc)}
        return values

    def _write(self, session, traps):
        """SET the given traps in chunks. Returns {varid: {value, error}}."""
        outcomes = {}
        for chunk in chunked(traps, self.chunk_size):
            sent = [
                {"id": t["varid"], "type": t["type"], "value": t["target"]}
                for t in chunk
            ]
            try:
                outcomes.update(map_response(sent, session.rpc("set", sent)))
            except RpcError as exc:
                for trap in chunk:
                    outcomes[trap["varid"]] = {"value": None, "error": str(exc)}
        return outcomes

    # -- per host ----------------------------------------------------------

    def run_host(self, host):
        started = time.time()
        clock = time.monotonic()
        result = {
            "host": host,
            "reachable": True,
            "error": None,
            "proto": None,
            "endpoint": None,
            # None on the cgi-bin path; the WebEasy version on a delegate path.
            # Reported so a fleet run shows which firmware each host resolved to.
            "webeasy_version": None,
            # Which of the three phases was in flight. Names the point an
            # interrupt landed, and distinguishes the read phase (nothing
            # written yet) from the write phase.
            "phase": None,
            "interrupted": False,
            "not_started": False,
            # Varids that never reached a decision at all. Phase 1 builds
            # no records until it completes, so an interrupt there leaves
            # every varid unassessed - which must be stated, not counted
            # as zero.
            "pending": 0,
            "requests": 0,
            "elapsed_s": 0.0,
            "counts": Counter(),
            "traps": [],
        }

        session = HostSession(self, host)

        # Hoisted out of the try so the interrupt handler can still report
        # on whatever the phases managed to decide.
        records = []
        written = set()

        try:
            session.open()
            result["proto"] = session.proto
            result["endpoint"] = session.endpoint
            result["webeasy_version"] = session.webeasy_version

            targets = self.traps

            # Phase 1 - what does the device currently hold? This phase
            # writes nothing, which is what makes an interrupt during it
            # harmless to the device.
            result["phase"] = "read"
            before = self._read(session, targets)

            to_write = []

            for trap in targets:
                reading = before.get(trap["varid"], {})
                record = {
                    "name": trap["name"],
                    "group": trap["group"],
                    # None when unexpanded; lets a JSON consumer filter by
                    # input without parsing the port out of the name.
                    "port": trap.get("port"),
                    "varid": trap["varid"],
                    "fault_varid": trap["fault_varid"],
                    "target": trap["target"],
                    "target_raw": trap["target_raw"],
                    "before": reading.get("value"),
                    "after": None,
                    "status": None,
                    "detail": None,
                }

                current = _to_comparable(reading.get("value"), trap["suffix"])

                if reading.get("error"):
                    # The device could not resolve this varid at all. Writing
                    # it would fail the same way, so skip the write and say so
                    # - firmware variants genuinely lack some notify rows.
                    record["status"] = "unresolved"
                    record["detail"] = "read failed: %s" % reading["error"]
                    if self.set_all:
                        record["detail"] += " (writing anyway: --set-all)"
                        to_write.append(trap)
                elif current == trap["target"] and not self.set_all:
                    record["status"] = "ok"
                    record["after"] = reading.get("value")
                else:
                    to_write.append(trap)

                records.append((trap, record))

            written = {t["varid"] for t in to_write}

            if self.check:
                for trap, record in records:
                    if trap["varid"] in written and record["status"] is None:
                        record["status"] = "would-change"
            elif to_write:
                # Phase 2 - write only what differs.
                result["phase"] = "write"
                set_outcomes = self._write(session, to_write)

                # Phase 3 - re-read what was written. The device, not the RPC
                # status, decides whether a write actually took.
                result["phase"] = "verify"
                after = self._read(session, to_write) if self.verify else {}

                for trap, record in records:
                    if trap["varid"] not in written:
                        continue

                    set_error = set_outcomes.get(trap["varid"], {}).get("error")

                    if not self.verify:
                        if set_error:
                            record["status"] = "failed"
                            record["detail"] = "set rejected: %s" % set_error
                        else:
                            record["status"] = "changed"
                            record["detail"] = "unverified (--no-verify)"
                        continue

                    reading = after.get(trap["varid"], {})
                    record["after"] = reading.get("value")
                    final = _to_comparable(reading.get("value"), trap["suffix"])

                    if final == trap["target"]:
                        record["status"] = "changed"
                        if set_error:
                            # Value is right despite the complaint; keep the
                            # complaint visible rather than papering over it.
                            record["detail"] = (
                                "verified, but set reported: %s" % set_error
                            )
                    elif reading.get("error"):
                        record["status"] = "failed"
                        record["detail"] = "verify read failed: %s" % reading["error"]
                    else:
                        record["status"] = "failed"
                        record["detail"] = "verify mismatch (device holds %r)" % (
                            reading.get("value"),
                        )
                        if set_error:
                            record["detail"] += "; set reported: %s" % set_error

            result["phase"] = "done"

        except KeyboardInterrupt:
            # Ctrl-C. Whatever was decided is kept: expansion is port-major
            # so the inputs already reached are coherent, and that is only
            # actionable if the report says where it stopped.
            result["interrupted"] = True
            phase = result["phase"] or "startup"
            result["error"] = "interrupted during %s" % phase
            if phase in ("startup", "read"):
                # Phase 1 only reads, so there is nothing to be partway
                # through. Say so rather than leaving it ambiguous.
                result["error"] += "; no write was issued"
            result["pending"] = max(0, len(self.traps) - len(records))
            self.interrupted = True
        except RpcError as exc:
            result["reachable"] = False
            result["error"] = str(exc)
        except Exception as exc:
            # One bad host must never take down the rest of the fleet run.
            result["reachable"] = False
            result["error"] = "%s: %s" % (type(exc).__name__, exc)

        # Finalized outside the try so an interrupted host still reports.
        # An unset status means "already correct" in a completed run but
        # "never reached" in an interrupted one; calling the latter `ok`
        # would claim a varid is converged without having looked at it.
        # Skipped for an unreachable host, which has nothing to report.
        if result["reachable"]:
            for trap, record in records:
                if record["status"] is None:
                    if result["interrupted"]:
                        record["status"] = "skipped"
                        record["detail"] = (
                            "write issued but not verified before the "
                            "interrupt"
                            if trap["varid"] in written
                            else "not reached before the interrupt"
                        )
                    else:
                        # Nothing to write, nothing read wrong - correct already.
                        record["status"] = "ok"
                result["traps"].append(record)
                result["counts"][record["status"]] += 1

        result["requests"] = session.request_count
        result["elapsed_s"] = round(time.monotonic() - clock, 2)
        result["started"] = started

        if self.progress:
            self.progress(result)

        return result

    def _not_started(self, host):
        """Placeholder for a host the run never reached.

        Recorded rather than omitted: an interrupted fleet run's recap must
        still account for every host that was asked for, or a shortened
        table reads as though the fleet were smaller than it is.
        """
        return {
            "host": host,
            "reachable": False,
            "error": "not started (run interrupted)",
            "not_started": True,
            "interrupted": False,
            "proto": None,
            "endpoint": None,
            "webeasy_version": None,
            "phase": None,
            "requests": 0,
            "elapsed_s": 0.0,
            "counts": Counter(),
            "traps": [],
            "started": None,
            "pending": len(self.traps),
        }

    def run(self):
        """Apply to every host. Returns the list of per-host results.

        A KeyboardInterrupt never discards what completed: run_host absorbs
        it for the host in flight, and the hosts after it are recorded as
        not started.
        """
        results = []

        if self.workers == 1:
            for index, host in enumerate(self.hosts):
                results.append(self.run_host(host))
                if self.interrupted:
                    results.extend(
                        self._not_started(h)
                        for h in self.hosts[index + 1:]
                    )
                    break
            return results

        # submit/as_completed rather than pool.map: map() propagates the
        # interrupt and takes the completed results with it.
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = OrderedDict(
                (pool.submit(self.run_host, host), host)
                for host in self.hosts
            )
            try:
                for future in futures:
                    future.result()
            except KeyboardInterrupt:
                # Setting the flag first is what lets the pool's
                # shutdown-wait below return promptly: running workers
                # abort at their next pacing checkpoint.
                self.interrupted = True
                for future in futures:
                    future.cancel()

        for future, host in futures.items():
            if future.cancelled():
                results.append(self._not_started(host))
                continue
            try:
                results.append(future.result())
            except BaseException:
                results.append(self._not_started(host))

        return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _format_duration(seconds):
    """Human-readable duration, for run estimates and the report header."""
    if seconds < 60:
        return "%.0fs" % seconds
    if seconds < 3600:
        return "%dm %02ds" % divmod(int(seconds), 60)
    hours, rest = divmod(int(seconds), 3600)
    return "%dh %02dm" % (hours, rest // 60)


def _format_value(value):
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _recap_counts(result):
    counts = result["counts"]
    return "  ".join(
        "%s=%d" % (status, counts.get(status, 0))
        for status in STATUS_ORDER
        if counts.get(status)
    ) or "no parameters"


def render_text_report(run):
    """Render the Ansible-style run report."""
    lines = []
    meta = run["meta"]

    def rule(char="="):
        lines.append(char * 78)

    rule()
    lines.append("trapSetter run report")
    rule()
    lines.append("Started      : %s" % meta["started"])
    lines.append("Finished     : %s" % meta["finished"])
    lines.append("Duration     : %.1fs" % meta["duration_s"])
    lines.append("Mode         : %s" % meta["mode"])
    lines.append("Params file  : %s" % meta["params_file"])
    lines.append("Groups       : %s" % (", ".join(meta["groups"]) or "none"))
    lines.append(
        "Varids/host  : %d  (%s)"
        % (
            meta["trap_count"],
            ", ".join(
                "%s %d" % (group, count)
                for group, count in meta["group_counts"].items()
            ) or "none",
        )
    )
    lines.append("Ports        : %s" % meta["ports_note"])
    if meta.get("varid_note"):
        lines.append("Varids       : %s" % meta["varid_note"])
    if meta.get("target_note"):
        lines.append("Target       : %s" % meta["target_note"])
    lines.append("Hosts        : %d" % len(run["hosts"]))
    lines.append(
        "Chunk size   : %d    Delay: %.2fs    Workers: %d    Retries: %d"
        % (meta["chunk_size"], meta["delay"], meta["workers"], meta["retries"])
    )
    lines.append(
        "Verify       : %s    Write-all: %s"
        % ("yes" if meta["verify"] else "NO", "yes" if meta["set_all"] else "no")
    )

    if meta["warnings"]:
        lines.append("")
        lines.append("Params file warnings:")
        for warning in meta["warnings"]:
            lines.append("  ! %s" % warning)

    lines.append("")

    for result in run["hosts"]:
        rule("-")
        header = "HOST %s" % result["host"]
        if result.get("not_started"):
            lines.append("%s -- NOT STARTED (run interrupted)" % header)
            lines.append("")
            continue
        if not result["reachable"]:
            lines.append("%s -- UNREACHABLE" % header)
            lines.append("  error: %s" % result["error"])
            lines.append("")
            continue
        if result.get("interrupted"):
            header += " -- INTERRUPTED"

        lines.append(
            "%s  [%s%s]  %s"
            % (header, result["proto"], result["endpoint"], _recap_counts(result))
        )
        lines.append(
            "  %d requests in %.1fs" % (result["requests"], result["elapsed_s"])
        )
        if result.get("interrupted"):
            lines.append("  %s" % result["error"])
            if result.get("pending"):
                lines.append("  %d parameter(s) never assessed"
                             % result["pending"])
        lines.append("")

        current_group = None
        current_port = None
        shown = 0
        for record in result["traps"]:
            if meta["brief"] and record["status"] == "ok":
                continue
            if record["group"] != current_group:
                current_group = record["group"]
                current_port = None
                lines.append("  %s" % current_group.upper())
            # Under expansion the rows are port-major, so a separator makes an
            # input's block findable without reading every name suffix.
            if record.get("port") is not None and record["port"] != current_port:
                current_port = record["port"]
                lines.append("   - port %d" % current_port)
            shown += 1

            if record["status"] == "changed":
                # Under --no-verify there is no read-back, so show the value
                # that was written rather than an empty "after" column.
                landed = (
                    record["after"] if record["after"] is not None
                    else record["target"]
                )
                transition = "%s -> %s" % (
                    _format_value(record["before"]), _format_value(landed),
                )
            elif record["status"] == "would-change":
                transition = "%s => %s" % (
                    _format_value(record["before"]),
                    _format_value(record["target"]),
                )
            elif record["status"] == "ok":
                transition = "%s" % _format_value(record["before"])
            else:
                # failed / unresolved: an arrow would read as though the write
                # took. State what the device holds and what was wanted.
                transition = "%s (want %s)" % (
                    _format_value(record["before"]),
                    _format_value(record["target"]),
                )

            lines.append(
                "    [%-12s] %-34s %-13s %s%s"
                % (
                    record["status"],
                    record["name"][:34],
                    record["varid"],
                    transition,
                    "  (%s)" % record["detail"] if record["detail"] else "",
                )
            )

        if meta["brief"] and not shown:
            lines.append("  all parameters already at target")

        lines.append("")

    rule()
    lines.append("RECAP")
    rule()
    lines.append(
        "%-24s %6s %8s %13s %7s %11s %8s %6s"
        % ("host", "ok", "changed", "would-change", "failed", "unresolved",
           "skipped", "reqs")
    )
    for result in run["hosts"]:
        if not result["reachable"]:
            lines.append(
                "%-24s %6s %8s %13s %7s %11s %8s %6d   %s"
                % (result["host"], "-", "-", "-", "-", "-", "-",
                   result["requests"],
                   "NOT STARTED" if result.get("not_started")
                   else "UNREACHABLE")
            )
            continue
        counts = result["counts"]
        lines.append(
            "%-24s %6d %8d %13d %7d %11d %8d %6d%s"
            % (
                result["host"],
                counts.get("ok", 0),
                counts.get("changed", 0),
                counts.get("would-change", 0),
                counts.get("failed", 0),
                counts.get("unresolved", 0),
                counts.get("skipped", 0),
                result["requests"],
                "   INTERRUPTED" if result.get("interrupted") else "",
            )
        )

    totals = run["totals"]
    lines.append("")
    lines.append(
        "TOTAL  hosts=%d  unreachable=%d  ok=%d  changed=%d  would-change=%d  "
        "failed=%d  unresolved=%d  skipped=%d  requests=%d"
        % (
            totals["hosts"],
            totals["unreachable"],
            totals["ok"],
            totals["changed"],
            totals["would-change"],
            totals["failed"],
            totals["unresolved"],
            totals.get("skipped", 0),
            totals["requests"],
        )
    )
    if totals.get("interrupted") or totals.get("not_started"):
        lines.append(
            "       interrupted=%d  not-started=%d  never-assessed=%d"
            % (totals.get("interrupted", 0), totals.get("not_started", 0),
               totals.get("pending", 0))
        )
    lines.append("RESULT: %s" % run["meta"]["result"])
    lines.append("")

    return "\n".join(lines)


def summarize(run_hosts):
    """Total the per-host counts.

    Every status key is seeded so both the recap table and the JSON report have
    a stable shape - a run with no failures still reports failed=0 rather than
    omitting the key and breaking whatever reads the JSON.
    """
    totals = Counter({status: 0 for status in STATUS_ORDER})
    totals["unreachable"] = 0
    totals["interrupted"] = 0
    totals["not_started"] = 0
    totals["pending"] = 0
    totals["requests"] = 0
    totals["hosts"] = len(run_hosts)

    for result in run_hosts:
        if result.get("not_started"):
            # Counted separately: a host that never ran is not the same
            # finding as one that could not be reached.
            totals["not_started"] += 1
        elif not result["reachable"]:
            totals["unreachable"] += 1
        if result.get("interrupted"):
            totals["interrupted"] += 1
        for status, count in result["counts"].items():
            totals[status] += count
        totals["pending"] += result.get("pending", 0)
        totals["requests"] += result["requests"]
    return totals


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# Runtime source order. JSON is the reference schema; the text exports are
# kept only as conversion input for --build-schema.
_PARAMS_EXTENSIONS = (".json", ".csv", ".txt")


def params_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "trap-params")


def logs_dir():
    """Where reports land when --report/--json are not given.

    Anchored to the script's own directory, like params_dir(), rather than to
    the process's cwd. The tool is normally invoked as
    `python3 trap-config-setter/trapSetter.py` from a parent directory, and a
    cwd-relative default scatters timestamped reports there instead of keeping
    them with the tool - which is also what makes the default behave the same
    after the directory is copied to another machine.
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def _ensure_parent(path):
    """Create the directory a report is about to be written into.

    Returns the path, so it composes into the assignment. Called before the run
    rather than at write time: a report path that cannot be created should fail
    in the first second, not after an 11-minute --ports 64 run has completed
    and has nowhere to put its only record.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    return path


def resolve_params_path(args, prefer=_PARAMS_EXTENSIONS):
    """Locate the params source for --device, or None if nothing is present.

    An explicit --params wins outright. Otherwise the JSON schema is preferred
    and a text export is only picked up when no schema has been built yet, so
    building one silently promotes it to the runtime source.
    """
    if args.params:
        return args.params

    for extension in prefer:
        candidate = os.path.join(params_dir(), args.device + extension)
        if os.path.isfile(candidate):
            return candidate

    return None


def collect_hosts(args):
    hosts = []
    for entry in args.hosts or []:
        hosts.extend(part for part in re.split(r"[,\s]+", entry) if part)

    if args.hosts_file:
        with open(args.hosts_file, "r") as handle:
            for line in handle:
                line = line.split("#", 1)[0].strip()
                if line:
                    hosts.extend(part for part in re.split(r"[,\s]+", line) if part)

    # De-duplicate while keeping the order the operator supplied.
    seen = set()
    ordered = []
    for host in hosts:
        if host not in seen:
            seen.add(host)
            ordered.append(host)
    return ordered


def build_parser():
    parser = argparse.ArgumentParser(
        prog="trapSetter",
        description="Bulk-set trap/notify varids on Evertz WebEasy devices "
                    "and write an Ansible-style report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run with --check first: it reports exactly what would change "
               "without writing anything.",
    )

    source = parser.add_argument_group("parameter source")
    source.add_argument(
        "--device", default="vip100g",
        help="device type; loads trap-params/<device>.json, falling back to "
             "the .csv/.txt export if no schema has been built (default: "
             "vip100g)",
    )
    source.add_argument(
        "--params",
        help="explicit path to a params source, overriding --device; a .json "
             "schema or a .csv/.txt export, dispatched by extension",
    )
    source.add_argument(
        "--groups",
        help="comma-separated groups to apply (e.g. video,audio). "
             "Default: every group in the file.",
    )
    source.add_argument(
        "--ports", type=int, metavar="N",
        help="number of video/audio inputs to apply to, overriding the "
             "schema's port_count. Per-port groups are replicated across N "
             "inputs by rewriting the port octet. 1 (or a null port_count) "
             "applies the base varids only.",
    )
    source.add_argument(
        "--varids",
        help="comma/space-separated varids to apply, filtering the loaded "
             "set after port expansion (e.g. 400.0.0@i,850.18@i). The "
             "@suffix is optional. Scopes a run to a handful of parameters "
             "without hand-writing a schema file.",
    )
    source.add_argument(
        "--target", metavar="VALUE",
        help="override every target value, in either direction: 0/1, "
             "FALSE/TRUE, a number, or a string for an @s parameter. "
             "Coerced per varid suffix, so one value suits a mixed set.",
    )
    source.add_argument(
        "--force-false", action="store_true",
        help="alias for --target 0: override every target value to 0, "
             "ignoring the TRUE entries in the params file",
    )
    source.add_argument(
        "--list", action="store_true",
        help="print the parsed parameter set and exit; touches no devices",
    )
    source.add_argument(
        "--build-schema", nargs="?", const="", metavar="OUT",
        help="convert the device's .csv/.txt export into the JSON reference "
             "schema and exit. Defaults to trap-params/<device>.json; pass a "
             "path to write elsewhere, or - for stdout.",
    )

    targets = parser.add_argument_group("targets")
    targets.add_argument(
        "--hosts", action="append",
        help="host or comma/space-separated hosts; repeatable",
    )
    targets.add_argument(
        "--hosts-file",
        help="file of hosts, one per line; # comments allowed",
    )
    targets.add_argument("--user", help="username for device auth")
    targets.add_argument("--password", help="password for device auth")
    targets.add_argument(
        "--proto", choices=("http", "https"),
        help="skip protocol discovery and force http or https",
    )
    targets.add_argument(
        "--webeasy-version", choices=[v for v, _ in _DELEGATE_ENDPOINTS],
        help="narrow delegate-endpoint probing to one WebEasy version. "
             "Normally left unset: the version is detected per host, newest "
             "first. Pin it only to stop a device that answers on more than "
             "one delegate path from being probed ambiguously; the "
             "/cgi-bin/cfgjsonrpc probe still runs first either way.",
    )

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument(
        "--check", action="store_true",
        help="dry run: read and report what would change, write nothing",
    )
    behaviour.add_argument(
        "--no-verify", dest="verify", action="store_false",
        help="skip the post-write read-back (statuses become unverified)",
    )
    behaviour.add_argument(
        "--set-all", action="store_true",
        help="write every targeted varid even if it already holds the target",
    )
    behaviour.add_argument(
        "--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
        help="parameters per request, capped at %d (default: %d)"
             % (MAX_CHUNK_SIZE, DEFAULT_CHUNK_SIZE),
    )
    behaviour.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY_S,
        help="seconds between requests to the same device (default: %.1f)"
             % DEFAULT_DELAY_S,
    )
    behaviour.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help="hosts to process concurrently; the delay is per-host, so this "
             "does not increase load on any one device (default: %d)"
             % DEFAULT_WORKERS,
    )
    behaviour.add_argument(
        "--retries", type=int, default=DEFAULT_RETRIES,
        help="retries per failed request chunk (default: %d)" % DEFAULT_RETRIES,
    )
    behaviour.add_argument(
        "--timeout", default="3,10",
        help="connect,read timeout in seconds (default: 3,10)",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--report",
        help="path for the text report (default: "
             "logs/trap-report-<device>-<timestamp>.txt, alongside the "
             "script). An explicit path is used as given.",
    )
    output.add_argument("--json", dest="json_report", help="path for a JSON report")
    output.add_argument(
        "--brief", action="store_true",
        help="omit already-correct parameters from the per-host listing",
    )
    output.add_argument(
        "--quiet", action="store_true", help="suppress per-host progress output",
    )

    return parser


def convert_schema(args, parser):
    """--build-schema: text export in, JSON reference schema out."""
    # Deliberately skips .json in the search order: the point is to convert an
    # export, and preferring an existing schema would make this a no-op.
    source = resolve_params_path(args, prefer=(".csv", ".txt"))
    if source is None:
        parser.error(
            "no .csv or .txt export for device %r in %s"
            % (args.device, params_dir())
        )

    traps, warnings, _meta = parse_export(source)
    if not traps:
        parser.error("no usable parameters parsed from %s" % source)

    schema = build_schema(traps, args.device, source)
    text = json.dumps(schema, indent=2) + "\n"

    for warning in warnings:
        print("! %s" % warning, file=sys.stderr)

    if args.build_schema == "-":
        sys.stdout.write(text)
        return 0

    out = args.build_schema or os.path.join(params_dir(), "%s.json" % args.device)
    with open(out, "w") as handle:
        handle.write(text)

    counts = ", ".join(
        "%s %d (-> %s)%s" % (name, len(group["params"]), group["target"],
                             " per-port" if group.get("per_port") else "")
        for name, group in schema["groups"].items()
    )
    print("wrote %s" % out)
    print("  from   : %s" % source)
    print("  params : %d  [%s]" % (schema["param_count"], counts))

    per_port_groups = [n for n, g in schema["groups"].items()
                       if g.get("per_port")]
    if per_port_groups:
        per_port_total = sum(len(schema["groups"][n]["params"])
                             for n in per_port_groups)
        print("  ports  : port_count is null (base varids only). Groups "
              "detected as per-port: %s" % ", ".join(per_port_groups))
        print("           set port_count to the device's input count to "
              "expand those %d params per port." % per_port_total)

    # Round-trip immediately: a schema that cannot be read back is worse than
    # no schema, and this is the only moment both sides are in hand.
    reloaded, reload_warnings, _reload_meta = load_schema(out)
    for warning in reload_warnings:
        print("! reload: %s" % warning, file=sys.stderr)

    original = [(t["group"], t["name"], t["varid"], t["target"]) for t in traps]
    round_tripped = [
        (t["group"], t["name"], t["varid"], t["target"]) for t in reloaded
    ]

    if original == round_tripped:
        print("  verify : round-trips to %d identical parameters" % len(reloaded))
        return 0

    print("  verify : MISMATCH - schema does not round-trip", file=sys.stderr)
    for left, right in zip(original, round_tripped):
        if left != right:
            print("    export %r != schema %r" % (left, right), file=sys.stderr)
            break
    if len(original) != len(round_tripped):
        print("    count %d != %d" % (len(original), len(round_tripped)),
              file=sys.stderr)
    return 1


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.build_schema is not None:
        return convert_schema(args, parser)

    params_path = resolve_params_path(args)
    if params_path is None:
        parser.error(
            "no params source for device %r in %s (looked for %s)"
            % (args.device, params_dir(),
               ", ".join(args.device + e for e in _PARAMS_EXTENSIONS))
        )
    if not os.path.isfile(params_path):
        parser.error("params file not found: %s" % params_path)

    try:
        traps, warnings, params_meta = load_params(params_path)
    except ValueError as exc:
        parser.error(str(exc))

    if not traps:
        parser.error("no usable parameters parsed from %s" % params_path)

    if not params_path.lower().endswith(".json") and not args.quiet:
        print("note: reading the text export %s; run --build-schema to "
              "generate the JSON reference schema"
              % os.path.basename(params_path), file=sys.stderr)

    available = []
    for trap in traps:
        if trap["group"] not in available:
            available.append(trap["group"])

    if args.groups:
        wanted = [g.strip().lower() for g in args.groups.split(",") if g.strip()]
        unknown = [g for g in wanted if g not in available]
        if unknown:
            parser.error(
                "unknown group(s) %s; %s has: %s"
                % (", ".join(unknown), os.path.basename(params_path),
                   ", ".join(available))
            )
        traps = [t for t in traps if t["group"] in wanted]
        groups = wanted
    else:
        groups = available

    # Expand after the group filter so a --groups run does not build 4000
    # traps and discard most of them, and before --force-false so the override
    # lands on every replica.
    port_count = args.ports if args.ports is not None else params_meta.get("port_count")

    if port_count is not None and port_count < 1:
        parser.error("--ports must be 1 or greater (got %d)" % port_count)

    base_count = len(traps)
    traps, port_warnings = expand_ports(traps, port_count)
    warnings.extend(port_warnings)

    expanded_groups = sorted({t["group"] for t in traps if t.get("port")})
    # Captured before --varids narrows the set: ports_note describes what
    # expansion produced, and the separate Varids line describes the
    # narrowing. Measuring the post-filter length here read as though
    # expansion itself had produced 3 params.
    expanded_count = len(traps)

    # --varids runs after expansion so a token can name one specific input
    # (400.31.0@i), and before the target override so the override lands on
    # exactly the set that will be written.
    varid_note = None
    if args.varids:
        wanted = [v for v in re.split(r"[,\s]+", args.varids) if v]
        selected = []
        matched = set()
        for trap in traps:
            address = trap["varid"].partition("@")[0]
            for token in wanted:
                # Accept the bare address too: typing the @suffix adds
                # nothing, since the type is derived from the varid anyway.
                if token in (trap["varid"], address):
                    selected.append(trap)
                    matched.add(token)
                    break

        missing = [v for v in wanted if v not in matched]
        if missing:
            parser.error(
                "no parameter matches varid(s) %s; the loaded set holds %d "
                "varid(s) - use --list to see them"
                % (", ".join(missing), len(traps))
            )

        varid_note = "%d of %d selected by --varids" % (
            len(selected), len(traps))
        traps = selected

        # Drop groups the filter emptied, so the report header and the
        # group counts describe what will actually run.
        groups = [g for g in groups
                  if any(t["group"] == g for t in traps)]

    if args.force_false and args.target is not None:
        parser.error("--force-false and --target are mutually exclusive "
                     "(--force-false is an alias for --target 0)")

    override_raw = "0" if args.force_false else args.target
    target_note = None
    if override_raw is not None:
        flag = "--force-false" if args.force_false else "--target"
        # Coerced per suffix rather than once, so a single --target suits a
        # set mixing @i and @s. A value no suffix can take is an error, not
        # a silently skipped parameter.
        rejected = []
        for trap in traps:
            value = _coerce_target(override_raw, trap["suffix"])
            if value is None:
                rejected.append("%s (@%s)" % (trap["varid"], trap["suffix"]))
                continue
            trap["target"] = value
            trap["target_raw"] = "%s (%s)" % (override_raw, flag)

        if rejected:
            parser.error(
                "%s %r is not a usable value for %d varid(s), e.g. %s"
                % (flag, override_raw, len(rejected),
                   ", ".join(rejected[:3]))
            )

        target_note = "every target overridden to %r by %s" % (
            override_raw, flag)

    group_counts = OrderedDict()
    for group in groups:
        group_counts[group] = sum(1 for t in traps if t["group"] == group)

    ports_note = "not expanded (base varids only)"
    if expanded_groups:
        ports_note = "%d per input; %s expanded from %d to %d params" % (
            port_count, ", ".join(expanded_groups), base_count,
            expanded_count,
        )

    if args.list:
        print("%s: %d parameters across %s"
              % (os.path.basename(params_path), len(traps), ", ".join(groups)))
        for warning in warnings:
            print("  ! %s" % warning)
        current = None
        for trap in traps:
            if trap["group"] != current:
                current = trap["group"]
                print("\n%s (%d)" % (current.upper(), group_counts[current]))
            print("  %-13s %-38s -> %-4s   fault: %s"
                  % (trap["varid"], trap["name"], trap["target"],
                     trap["fault_varid"] or "-"))
        return 0

    hosts = collect_hosts(args)
    if not hosts:
        parser.error("no hosts given; use --hosts and/or --hosts-file")

    credentials = {}
    if args.user:
        credentials = {args.user: args.password or ""}

    try:
        connect, read = (float(part) for part in args.timeout.split(","))
    except ValueError:
        parser.error("--timeout must be connect,read (e.g. 3,10)")

    if args.chunk_size > MAX_CHUNK_SIZE:
        print("note: --chunk-size %d capped to %d to protect the WebEasy server"
              % (args.chunk_size, MAX_CHUNK_SIZE), file=sys.stderr)

    # Both output paths are resolved and their directories created up front,
    # before a single request goes out. An explicit --report/--json is honoured
    # exactly as given (so it stays relative to the operator's cwd); only the
    # default lands in logs/.
    report_path = _ensure_parent(args.report or os.path.join(
        logs_dir(),
        "trap-report-%s-%s.txt" % (args.device, time.strftime("%Y%m%d-%H%M%S")),
    ))
    if args.json_report:
        _ensure_parent(args.json_report)

    def progress(result):
        if args.quiet:
            return
        if not result["reachable"]:
            print("%-24s UNREACHABLE  %s" % (result["host"], result["error"]))
        else:
            print("%-24s %s  (%d requests, %.1fs)%s"
                  % (result["host"], _recap_counts(result),
                     result["requests"], result["elapsed_s"],
                     "  INTERRUPTED" if result.get("interrupted") else ""))

    setter = TrapSetter(
        hosts,
        traps,
        chunk_size=args.chunk_size,
        delay=args.delay,
        workers=args.workers,
        retries=args.retries,
        timeout=(connect, read),
        proto=args.proto,
        webeasy_version=args.webeasy_version,
        check=args.check,
        verify=args.verify,
        set_all=args.set_all,
        credentials=credentials,
        progress=progress,
    )

    mode = "check (no writes)" if args.check else "apply"
    if not args.quiet:
        print("trapSetter: %s, %d hosts x %d varids, chunk=%d delay=%.2fs"
              % (mode, len(hosts), len(traps), setter.chunk_size, setter.delay))
        if expanded_groups:
            print("            ports: %s" % ports_note)
        # Expansion turns a 95-param run into thousands, so state the pacing
        # cost up front rather than letting it be discovered at minute nine.
        chunks = -(-len(traps) // setter.chunk_size)
        max_requests = chunks if args.check else chunks * 3
        estimate = max_requests * setter.delay
        print("            up to %d requests/host, >= %s at this delay%s"
              % (max_requests, _format_duration(estimate),
                 "" if setter.workers == 1 else
                 " (%d hosts in parallel)" % setter.workers))
        print("")

    started = time.time()
    clock = time.monotonic()
    try:
        results = setter.run()
    except KeyboardInterrupt:
        # run() absorbs an interrupt that lands inside a host; this is the
        # backstop for one that lands between them. Either way the report
        # below is still written - losing a long run's only record to a
        # Ctrl-C is the whole failure this guards against.
        setter.interrupted = True
        results = []
    duration = time.monotonic() - clock

    totals = summarize(results)
    failed = (totals["failed"] + totals["unresolved"]
              + totals["unreachable"] + totals["interrupted"]
              + totals["not_started"])

    if setter.interrupted:
        result_line = (
            "INTERRUPTED - %d varid(s) at target, %d skipped, %d never "
            "assessed, %d host(s) incomplete"
            % (totals["ok"] + totals["changed"], totals["skipped"],
               totals["pending"],
               totals["interrupted"] + totals["not_started"])
        )
    elif args.check:
        result_line = (
            "CHECK ONLY - %d varid(s) would change, %d already correct, "
            "%d problem(s)"
            % (totals["would-change"], totals["ok"], failed)
        )
    elif failed:
        result_line = "FAILED - %d varid(s)/host(s) did not reach target" % failed
    else:
        result_line = "SUCCESS - every targeted varid is at its target value"

    run = {
        "meta": {
            "tool": "trapSetter",
            "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
            "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "duration_s": duration,
            "mode": mode,
            "device": args.device,
            "params_file": params_path,
            "groups": groups,
            "group_counts": group_counts,
            "trap_count": len(traps),
            "base_trap_count": base_count,
            "port_count": port_count,
            "ports_note": ports_note,
            "varid_note": varid_note,
            "target_note": target_note,
            "varids_filter": args.varids,
            "target_override": override_raw,
            "expanded_groups": expanded_groups,
            "chunk_size": setter.chunk_size,
            "delay": setter.delay,
            "workers": setter.workers,
            "retries": setter.retries,
            "verify": args.verify,
            "set_all": args.set_all,
            "webeasy_version_pin": args.webeasy_version,
            "force_false": args.force_false,
            "brief": args.brief,
            "warnings": warnings,
            "result": result_line,
        },
        "hosts": results,
        "totals": dict(totals),
    }

    report_text = render_text_report(run)
    with open(report_path, "w") as handle:
        handle.write(report_text)

    if args.json_report:
        with open(args.json_report, "w") as handle:
            # Counter is not JSON-serializable; per-host counts go out as dicts.
            serializable = json.loads(json.dumps(run, default=dict))
            json.dump(serializable, handle, indent=2)

    if not args.quiet:
        print("")
        print(result_line)
        print("report: %s" % os.path.abspath(report_path))
        if args.json_report:
            print("json  : %s" % os.path.abspath(args.json_report))

    return 1 if failed or setter.interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
