# trapSetter — plan

## Where we are

The write path is proven on hardware. P0-1 and P0-2 both passed against a
vip100g on 2026-09-08; what remains in P0 is scale, not viability.

**Done and verified without a device:**

- Export parsing (`.csv` + tab-separated `.txt`, cp1252, column-agnostic).
- `--build-schema` with an immediate round-trip diff.
- Per-port expansion, with the trailing-octet refusal and the collision check.
- `--list`, `--groups`, `--ports`, `--force-false`.
- Report rendering (text + JSON), recap, exit status.
- `vip100g`: 95 base params → 4379 at `--ports 64`.
- Endpoint discovery: cgi-bin first, then WebEasy 1.6/1.5 newest-first, with
  `--webeasy-version` to pin (P1-1, verified against a mocked transport).

**Verified on hardware (2026-09-08, 172.17.223.93, WebEasy 1.5):** protocol
and endpoint discovery, delegate Basic auth, `get`, `set` in both directions,
the phase-2/3 write-verify loop, the `ok` / `changed` / `would-change` /
`unresolved` statuses, idempotence, `target_overrides`, chunking with mixed
outcomes, and exit status.

**Still never executed:** the `cfgjsonrpc` cookie-login path's *write*
behaviour (that device resolved to the delegate endpoint — see P1-2), the
`failed` status, the cookie-replay retry, and any apply wider than 12
varids. The widest *read* so far is 256.

The gate is open.

---

## P0 — Hardware validation

### P0-1. One varid, both directions — **PASSED (2026-09-08)**

Ran against 172.17.223.93 with `trap-params/probe.json` (`400.0.0@i`, Loss of
Video, input 1). Reports in `logs/probe-check.*`, `logs/probe-set-0.*`,
`logs/probe-set-1.*`.

The README sequence needed **reversing**: the varid already held 1, so "set it
to 1" was a no-op that phase 1 skipped, proving nothing about `set`. Ran
`--force-false` first (a genuine `1 -> 0` write), confirmed the UI had gone
unticked, then restored it. README now documents this.

| What | Recorded |
|---|---|
| `proto`, `endpoint` | `https`, `/v.1.5/php/datas/cfgjsonrpc.php` — the **delegate/Basic-auth** path, so the cgi-bin cookie path is still unproven (P1-2). |
| `webeasy_version` | `1.5` |
| `requests` | Exactly **3** on both writes. No retry fired. |
| `before` / `after` | `1 -> 0`, then `0 -> 1`. Both confirmed by the phase-3 read-back, and independently by a raw `get` issued outside the tool. |
| `set` reply shape | `{"id":"400.0.0@i","status":"success","type":"integer","value":1}` — note the `status` field, which `map_response` ignores (P1-3). |

Step 5's converged re-check reported `ok` in **1 request with zero writes**, so
idempotence holds on real hardware.

Also settled: the 0/1 mapping is **not** inverted — the one bug class a report
full of green `changed` rows would happily hide. `--force-false` asked for `0`,
the device read back `0`, and the UI went unticked: three independent
agreements.

### P0-2. One chunk, mixed outcomes — **PASSED (2026-09-08)**

Done with **5 varids, not 12**: the four video params plus one bogus index
(`400.0.99@i`) placed *third* so it sat mid-chunk, as
`trap-params/probe4.json`. Chunking was exercised with `--chunk-size 2` (3
chunks) instead of padding the schema out to cross the default boundary.
Reports in `logs/probe4-*`.

`ok=1 changed=3 unresolved=1`, reconciling to 5, in **7 requests** (3 phase-1 +
2 phase-2 + 2 phase-3, so no retries):

```
[changed     ] Loss of Video      400.0.0@i   1 -> 0
[changed     ] Video Frozen       400.0.1@i   1 -> 0
[unresolved  ] BOGUS index 99     400.0.99@i  - (want 0)  (read failed: Failed to process request)
[changed     ] Video Black        400.0.2@i   1 -> 0
[ok          ] Motion Detected    400.0.3@i   1
```

- **No chunk poisoning.** The bad varid shared a chunk with Video Black and did
  not disturb it, in either the read or the write. **The fallback-to-singles
  retry is not needed** — that contingency is dropped.
- **`unresolved` behaves as designed:** read error, write skipped, stated in
  the report, exit 1.
- **`set` replies do echo `id` per parameter**, errored entries included (P1-3).
- `target_overrides` exercised on hardware for the first time — it is what
  held Motion Detected at 1, producing an `ok` alongside the `changed` rows.

State was restored to its pre-test values afterwards, confirmed by a read
issued outside the tool.

**Still not produced on hardware: `failed`.** It is reachable only via a verify
mismatch or a rejected `set`. A `--set-all` run over `probe4.json` would force
it, since that writes the bogus varid instead of skipping it, and would confirm
`unresolved` becomes `failed`.

### P0-3. Full device, check then apply — **CHECK PASSED (2026-09-08)**

`ok=17 would-change=68 unresolved=10`, reconciling to 95, in 10 requests /
5.8s. Report in `logs/p03-check.*`. The recap totals reconcile.

The 10 `unresolved` are **absent hardware, not tool error**: Main Port 2 and
Backup Port 2 (`340.0.1.*`, `340.1.1.*`) and Fans J30-J35 (`850.12`-`850.17`).
This frame has one port per main/backup and only fans J28/J29. All 17 `ok`
are system traps already at 1; all 68 `would-change` are video/audio at 1
against a target of 0.

**The apply half was deliberately not run** (instructed to check only), so
*a second apply is a no-op* is still unverified. That is the only part of
P0-3 outstanding.

### P0-4. Expansion, one host — **PASSED, 3 ports (2026-09-08)**

Scoped to display ports **1, 32 and 64** rather than all 64, via
`trap-params/probe-ports.json`. Ports 1-3 (`--ports 3`) were rejected as the
scope: they exercise only octets 0-2 and miss the high end, which is the
whole point. Reports in `logs/p04-*`.

| What | Result |
|---|---|
| Expansion arithmetic | `--ports 64 --list` emits `400.0.*` / `400.31.*` / `400.63.*` for display ports 1/32/64, matching varids read independently — so the fixture tests `expand_ports`, not hand arithmetic. |
| All 64 inputs resolve | read-only check of all **256** expanded video varids: `would-change=256`, **zero unresolved**. |
| Wall clock | 26 requests in 14.8s against the banner's >= 13s. Device response adds ~0.07s/request, so **the pacing estimate holds** and `--delay` guidance stands. A full 4379-param apply extrapolates to ~12.5 min vs the documented ~11. |
| Apply | 12 varids across ports 1/32/64: `changed=12`, 6 requests, exit 0. |
| Collateral | **none** — neighbours 2, 31, 33, 63 all still at 1 after the write. |

UI spot-check confirmed inputs 1 and 32. Input 64 is not reachable in this
unit's UI, so the arithmetic is UI-confirmed to 32 and API-confirmed to 64.
State was restored to pre-test values afterwards.

**Not measured:** device CPU and web-UI responsiveness during the run. The
chunk cap of 10 and the 0.5s delay remain assumptions; only the *pacing
estimate* was turned into a measurement, not the load ceiling.

#### Firmware observation: the port octet is capacity, not population

This test unit has **32 physical inputs**, but the API accepted, stored and
read back a write to `400.63.0@i` (display input 64). An octet sweep pins the
behaviour down:

```
port octet  (2nd)   valid 0-63,  errors from 64   -> family capacity, 64 inputs
index octet (3rd)   valid 0-3,   errors from 4    -> the 4 video params
```

So for a per-port group, *does this varid resolve?* answers **is it within
capacity**, not *is it physically present*. Chassis groups are the opposite:
P0-3's absent fans and ports resolved as `unresolved` precisely because they
reflect population. One rule does not cover both.

The consequence is that `--ports 64` on a 32-port card would report
`SUCCESS` with `changed=2048`, half of it phantom, and the three-phase
design cannot catch it: phase 3 re-reads the same storage the write went
into and correctly confirms the value.

**Decided: left as is, no guard.** trapSetter is a power-user tool and the
operator is expected to check the frame's port indices before an apply.
Recorded here so it is not rediscovered on a 32-port card.

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

**P0-1 did not exercise this.** 172.17.223.93 resolved to the delegate
endpoint, so it authenticated with per-request Basic auth and never called
`_login` at all. Proving this path needs an older-generation frame that answers
on `/cgi-bin/cfgjsonrpc`. Until one is available the write path is validated
for delegate firmware only — **not fleet-wide**, and P0-1 should not be read
as saying otherwise.

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
`len(parameters) == len(sent)`; otherwise mark the unattributable entries
against the chunk as a whole.

**P0-2 answered the shape question: this is not triggered on WebEasy 1.5.**
Every entry echoes its `id`, errored ones included, and replies arrive one per
sent parameter in order — so the fallback is unreachable there, and would
attribute correctly even if reached. The fix is still worth having as defence
against other firmware, but it is no longer urgent and should not be written
blind against a shape no device has emitted. Deferred.

One related gap P0-2 did surface: `map_response` reads only `value` and
`error`, ignoring the `status` field that `set` replies carry, so a
non-success `status` with no `error` key would read as clean. Phase 3's
read-back catches it, which is exactly the design rationale — a note, not a
bug.

### P1-4. `--varids` flag — **DONE (2026-09-08)**

`--varids 400.0.0@i,850.18@i` filters the loaded set **after** expansion, and
`--target VALUE` overrides the schema target in either direction.
`--force-false` is now an alias for `--target 0` and is refused alongside it.

Details that matter:

- **Filtering after expansion** is what lets a token name one input
  (`400.31.0@i`). A pre-expansion filter could not.
- **The bare address is accepted** (`850.18`), since the `@suffix` adds
  nothing — the type is derived from the varid anyway.
- **An unmatched varid is an error.** Silently selecting nothing would be the
  worst outcome available: the run would report a clean `SUCCESS` having
  written nothing at all.
- **`--target` is coerced per suffix**, not once, so one value suits a set
  mixing `@i` and `@s`. A value no suffix can take is an error rather than a
  quietly skipped parameter.
- Groups the filter empties are dropped from the report header, and
  `ports_note` reports what *expansion* produced (95 → 4379) with the
  narrowing stated separately (`3 of 4379 selected by --varids`). Measuring
  the post-filter length there read as though expansion had produced 3.

Verified on 172.17.223.93: `--ports 64 --varids 400.0.0@i,400.31.0@i,850.18@i`
selects 3 of 4379 and runs in one request (`logs/p14-varids.*`). 16 unit
tests.

This replaces the hand-written probe schema as the way to scope a run, though
`probe.json` / `probe4.json` / `probe-ports.json` are kept as documented
fixtures.

### P1-5. Retry loop retries the unretryable

`rpc` (`:838`) retries every `RpcError`, including HTTP 4xx and malformed JSON,
and attempts a re-login on each. A 401 becomes two 401s and a wasted login.

**Fix:** classify — retry timeouts, connection errors and 5xx; fail fast on 4xx
other than 401/403 (which get exactly one re-login, as now).

---

## P2 — Operator safety

These matter the moment someone runs `--ports 64` against more than one host.

### P2-1. Survive Ctrl-C with a report — **DONE (2026-09-08)**

`run_host` absorbs the interrupt for the host in flight; `run()` records the
hosts after it as `not_started`; `main()` keeps a backstop for one landing
between hosts. The report is written either way and exit status is `1`.

The design decisions worth remembering:

- **A new `skipped` status.** The finalize loop used to turn any unset status
  into `ok`, which after an interrupt would report a varid as converged
  without having looked at it. That loop moved out of the `try` block so an
  interrupted host still reports, and an unset status now means `skipped`
  when interrupted and `ok` otherwise.
- **`not_started` is distinct from `unreachable`.** A host that never ran is
  not a network fault, and omitting those hosts would silently shorten the
  recap.
- **A `pending` count.** Records are not built until phase 1 completes, so an
  interrupt there leaves no per-varid rows. Reporting `0 never reached` for
  4096 unassessed varids was the first version's bug; the count is now
  explicit (`4096 parameter(s) never assessed`).
- **The phase is recorded**, so the error names where it stopped, and a
  phase-1 interrupt states `no write was issued` — phase 1 only reads, so the
  device is untouched and there is nothing to be partway through.
- **Worker threads learn via a flag** checked in `_pace`, since
  `KeyboardInterrupt` reaches only the main thread. Without it a `--workers`
  run would keep issuing requests to every remaining host, and the pool's
  shutdown-wait would block until the whole fleet finished.

Verified on 172.17.223.93 with a real `KeyboardInterrupt` (via
`_thread.interrupt_main`, which is what Ctrl-C raises):

| Interrupt point | Outcome |
|---|---|
| phase 1, 4096-varid check, killed at 4s of a 3m25s run | report written, `interrupted during read; no write was issued`, `4096 parameter(s) never assessed`, exit 1 (`logs/p21-read.*`) |
| phase 3, `--set-all` writing values already held | 4 `skipped` rows reading `write issued but not verified before the interrupt`, exit 1, **device state unchanged** (`logs/p21-verify.*`) |

14 unit tests, confirmed to fail on regression: an unreached varid falling
back to `ok` fails 1, dropping the not-started placeholders fails 4.

**Known limitation:** a phase-1 interrupt discards the partial reads it had
already collected, so it yields no per-varid rows — only the count. Salvaging
them would mean restructuring `_read` to accumulate into a caller-owned dict.
Left alone because phase 1 writes nothing, so there is no device state to
reconcile.

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

### P3-1. Unit tests — **started (2026-09-04), extended (2026-09-08)**

`test_trapSetter.py`: **62** `unittest` tests, ~20ms, no network. Covers
endpoint discovery and the auth path it selects — the transport decision
duplicated from ptpMon, hence the one most likely to drift, and the site of the
P1-1 bug — plus `--varids`/`--target` (P1-4) and interrupt handling (P2-1).

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
P0-1  done - one varid, both directions
P0-2  done - mixed outcomes, no chunk poisoning
P0-3  done - check only; the apply half is still outstanding
P0-4  done - 3 ports; device health not measured
P1-4  done - --varids / --target
P2-1  done - Ctrl-C report
P3-1  unit tests — 62 now; the pure functions are still outstanding
P0-3b full 95-param apply, then re-apply as a no-op
P1-2  credentialed write — blocked, needs an older frame
P1-3  map_response — deferred, not triggered by WebEasy 1.5
then P2-2, P2-3, P1-5, P3-2, P3-3

P1-4 and P2-1 were the two the tool was not to ship without. Both are done
and neither needed anything above them. What remains before a wider rollout
is P0-3b (the 95-param apply and its no-op re-apply) and P3-1's pure-function
coverage; P1-2 stays blocked on an older frame.
```
