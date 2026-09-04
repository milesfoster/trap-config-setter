# trapSetter

Bulk trap/notify configuration writer for Evertz WebEasy devices.

Where [`ptpMon`](../../ptpMon.py) **reads** device state through the `cfgjsonrpc`
web service on a poller cycle, `trapSetter` **writes** trap-enable varids to a
target value across a fleet — once, from the command line — and produces an
Ansible-style report of what changed, what was already correct, and what failed.

It shares ptpMon's transport shape (protocol discovery, endpoint discovery, two
auth paths, per-request cookie/auth to stay thread-safe) but nothing else. It is
a standalone script with no poller integration and no imports from ptpMon.

> **Status: not yet validated against real hardware.** Every offline path —
> parsing, schema build/round-trip, port expansion, `--list`, reporting — works.
> No `set` RPC has ever reached a device. Start with the
> [single-varid smoke test](#single-varid-smoke-test); see [PLAN.md](PLAN.md).

---

## Layout

```
trap-config-setter/
  trapSetter.py          the whole tool (~1900 lines, single file, no package)
  test_trapSetter.py     offline test suite (unittest, no network)
  fleet.example.txt      documented --hosts-file template
  trap-params/
    vip100g.csv          WebEasy "Notify" page export (conversion input only)
    vip100g.json         the reference schema (what runtime actually reads)
    probe.json           one-varid schema for the smoke test
  README.md
  PLAN.md
```

## Tests

```bash
python3 test_trapSetter.py            # or: python3 -m unittest -v test_trapSetter
```

32 tests, ~4ms, no device and no network. Currently covering endpoint discovery
and the auth path it selects — the transport decision duplicated from ptpMon,
and therefore the one most likely to drift. `unittest` rather than pytest, and
no f-strings, so the suite runs wherever the module does.

See PLAN.md P3-1 for what is still uncovered (the pure parsing and expansion
functions). Run it before touching `HostSession`: `extras/` is gitignored, so
this code has no git history to fall back on.

## Quick start

```bash
# 1. Validate the parameter set. No network at all.
python3 trapSetter.py --device vip100g --list

# 2. Dry run against hardware: reads, reports what would change, writes nothing.
python3 trapSetter.py --device vip100g --hosts 172.17.223.93 --check

# 3. Apply, with a report on disk.
python3 trapSetter.py --device vip100g --hosts-file fleet.txt \
    --user root --password evertz --report run.txt --json run.json

# Convert a new device's export into the JSON reference schema.
python3 trapSetter.py --device vip100g --build-schema
```

Exit status is `0` when every targeted varid on every host ended at its target,
`1` if anything failed or a host was unreachable.

---

## Single-varid smoke test

Before trusting a 95- or 4379-parameter run, confirm the `set` RPC round-trips at
all. There is no `--varids` flag yet (see PLAN.md, P1-4), so the way to scope a
run to one parameter is a **one-entry schema** passed with `--params`.

`trap-params/probe.json` is exactly that — one varid, `400.0.0@i` (Loss of Video
trap enable, input 1), chosen because it is chassis-visible on the WebEasy
**Video Notify** page, non-per-port at index 0, and trivially revertible:

```json
{
  "device": "probe",
  "port_count": null,
  "groups": {
    "probe": {
      "target": 1,
      "params": {"Loss of Video": "400.0.0@i"}
    }
  }
}
```

Set the target address once — the steps below reuse it. The variable is pure
convenience; paste the address into each command instead if you prefer.

```bash
IP=172.17.223.93
```

Run it in this order. Substitute your credentials; `--user`/`--password` matter
only if the host lands on a delegate endpoint, but pass them anyway.

```bash
# 1. Read-only. Proves discovery, auth, and the get RPC. Writes nothing.
#    Expect status "ok" (already 1) or "would-change" (currently 0).
python3 trapSetter.py --params trap-params/probe.json --hosts $IP --user root --password evertz --check --report probe-check.txt

# 2. Set it to 1 (true). Phase 3 re-reads, so "changed" means the device agreed.
python3 trapSetter.py --params trap-params/probe.json --hosts $IP --user root --password evertz --report probe-set-1.txt --json probe-set-1.json

# 3. Confirm in the WebEasy UI: Video Notify -> Loss of Video should now be
#    ticked. This is the only step that proves the varid maps to the control you
#    think it does.

# 4. Set it back to 0 (false). --force-false overrides the schema target,
#    so the same file drives both directions.
python3 trapSetter.py --params trap-params/probe.json --hosts $IP --user root --password evertz --force-false --report probe-set-0.txt

# 5. Re-run step 1. Now expect "ok" — proving the revert took and that a
#    converged parameter issues no write at all.
python3 trapSetter.py --params trap-params/probe.json --hosts $IP --user root --password evertz --check
```

Each command is on one line so it pastes cleanly; add `\` continuations to taste.

The probe accepts `--hosts-file` like any other run, so the same five steps work
across a handful of devices — see [Providing hosts](#providing-hosts).

Step 2 should print something close to:

```
  PROBE
    [changed     ] Loss of Video                      400.0.0@i     0 -> 1
```

**What to check in `probe-set-1.json` beyond the status:** the `proto` and
`endpoint` fields (which auth path this device took), `requests` (should be 3 —
one get, one set, one verify get), and `before`/`after` on the trap record.

To probe a **different** varid, copy `probe.json` and change the one entry, or
edit it in place — the group name and `device` field are free text and affect
only report headings. To test a `@s` or `@b` parameter, just use its varid; the
type is derived from the suffix.

If step 2 reports `failed` with a verify mismatch while step 1 read cleanly, the
`get` path works and the `set` path does not — that is the credentialed-write
question in PLAN.md P1-2, and it is the single most likely thing to break here.

---

## Providing hosts

Two ways, and they combine — `--hosts` for ad-hoc runs, `--hosts-file` for a
fleet you keep.

```bash
# a file (the normal way for anything beyond one device)
python3 trapSetter.py --device vip100g --hosts-file fleet.txt --check

# inline: repeatable, and comma- or space-separated within each value
python3 trapSetter.py --device vip100g --hosts 172.17.223.93,172.17.223.94 --check
python3 trapSetter.py --device vip100g --hosts 172.17.223.93 --hosts 172.17.223.94 --check

# both at once - the file plus one extra
python3 trapSetter.py --device vip100g --hosts-file fleet.txt --hosts 172.17.223.99 --check
```

When both are given, `--hosts` entries run **first**, then the file's.

`fleet.example.txt` in this directory is a documented template. The format:

| | |
|---|---|
| one host per line | an address or a resolvable name |
| blank lines | ignored |
| `#` | starts a comment, whole-line **or** trailing |
| several per line | fine — comma- or space-separated |
| duplicates | removed, first occurrence wins |
| order | preserved, and it is the order hosts run in |

So this file yields six hosts:

```
# vip100g fleet - studio A
172.17.223.93
172.17.223.94   # spare, currently powered down

172.17.223.95 172.17.223.96
172.17.223.97,172.17.223.98
172.17.223.93
```

Commenting a line out is the intended way to park a frame that is down for
maintenance without losing it from the list.

Hosts run **one at a time** by default. `--workers N` runs N concurrently; the
pacing delay is per-host, so raising it does not increase load on any single
device — it only shortens a fleet run's wall clock. Each host gets its own
discovery, login and report section, and one unreachable host never stops the
others.

## The parameter schema

Varids and target values are **not hardcoded**. At runtime they come from
`trap-params/<device>.json`, in which each group is a flat `name -> varid` map:

```json
{
  "device": "vip100g",
  "port_count": null,
  "groups": {
    "video":  {"target": 0, "per_port": true, "port_octet": 1,
               "params": {"Loss of Video": "400.0.0@i", "...": "..."}},
    "system": {"target": 1,
               "params": {"NTP Error": "850.18@i", "...": "..."}}
  }
}
```

| Field | Meaning |
|---|---|
| `target` | Per **group**, not per parameter — a notify section is enabled or disabled as a whole, which is how these pages are configured in practice. |
| `target_overrides` | `{name: value}` for members that disagree with the group target. Wins over `target`. Written by `--build-schema` when a group is mixed, rather than silently picking a majority. |
| `port_count` | Device input count for per-port expansion. `null`/absent/`1` = base varids only. The one knob an operator is expected to hand-edit. |
| `per_port` | Group is replicated across inputs. Guessed by `--build-schema`, written into the file so it is visible and correctable. |
| `port_octet` | Which dotted octet carries the input index (default `1`). |

A parameter's RPC type is **derived from its own varid suffix** (`@i` → integer),
never stored in the schema — one less field to keep in sync, and a group holding
mixed types needs no special handling.

```
@i, @d -> integer    @s -> string    @b -> boolean    @f -> float
```

**The schema is the file to hand-edit** when a device needs a varid added,
removed, or retargeted. Text exports are conversion input only; if no schema
exists yet the text export is read directly, with a note on stderr.

### Building a schema from an export

```bash
python3 trapSetter.py --device vip100g --build-schema
```

Reads `trap-params/<device>.csv` (or the older tab-separated `.txt`), writes the
JSON, then **immediately reloads it and diffs** against the parsed export — a
schema that cannot be read back is worse than no schema, and this is the only
moment both sides are in hand. A round-trip mismatch exits `1`.

The export parser is deliberately **column-position agnostic**, because the
exports disagree: the original `.txt` carries both a Control Trap Value and a
Control Fault Value column, the `.csv` keeps only the trap column. Varid cells
are recognised **by shape** (`^\d+(\.\d+)*@[a-z]$`) rather than by index — the
first is the trap varid, a second (if any) is the fault varid, and the target is
the trailing non-varid cell. Both shapes parse with no per-format branch, and an
export that adds a column still works. **Only the Control Trap Value is ever
written.**

Two export quirks are handled explicitly:

- **Encoding.** Exports arrive `cp1252`; the filler cells in a section-header row
  are non-breaking spaces (`0xA0`), which is not valid UTF-8 and hard-fails a
  plain `open()`. `utf-8-sig` is tried first so a genuinely UTF-8 file is never
  mangled.
- **Section headers.** A row with a name and no varids (`Video Notify`) opens a
  group whose key is the header's first word, lowercased — so `--groups
  video,audio` works for any device, including groups this module has never seen.

---

## Per-port expansion

An export describes exactly **one input**. On a device with many inputs the same
parameter repeats per input in the port octet:

```
530.0.0@i   channel 1 audio loss, input 1
530.1.0@i   channel 1 audio loss, input 2
```

`port_count` (or `--ports N`, which overrides it per run) is that input count.
Only groups marked `per_port` expand — this is what keeps chassis-level traps
from being multiplied. On the vip100g, `port_count: 64` takes video 4 → 256 and
audio 64 → 4096 while system stays at 27:

| | base | `--ports 64` |
|---|---|---|
| video (per-port) | 4 | 256 |
| audio (per-port) | 64 | 4096 |
| system (chassis) | 27 | 27 |
| **total** | **95** | **4379** |

Ports display **1-based** (matching the front panel and the WebEasy page) while
the varid octet is **0-based** — display port 1 is octet 0.

Expansion is **port-major** — every parameter for input 1, then input 2 — so a
run interrupted partway leaves the inputs it reached fully configured rather than
leaving all 64 half-done.

### Two guards worth knowing

- **`port_octet` must have an index octet after it.** A per-port varid is
  `<base>.<port>.<index>`. Rewriting a *trailing* octet would silently walk
  across unrelated parameters instead of across inputs — marking the chassis
  group per-port would turn `850.2@i` (CPU Usage) into `850.0`/`850.1`/`850.2`,
  three different traps. Those are distinct varids, so the collision check below
  cannot catch it. This guard is the only thing between a bad hand-edit and
  writing to the wrong parameters on real hardware. The parameter is refused and
  left unexpanded, with a warning.
- **Collision detection.** After expansion, two names claiming one varid is
  reported (first five, then a count) — the signature of a wrong `port_octet`.

`--build-schema` guesses `per_port` by requiring every varid in the group to be
three octets **with an identical middle octet** — the signature of one input
exported at index 0. It deliberately rejects the system group, whose varids are a
mix of four-octet network-port addresses and two-octet chassis addresses with a
varying middle octet.

### Pacing cost

4379 parameters is ~1314 requests per host, or **about 11 minutes** at the
default 0.5s delay. The startup banner states the estimate up front rather than
letting it be discovered at minute nine. `--brief` keeps the report to just the
rows that were not already correct.

---

## The run: three phases per host

This is what makes the report trustworthy.

| | Phase | Note |
|---|---|---|
| 1 | **GET** every targeted varid | current value |
| 2 | **SET** only the varids that differ | skipped entirely under `--check` |
| 3 | **GET** the varids just written | authoritative verification |

Because phase 3 re-reads the device, **`changed` means the device confirmed the
new value**, not merely that the RPC returned 200. Phase 1 makes the run
idempotent: a varid already at its target is reported `ok` and never written, so
a re-run against a converged fleet issues **no writes at all**.

### Status vocabulary

| Status | Meaning |
|---|---|
| `ok` | Already at target. Not written. |
| `changed` | Written **and** confirmed by read-back. |
| `would-change` | `--check` only: differs from target, nothing written. |
| `failed` | Written but read-back disagrees, or the set was rejected. |
| `unresolved` | Phase 1 read errored — the device cannot resolve this varid at all. Writing it would fail the same way, so the write is skipped and said so. Firmware variants genuinely lack some notify rows. `--set-all` writes anyway. |

Detail lines keep contradictions visible rather than papering over them: a varid
that verifies correctly but whose `set` returned a complaint reports
`changed  (verified, but set reported: ...)`.

A chunk-level RPC failure attaches to **every varid in the chunk**, so a dropped
request is never mistaken for a converged value.

### Comparison normalisation

Firmware is inconsistent about whether an `@i` control reads back as `0`, `"0"`
or `false`, so every flavour folds to an int before comparing (`_to_comparable`).
`None` means "no comparable value" and never compares equal to a target.

---

## Transport

Per host, once — not once per request. A trap run issues ~30 requests to the same
device; re-logging-in for each would triple the load this module exists to keep
low.

1. **Protocol** — `HEAD http://<host>` following redirects; falls back to HTTPS.
   `--proto` skips discovery.
2. **Endpoint** — probes `/cgi-bin/cfgjsonrpc` directly first (device
   generations disagree on 404 vs 200 for the `/cgi-bin/` directory even when
   `cfgjsonrpc` works, so the directory status is not a reliable gate). If that
   is absent, the version-stamped delegate paths are probed **newest first**
   from `_DELEGATE_ENDPOINTS`:

   | WebEasy | Path |
   |---|---|
   | 1.6 | `/v.1.6/php/datas/cfgjsonrpc.php` |
   | 1.5 | `/v.1.5/php/datas/cfgjsonrpc.php` |

   The version travels with the *device*, not with the device type, so one
   fleet routinely spans both and nothing needs configuring. A new WebEasy
   release is one row in that table — nothing else keys off the literal path.
   `--webeasy-version` narrows the probe to a single version for a device that
   answers on more than one path; the `cfgjsonrpc` probe still runs first
   either way, so pinning never locks out an older device in the same fleet.

   When nothing answers, the error names every path tried **and its status** —
   a 401 everywhere means bad credentials, a 404 everywhere means an unknown
   WebEasy version to add to the table:

   ```
   no usable endpoint: /cgi-bin/cfgjsonrpc unavailable,
   /v.1.6/php/datas/cfgjsonrpc.php (401), /v.1.5/php/datas/cfgjsonrpc.php (401)
   ```
3. **Auth** — decided by whether discovery settled on a delegate path
   (`webeasy_version is not None`), not by comparing the endpoint against a
   literal:
   - `cfgjsonrpc` (older): `GET /login.php`, take the `Set-Cookie` value, echo it
     back by hand with `; webeasy-loggedin=true`. Read off the header rather than
     via `session.cookies` so concurrent hosts do not race on a shared jar.
   - delegate (newer): HTTP Basic auth **per request** — not `session.auth`,
     which is one mutable attribute on a shared session and would race across
     hosts. Credentials come from `--user`/`--password`.

A stale session cookie is the one failure worth special handling: `rpc()`
re-logins once and replays the chunk. Everything else just retries
(`--retries`). Transport exceptions are condensed to one report-legible clause —
urllib3 nests the real cause inside a retry-pool message several layers deep, and
the innermost `[Errno …]` / `[WinError …]` clause is the only part an operator
acts on.

Responses map back onto sent varids by **echoed `id`**, with a positional
fallback for firmware that omits it on error — an errored parameter that cannot
be attributed would otherwise vanish from the report.

### Load protection

| Knob | Default | Note |
|---|---|---|
| `--chunk-size` | 10 | **Capped at 10, enforced not defaulted.** The WebEasy server degrades quickly under wide parameter sets. A larger value prints a note and is clamped. |
| `--delay` | 0.5s | Between requests **to the same device**, measured from the *completion* of the previous request — a slow RPC does not get to eat the pacing delay it was supposed to be followed by. |
| `--workers` | 1 | Hosts processed concurrently. The delay is per-host, so this does not increase load on any one device. |
| `--retries` | 1 | Per failed request chunk. |
| `--timeout` | `3,10` | connect,read. |

One shared pooled `requests.Session` with keep-alive (removes a TLS handshake per
request, which matters most here) and `verify=False`.

One bad host never takes down the rest of the fleet run — failures land in that
host's `error` field and it is reported `UNREACHABLE`.

---

## CLI reference

**Parameter source**

| Flag | Effect |
|---|---|
| `--device NAME` | Loads `trap-params/<device>.json`, falling back to `.csv`/`.txt` if no schema is built. Default `vip100g`. |
| `--params PATH` | Explicit source, overriding `--device`. Dispatched by extension. |
| `--groups a,b` | Apply only these groups. Default: all. Unknown group is an error listing what the file has. |
| `--ports N` | Override the schema's `port_count`. |
| `--force-false` | Override every target to `0`, ignoring `TRUE` entries. |
| `--list` | Print the parsed set and exit. Touches no devices. |
| `--build-schema [OUT]` | Convert export → JSON schema and exit. `-` for stdout. |

**Targets:** `--hosts` (repeatable, comma/space-separated), `--hosts-file` (one
per line, `#` comments), `--user`, `--password`, `--proto {http,https}`,
`--webeasy-version {1.6,1.5}`.

**Behaviour:** `--check`, `--no-verify`, `--set-all`, `--chunk-size`, `--delay`,
`--workers`, `--retries`, `--timeout`.

**Output:** `--report PATH` (default `trap-report-<device>-<timestamp>.txt`),
`--json PATH`, `--brief`, `--quiet`.

Order of operations in `main()` matters and is deliberate: **group filter → port
expansion → `--force-false`**. Filtering first means a `--groups video` run does
not build 4379 traps and discard most of them; `--force-false` last means the
override lands on every replica.

---

## Reports

The text report is written **always** (even on failure), plus optional JSON.
Structure: run header (mode, params file, group counts, ports note, pacing knobs,
parse warnings) → per-host listing grouped by group and, under expansion,
sub-headed per port → `RECAP` table → `TOTAL` line → `RESULT`.

The JSON report carries the same data with a **stable shape** — every status key
is seeded, so a run with no failures still reports `failed: 0` rather than
omitting the key and breaking whatever reads it. Each trap record carries `port`
(`null` when unexpanded) so a consumer can filter by input without parsing the
port out of the name, and `before`/`after` for every varid.

Each host record carries `proto`, `endpoint` and `webeasy_version` (`null` on
the `cfgjsonrpc` path), so a fleet run doubles as a firmware-version inventory.

---

## Adding a device

1. Export the device's **Notify** page from WebEasy to `trap-params/<name>.csv`.
2. `python3 trapSetter.py --device <name> --build-schema`
3. Read the generated JSON. Check the `per_port` guesses and the group `target`s;
   correct by hand. Set `port_count` if the device has multiple inputs.
4. `python3 trapSetter.py --device <name> --list` to confirm the expanded set.
5. `--check` against one host before applying.

Unlike ptpMon there is **no device registry to edit** — `--device` is just a
filename stem, so a new device is a new file and no code change.

---

## Constraints

- **Python 3.6 compatible** — no f-strings anywhere, `%`-formatting throughout,
  matching the poller platform's floor (see the repo's `Remove statistics to
  support python 3.6` commit). Keep it that way.
- Dependencies: `requests`, `urllib3`. No stdlib beyond 3.6.
- `extras/` is gitignored, so this directory is **local-only** and not in the
  repo's history.
