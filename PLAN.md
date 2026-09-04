# trapSetter — plan

## Where we are

The tool is feature-complete offline and unproven online.

**Done and verified without a device:**

- Export parsing (`.csv` + tab-separated `.txt`, cp1252, column-agnostic).
- `--build-schema` with an immediate round-trip diff.
- Per-port expansion, with the trailing-octet refusal and the collision check.
- `--list`, `--groups`, `--ports`, `--force-false`.
- Report rendering (text + JSON), recap, exit status.
- `vip100g`: 95 base params → 4379 at `--ports 64`.
- Endpoint discovery: cgi-bin first, then WebEasy 1.6/1.5 newest-first, with
  `--webeasy-version` to pin (P1-1, verified against a mocked transport).

**Never executed:** the `set` RPC. Everything downstream of it — the phase-2/3
write-verify loop, the `changed`/`failed` distinction, the cookie-replay retry,
delegate Basic auth — has only ever run against the code, not a device.

That is the gate. Nothing below P0 is worth building until P0 answers.

---

## P0 — Hardware validation

### P0-1. One varid, both directions

Run the five-step sequence in
[README.md → Single-varid smoke test](README.md#single-varid-smoke-test) against
a single lab vip100g. It uses `trap-params/probe.json` (`400.0.0@i`, Loss of
Video, input 1) and covers: discovery → auth → `get` → `set 1` → UI
confirmation → `set 0` → converged re-check.

Record, from `probe-set-1.json`:

| What | Why it matters |
|---|---|
| `proto`, `endpoint` | Which of the two auth paths this firmware takes. Both need proving; a lab with only one generation proves only half. |
| `requests` | Should be exactly 3 (get, set, verify get). More means a retry fired. |
| `before` / `after` | The read-back is the whole basis of the `changed` status. |
| exact `set` reply shape | Feeds P1-3. Capture the raw JSON if it deviates. |

**Pass:** step 2 reports `changed`, the UI agrees, step 5 reports `ok`.

### P0-2. One chunk, mixed outcomes

Extend the probe schema to ~12 varids, deliberately including one that does not
exist on this firmware (a bogus index). This is the first test of behaviour the
single-varid case cannot reach:

- Chunking at the boundary (12 params → 2 requests of 10 + 2).
- `unresolved` vs `failed` — does the device error the one bad varid, or reject
  the whole chunk? If the latter, one missing notify row poisons nine good ones
  and chunking needs a fallback-to-singles retry.
- Whether `set` replies echo `id` per parameter (P1-3).

### P0-3. Full device, check then apply

`--check` the full 95-param `vip100g` schema, read the report, then apply.
Confirm the recap totals reconcile and that a second apply is a no-op
(`ok=95`, zero writes).

### P0-4. Expansion, one host

`--ports 64 --check` on one host, `--brief`. Two things to watch:

- **Wall clock** against the banner's estimate. 4379 params ≈ 438 requests in
  check mode, ~4 minutes at 0.5s. If the device's real response time dominates
  the pacing delay, the estimate is wrong and `--delay` guidance needs revising.
- **Device health during and after** — CPU, web UI responsiveness. The chunk cap
  of 10 and the 0.5s delay are assumptions, not measurements. This is the run
  that turns them into measurements.

Then apply to one host and spot-check three inputs in the UI (1, 32, 64) to
prove the port-octet arithmetic is right on real hardware and not just in the
collision checker.

---

## P1 — Fixes hardware testing will likely force

Ordered by how likely P0 is to demand them.

### P1-1. Delegate endpoint pinned to WebEasy 1.5 — **DONE (2026-09-04)**

`_check_endpoint` knew exactly one delegate path, hardcoded in three places, so
any WebEasy 1.6 device failed endpoint discovery outright. Fixed by lifting
ptpMon's approach:

- `_DELEGATE_ENDPOINTS` module table (1.6, then 1.5), probed **newest first**,
  with a note that ptpMon is upstream for every transport decision here.
- `_check_endpoint` now returns `(path, version)`; `uses_basic_auth` keys off
  `webeasy_version is not None` instead of an equality test against one
  literal, so a new row in the table needs no other change.
- Exhaustion error names every path tried and its status (401 everywhere = bad
  credentials, 404 everywhere = unknown version to add).
- `--webeasy-version {1.6,1.5}` narrows the probe, validated against the table
  at startup. The `cfgjsonrpc` probe still runs first, so a pin never locks out
  an older device in the same fleet.
- `webeasy_version` added to each host's report record, making a fleet run
  double as a firmware inventory.

Verified offline against a mocked transport (17 checks): cgi-bin wins and skips
delegate probing; a 1.6-only device resolves; 1.5-only still resolves with 1.6
probed first; both present → 1.6 wins; a pin selects and skips the other; a pin
still allows cgi-bin; a connection error on cgi-bin falls through; exhaustion
and 401-everywhere produce legible errors.

### P1-2. Does `set` need real credentials on the cfgjsonrpc path?

`_login` (`:760`) does what ptpMon's `fetch` does: `GET /login.php`, keep the
`Set-Cookie`, send it back with `webeasy-loggedin=true`. It never presents
`--user`/`--password`. That is sufficient for ptpMon because ptpMon only ever
calls `get`.

`set` may well require an authenticated session. If P0-1 step 2 reads cleanly and
then fails to verify, this is why.

**Fix if needed:** POST credentials to `login.php` (or call the RPC `login`
method — CLAUDE.md describes ptpMon as doing this, though the code does not, so
the service likely exposes it) before the first `set`. Keep it conditional: an
unauthenticated cookie that already works should not start needing credentials.

### P1-3. `map_response` positional fallback is unsafe under partial replies

`map_response` (`:869`) keys on the echoed `id` and falls back to the **index in
the reply array** for entries without one. That fallback is only correct if the
device returns one entry per sent parameter, in order. If firmware returns *only*
the errored parameters, index 0 of the reply is not `sent[0]` and the error is
attributed to the wrong varid — which under `set` means a report that names the
wrong parameter as failed.

**Fix:** only use the positional fallback when
`len(parameters) == len(sent)`; otherwise mark the unattributable entries against
the chunk as a whole. P0-2 tells us which shape the device actually emits.

### P1-4. `--varids` flag

P0 needs a one-parameter run and the answer today is a hand-written schema file.
That is fine once, awkward as a habit.

**Fix:** `--varids 400.0.0@i,850.18@i` filtering the loaded set after expansion,
plus `--target N` to override the schema target in either direction (`--force-false`
becomes `--target 0`, kept as an alias). Small, and it makes every future
debugging session a one-liner.

### P1-5. Retry loop retries the unretryable

`rpc` (`:838`) retries every `RpcError`, including HTTP 4xx and malformed JSON,
and attempts a re-login on each. A 401 becomes two 401s and a wasted login.

**Fix:** classify — retry timeouts, connection errors and 5xx; fail fast on 4xx
other than 401/403 (which get exactly one re-login, as now).

---

## P2 — Operator safety

These matter the moment someone runs `--ports 64` against more than one host.

### P2-1. Survive Ctrl-C with a report

An 11-minute run interrupted at minute nine currently loses everything: no
report is written, and there is no record of which inputs were reached.
Port-major expansion was designed so a partial run is *coherent* — but only if
you can find out where it stopped.

**Fix:** catch `KeyboardInterrupt` in `run()` (`:1120`) and in `main()`, mark the
in-flight host `interrupted`, and render the report from whatever completed.

### P2-2. Revert from a JSON report

Every trap record already carries `before`. There is no way to feed it back.

**Fix:** `--restore run.json`, which builds the trap set from the report's
`before` values instead of a schema, then runs the normal three-phase loop. This
is the undo button for a 4379-parameter mistake, and the data for it is already
being written.

### P2-3. Confirmation prompt for large applies

An apply above some threshold (say 500 varids, or any run without `--check`
having preceded it) should require `--yes` or a typed confirmation. Cheap;
prevents the obvious accident.

---

## P3 — Coverage and confidence

### P3-1. Unit tests — **started (2026-09-04)**

`test_trapSetter.py` exists: 32 `unittest` tests, ~4ms, no network. It covers
endpoint discovery and the auth path it selects — the transport decision
duplicated from ptpMon, hence the one most likely to drift, and the site of the
P1-1 bug.

Confirmed to fail on regression, not merely to pass: reversing
`_DELEGATE_ENDPOINTS` fails 4 tests, deleting the 1.6 row (reintroducing the
original bug) fails 9, and reverting `uses_basic_auth` to a literal path
comparison fails 2.

Still to cover — each drops in as a new TestCase:

| Function | What to pin down |
|---|---|
| `_coerce_target` | `FALSE`/`0`/`disabled`/`0x1`/junk, per suffix |
| `_to_comparable` | `0` vs `"0"` vs `False` vs `""` vs `None` all folding correctly |
| `parse_export` | both export shapes, cp1252 NBSP headers, malformed rows → warnings |
| `expand_ports` | port-major order, trailing-octet refusal, collision detection, `None`/`0`/`1` no-op |
| `detect_per_port` | video/audio yes, system no |
| `map_response` | id-keyed, positional, partial reply (see P1-3) |
| round-trip | `parse_export` → `build_schema` → `load_schema` identity |

`unittest` (3.6-safe, no new dependency). Worth finishing before the P1
refactors, and worth more than usual here: `extras/` is gitignored, so this
code has no git history to fall back on.

### P3-2. More device schemas

Only `vip100g` exists. ptpMon supports ~22 device types. Each new one is a
Notify-page export plus `--build-schema` plus a hand-check — no code change.
Prioritise whatever the fleet actually has most of.

### P3-3. Share transport with ptpMon, or don't

`HostSession` is a near-copy of ptpMon's `checkProto`/`checkEndpoint`/`fetch`/
`auth_fetch`, and P1-1 is a direct consequence of that copy drifting. Options:

- **Extract** a small shared transport module both import. Correct, but ptpMon
  deploys into the poller's module directory and gains a dependency.
- **Accept the duplication** and add a note at the top of `HostSession` naming
  ptpMon as the upstream to re-sync from.

Given the repo's existing convention — non-Evertz vendors get a *standalone*
`<vendor>Mon.py` reusing the interface rather than merging — the second is more
in keeping. Decide once and write it down either way.

---

## Suggested order

```
P1-1  done
P0-1  one varid, both directions          <- the actual next step
P0-2  one chunk, mixed outcomes
P1-2  credentialed write, if P0-1 fails to verify
P1-3  map_response, informed by P0-2
P0-3  full 95-param check then apply
P1-4  --varids / --target
P0-4  --ports 64, watch device health
P2-1  Ctrl-C report
P3-1  unit tests — endpoint discovery done, pure functions outstanding
then P2-2, P2-3, P1-5, P3-2, P3-3
```
