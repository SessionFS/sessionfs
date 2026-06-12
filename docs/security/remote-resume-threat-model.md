# Remote Resume — Threat Model & Binding Security Design (Phase 1a)

**Status:** BINDING for Atlas implementation. Sentinel-authored security design.
**Ticket:** `tk_b216fc3e30534bc0` (parent Issue: Remote Resume product shape).
**Scope:** The Phase 1 *device command channel* — a web click on the SessionFS dashboard enqueues a command that the user's local daemon (`sfsd`) polls, claims, and executes, launching `sfs resume <session_id> --in <tool>` on the user's machine.

This is **web-click → local process execution**. It is the most security-sensitive primitive SessionFS will have shipped. Every decision below is fail-closed by default. Atlas implements verbatim; deviations require a new Sentinel review.

---

## 0. Binding Key-Decision Constraints (inherited, non-negotiable)

- HTTP + ETag long-polling ONLY. No WebSockets, no Redis, no server push. The daemon already polls authenticated (`DaemonSyncer._fetch_remote_settings`, `src/sessionfs/daemon/main.py:359`). The command channel rides the **same poll model**.
- No server-side LLM keys. The server never runs the model; it only enqueues a *command record*. Execution is local.
- Local-first / opt-in. Remote execution is OFF by default and requires an explicit, revocable, per-device opt-in.
- Server-side authorization is authoritative. UI/CLI checks are convenience only.
- Public/authenticated mutating endpoints must be rate-limited at a layer that survives horizontal scaling (the current `SlidingWindowRateLimiter` is in-memory per-replica — see §5.7 and the Forge hand-off).

---

## 1. Assets

| # | Asset | Why it matters |
|---|-------|----------------|
| A1 | **Local process execution on the user's machine** | The daemon runs with the user's OS privileges. Anything that controls the argv controls the user's shell-equivalent. RCE-grade. |
| A2 | **The argv passed to `subprocess.run`** (`cmd_ops.py:299/353/420`) | If any attacker-controlled string reaches the process list, it is command injection. |
| A3 | **The set of registered devices for a user** | Each device is an execution target. Fake/extra devices = new attack surface. |
| A4 | **Device opt-in state (`allow_remote`)** | The single switch between "inert" and "armed." Downgrade/forced-on is privilege escalation. |
| A5 | **The command queue rows** (pending/claimed/executed) | Replay, race, and TTL abuse all target these. |
| A6 | **Device-binding credential** (the secret that authenticates a daemon as a specific device) | Theft → daemon impersonation → claim commands meant for the victim, or register a rogue execution target. |
| A7 | **API keys** (`ApiKey`, hashed in `src/sessionfs/server/auth/keys.py`) | Already grant cloud access; under this feature a stolen key must NOT silently gain remote-exec on devices. |
| A8 | **Audit trail** (who enqueued/claimed/executed what, on which device, when) | The only forensic record after an incident. Must survive user/session/device deletion. |
| A9 | **Session content + workspace paths** | `sfs resume` reads the session and launches a tool in the workspace dir. The command implicitly exposes/acts on this. |

---

## 2. Actors

| Actor | Trust | Relevant capability |
|-------|-------|---------------------|
| **Legitimate user** (owner) | Trusted for own sessions + own devices | Enqueue resume onto their own opted-in device. |
| **Stolen-API-key attacker** | Hostile, holds a valid user key | Can call any endpoint the key's scope allows. PRIMARY threat: must NOT reach remote-exec without the device opt-in + (§5.1) a separate trust path. |
| **Malicious org admin** | Authenticated, org-admin role | Wants to enqueue onto *member* devices. **DENIED in v1** (§4, §6). |
| **Malicious teammate** | Authenticated org member | Wants to enqueue onto another member's device / target sessions they don't own. Denied by the ownership predicate (§3.4). |
| **Compromised CI / cloud service token** | Holds a *service* key | Service keys must be **structurally barred** from the enqueue + device endpoints (§5.1). |
| **Web attacker (CSRF / clickjacking)** | Can make the victim's browser issue requests / clicks | Wants the victim to enqueue a command unknowingly (§5.2). |
| **Network attacker** | On-path | TLS already mitigates; in-scope only for replay if app-layer nonces are absent (§5.5). |
| **Rogue daemon / fake device** | Holds (or guesses) a device identity | Wants to register as a victim's device, or claim a command not addressed to it (§5.8). |

---

## 3. Entry Points & Trust Boundaries

### 3.1 New entry points (all server-side, all authenticated)
1. `POST /api/v1/devices` — device registration (daemon → server).
2. `GET  /api/v1/devices` / `DELETE /api/v1/devices/{device_id}` — list / revoke.
3. `POST /api/v1/devices/{device_id}/allow-remote` + `DELETE` (or a PUT toggle) — opt-in / opt-out.
4. `POST /api/v1/devices/{device_id}/commands` — **enqueue** (dashboard → server). Highest-risk.
5. `GET  /api/v1/devices/{device_id}/commands/poll` — daemon long-poll for pending commands (ETag).
6. `POST /api/v1/devices/{device_id}/commands/{cmd_id}/claim` — atomic single-claim.
7. `POST /api/v1/devices/{device_id}/commands/{cmd_id}/result` — execution result / failure report.

### 3.2 The one new dangerous *local* entry point
- The daemon poll loop, on claiming a command, **constructs an argv and calls `subprocess.run`**. This is the boundary where a server-trusted record becomes a local process. The daemon MUST treat the claimed command as **untrusted input** and rebuild argv from validated parts (§4.4) — it must never trust that the server validated it.

### 3.3 Trust boundaries crossed
- **Cloud → local execution** (NEW and the sharpest boundary in the whole product).
- Browser → server (CSRF surface on enqueue).
- Server → daemon (poll/claim authenticity).
- User → org (org-admin must not cross into member devices).
- User key → service key (service keys barred).

### 3.4 The authorization invariant (enqueue)
A command to run `resume <session_id>` on `device_id` is authorized **iff ALL hold**:
1. Caller is authenticated with a **user** key (not service — §5.1).
2. `device.user_id == caller.id` (device ownership). No org override.
3. `session.user_id == caller.id` (session ownership; `Session.user_id` is NOT NULL — `models.py:140`). No org override.
4. `device.allow_remote is True` (opt-in armed).
5. `device.revoked_at is None` and the device is active.

Org membership grants **zero** additional power here. This is the explicit v1 denial (§6).

---

## 4. STRIDE Threat Analysis

Legend for mitigation owner: **[A]** Atlas (route/daemon/migration), **[F]** Forge (platform), **[P]** Prism (dashboard/CSRF UI), **[S]** Sentinel (review).

### 4.1 Spoofing

| Threat | Vector | Mitigation |
|--------|--------|------------|
| **Daemon impersonation** | Attacker registers a device or claims commands as the victim. | Device identity bound to a **per-device secret** issued once at registration (§5.3). The poll/claim/result endpoints require the device secret AND the user's API key. A command is only delivered to the device it is `device_id`-addressed to; claim checks `command.device_id == authenticated_device_id`. **[A]** |
| **Fake device registration** | Stolen API key registers a *new* device, opts it in, enqueues to it. | Registration is allowed (the key owner can add devices), but (a) the new device starts `allow_remote=False` (§5.4 default-off), and (b) opt-in (`allow-remote`) requires a **second, deliberate user action** that is rate-limited and audited, and SHOULD be confirmable from an *already-trusted* device or the dashboard with re-auth. A freshly registered device cannot self-arm in one call. **[A]** + **[S]** |
| **Server impersonation to daemon** | On-path server spoof feeds malicious commands. | TLS pinning is out of scope for v1; rely on HTTPS to `api.sessionfs.dev`. Residual risk documented (§7). |

### 4.2 Tampering

| Threat | Vector | Mitigation |
|--------|--------|------------|
| **Command-injection via `session_id`** | `session_id` carries shell metacharacters / extra args that reach `subprocess.run`. | Server validates `session_id` against `SESSION_ID_RE` (`^ses_[a-z0-9]{8,40}$`, `src/sessionfs/session_id.py:12`) at enqueue, AND the daemon re-validates with the SAME regex before building argv. The daemon builds argv as a **list of literals** `["sfs","resume", session_id, "--in", tool]` — never a shell string, never `shell=True`. The existing `subprocess.run(cmd, ...)` call sites already pass a list and never use a shell; the daemon path must preserve that. **[A]** |
| **Command-injection via `tool`** | Free-form tool string reaches argv. | `tool` is a **closed enum** validated server-side AND daemon-side against the known set `{claude-code, codex, copilot, gemini}` (the four bidirectional resume targets in `cmd_ops.py`; capture-only tools rejected). No free-form string ever reaches exec. **[A]** |
| **Argument smuggling via params object** | A future param (model, project path) injects `--dangerously-skip-permissions` or a flag. | v1 command schema is a **fixed allowlist** (§4.4). No `model`, no `project`, no extra flags accepted in v1. The daemon ignores any field not in the v1 schema. Adding params is a future ticket with its own validation. **[A]** + **[S]** |
| **TTL tampering** | Caller sets a far-future or zero expiry to keep a command claimable indefinitely / forever. | `expires_at` is **server-computed** (now + fixed server-side TTL, e.g. 120s), never client-supplied. The daemon independently rejects any command whose `expires_at` is in the past at claim time. **[A]** |
| **Opt-in state tampering** | Direct row write flips `allow_remote`. | Only the authenticated device owner via the opt-in endpoint can flip it; every flip is audited (§5.6). **[A]** |

### 4.3 Repudiation

| Threat | Mitigation |
|--------|------------|
| User denies enqueuing; admin denies arming a device; "I never ran that." | Append-only audit rows for **enqueue, claim, execution-start, execution-result/failure, opt-in toggle, device register/revoke**, each capturing actor user-id (FK `SET NULL`), actor-snapshot email/string, device-id + device-name snapshot, session-id snapshot, tool, source IP, timestamp. Snapshots survive user/session/device hard-delete (§5.6, mirrors `AgentRun` convention at `models.py:1483`). **[A]** |

### 4.4 Information Disclosure

| Threat | Mitigation |
|--------|------------|
| Enumerate other users' devices / commands by guessing IDs. | Every device/command route filters by `device.user_id == caller.id` (404, not 403, on cross-user IDs — same not-found-over-forbidden style as handoffs `GET /{id}`). **[A]** |
| Device secret leaks in logs / responses / errors. | Device secret returned **exactly once** at registration (mirror the raw-API-key one-time-return pattern, `routes/auth.py`/`api_keys.py`). Stored hashed server-side. Never echoed in list/detail/errors/logs. **[A]** + **[S]** |
| Command payload leaks session content. | The command record carries only `session_id` + `tool` (validated IDs), never session body. **[A]** |

### 4.5 Denial of Service

| Threat | Mitigation |
|--------|------------|
| Enqueue flood arms-and-fires hundreds of resumes, thrashing the victim's machine. | Per-user + per-device enqueue **rate limit** that survives multi-replica (§5.7). Plus a server-side cap on **outstanding pending commands per device** (e.g. ≤ 5); enqueue beyond cap → 429. **[A]** for the cap; **[F]** for the platform limiter. |
| Poll-storm from a rogue daemon. | Poll endpoint shares the standard auth rate limiter keyed on key-hash (`dependencies.py:164`); flag for the same multi-replica fix (§5.7). **[F]** |
| Giant/garbage params expand exec. | Fixed schema + strict validation rejects oversized/extra fields with 422 before any queue write. **[A]** |

### 4.6 Elevation of Privilege

| Threat | Mitigation |
|--------|------------|
| **Org admin → member device exec** | Authz predicate (§3.4) uses ownership ONLY. Org role grants nothing. Explicit v1 denial (§6). Negative test required. **[A]** + **[S]** |
| **Service/CI token → remote exec** | The enqueue + device + opt-in routes depend on `get_current_user` (which **rejects service keys with 403 `service_key_not_allowed`**, `dependencies.py:254`). They do NOT use `require_scope`. There is **no remote-exec scope** in v1. A stolen CI token therefore cannot reach this surface at all. **[A]** + **[S]** |
| **Stolen user key → exec on a device the attacker never armed** | Default-off opt-in (§5.4) means a stolen key on its own cannot fire onto a device unless that device was *already* armed by the legitimate user. Arming a fresh attacker-registered device is a separate, audited, rate-limited action that does not provide a session-content side channel. The residual (a key stolen *while a device is already armed*) is the irreducible risk and is the reason opt-in is per-device and revocable (§7). **[A]** + **[S]** |

---

## 5. Binding Design Decisions (implement verbatim)

### 5.1 Service keys are structurally barred
All seven new endpoints (§3.1) use `Depends(get_current_user)`, NOT `require_scope(...)`. No `remote_exec:*` scope is added to the catalog in v1. `get_current_user` already 403s service keys pre-route (`src/sessionfs/server/auth/dependencies.py:254`). This makes "compromised CI token → remote exec" impossible without a future deliberate scope addition (which would require a fresh Sentinel review).

### 5.2 CSRF / clickjacking on enqueue
- Enqueue is `POST` with an `Authorization: Bearer` header (not a cookie). SessionFS API auth is **bearer-token, not cookie-session** (`dependencies.py:156`), so classic form-CSRF does not apply: a cross-site form cannot attach the bearer header. **This must remain true** — the enqueue endpoint MUST NOT accept cookie auth.
- The dashboard MUST send the token via header (it already does for `PUT /rules` etc.), never via a GET with query-param token, and never auto-enqueue on page load. Enqueue requires an explicit user gesture in the dashboard. **[P]**
- Clickjacking: the dashboard already ships frame-protection headers for its origin; confirm `X-Frame-Options: DENY` / CSP `frame-ancestors 'none'` covers the remote-resume control. **[P]** + **[F]**

### 5.3 Device registration + identity binding
- `POST /api/v1/devices` (auth: user key). Body: `device_name` (validated `^[a-zA-Z0-9][a-zA-Z0-9._ -]{0,63}$`), `platform`, optional `hostname`.
- Server generates `device_id` (`dev_<hex>`) and a **device secret** (high-entropy, same generator family as `generate_api_key`). Secret returned **once**; stored hashed (`hash_api_key` family). Bound to `user_id` at creation.
- The daemon persists `device_id` + secret locally (file mode `0o600`, mirroring `_write_pid` at `main.py:647`) and sends BOTH the user API key (Authorization) AND the device secret (e.g. `X-Device-Secret` header) on poll/claim/result. Server verifies `hash(secret) == device.device_secret_hash AND device.user_id == authenticated_user.id`.
- **Rotation:** `POST /api/v1/devices/{id}/rotate` issues a new secret (once), invalidates the old. **Revocation:** `DELETE` sets `revoked_at`; a revoked device cannot poll/claim/be-enqueued-to (all routes check `revoked_at IS NULL`).

### 5.4 `sfs daemon allow-remote` opt-in semantics — DEFAULT OFF
- A device row is created with `allow_remote = False` (DB default `false`, server_default `'false'`).
- `sfs daemon allow-remote` (CLI) → `POST /api/v1/devices/{id}/allow-remote` flips it **True** for *that one device*. `sfs daemon allow-remote --off` (or `DELETE`) flips it back.
- Opt-in is **per-device**, never account-wide. Registering a device never arms it.
- **Revocation propagation:** the daemon already polls every cycle. The poll/claim path reads `allow_remote` live server-side: a command can be enqueued only while armed, and the daemon re-checks at claim. Turning opt-in off:
  1. Immediately blocks new enqueues (server predicate §3.4).
  2. The daemon, on its next poll (same loop as `_fetch_remote_settings`, ≤ poll interval), observes `allow_remote=False` and MUST stop claiming/executing — it discards any already-pending command for that device without executing.
- Default-off + per-device + live-revocation is the core containment for the stolen-key threat.

### 5.5 Command schema, validation, and the no-string-to-shell rule
**v1 command schema (the ONLY accepted shape):**
```
{
  "verb": "resume",                 # allowlist of exactly ["resume"]
  "session_id": "ses_<8-40 [a-z0-9]>",
  "tool": "<enum: claude-code|codex|copilot|gemini>"
}
```
- `verb`: rejected with 422 unless `== "resume"`. No other verb exists in v1 (§6).
- `session_id`: validated with `SESSION_ID_RE` (`session_id.py:12`) server-side at enqueue **and** independently in the daemon before argv build.
- `tool`: validated against the closed enum server-side and daemon-side. Capture-only tools (`cursor`, `cline`, `roo-code`, `kilo-code`, `amp`) are **rejected at enqueue** (they cannot resume — `cmd_ops.py:124-178`).
- **Daemon exec rule (BINDING):** the daemon builds argv as a Python list of validated literals only:
  ```python
  argv = ["sfs", "resume", validated_session_id, "--in", validated_tool]
  subprocess.run(argv)   # no shell=True, ever; no f-string command; no concatenation
  ```
  It MUST NOT pass a string to a shell, MUST NOT interpolate the command into `os.system`/`shell=True`, and MUST re-validate every field even though the server validated — defense in depth at the exec boundary. Any field outside the v1 schema is ignored, not forwarded.
- A nonce/idempotency note: each command row has a unique `cmd_id`. The daemon records executed `cmd_id`s (locally + via the result endpoint) and refuses to execute a `cmd_id` it has already executed — replay-safe (§4.x replay).

### 5.6 TTL + atomic single-claim (house rowcount-1 pattern)
- `expires_at` is **server-computed** at enqueue: `now + SFS_REMOTE_CMD_TTL_SECONDS` (default 120s; never client-supplied).
- Claim is the canonical atomic single-claim, mirroring the handoff claim (`routes/handoffs.py:781-810`):
  ```sql
  UPDATE remote_commands
     SET status='claimed', claimed_at=:now, claimed_by_device_id=:device_id
   WHERE id=:cmd_id
     AND device_id=:device_id
     AND status='pending'
     AND expires_at > :now
  ```
  `if result.rowcount != 1:` → 409 `claim_race_lost` (two daemons for one user, or expired). The winner is the only executor; losers never execute and never write a result row for that claim.
- Status FSM: `pending → claimed → executing → succeeded | failed | expired`. Expiry is enforced both by the `expires_at` predicate in the claim and by a lazy check (a pending command past `expires_at` is treated as `expired`, never claimable — same lazy-expiry shape as handoffs).
- Replay defense: because claim flips `pending → claimed` atomically, a replayed claim request returns rowcount 0 → 409. A replayed *result* post for an already-terminal command is rejected (status guard).

### 5.7 Enqueue rate limits — MUST survive multi-replica (Forge hand-off)
- **App-level cap (Atlas, ships in v1):** server caps outstanding `pending` commands per device (≤ 5) and rejects enqueue beyond it with 429 — a deterministic, DB-backed limit (count of pending rows), which works across replicas because it reads shared state.
- **Frequency limiter (Forge, platform):** a per-user/per-device enqueue *frequency* limit (e.g. ≤ N enqueues/min) must NOT rely on the in-memory `SlidingWindowRateLimiter` (`src/sessionfs/server/auth/rate_limit.py`), which is per-replica and trivially bypassed by hitting different Cloud Run instances. **Forge must provide an edge/platform limit (Cloud Armor / API Gateway / shared store) for the enqueue + poll endpoints.** Until that lands, the DB-backed pending-cap is the only enforced frequency control and the residual DoS risk is documented (§7).

### 5.8 Audit-trail shape (survives user/session/device deletion)
New table `remote_command_events` (append-only), one row per lifecycle event, mirroring `AgentRun` durability (`models.py:1483-1568`):
- `id` (`evt_<hex>`, PK), `command_id` (string snapshot, NOT a CASCADE FK — survives row purge), `event_type` (`enqueued|claimed|execution_started|execution_result|execution_failed|opt_in_changed|device_registered|device_revoked`).
- `device_id` + `device_name_snapshot` (plain strings — survive device delete).
- `session_id_snapshot`, `tool_snapshot`, `verb_snapshot` (plain strings).
- `actor_user_id` (FK `users.id` `ondelete="SET NULL"`) + `actor_email_snapshot` (plain string, survives user delete).
- `actor_type` (`user` — service keys are barred, so always `user` in v1; column present for future-proofing).
- `source_ip` (45 chars, IPv6-safe, via `_client_ip`, `dependencies.py:102`).
- `result_status`, `error_message` (Text, nullable), `created_at`.
The device/command tables themselves carry the live state; the events table is the forensic record that outlives hard-deletes. Enqueue, claim, and result endpoints each write an event row in the same transaction as the state change.

---

## 6. Explicit v1 DENIALS (recorded per acceptance criteria)

1. **No arbitrary verbs.** The command `verb` allowlist is exactly `["resume"]`. Any other verb is 422 at enqueue and ignored by the daemon. No `exec`, no `shell`, no `run-script`.
2. **No org-admin enqueue onto member devices.** Authorization is ownership-only (`device.user_id == caller.id AND session.user_id == caller.id`). Org role grants zero remote-exec power. An org admin enqueuing onto a member device gets 404/403. Negative test mandatory.
3. **Default opt-out.** Every device is `allow_remote=False` at registration. Remote execution is inert until the device owner explicitly arms that specific device, and arming is revocable with live propagation on the next poll.
4. **No service/CI tokens.** No `remote_exec` scope exists; all routes reject service keys via `get_current_user`.
5. **No free-form params reaching exec.** v1 schema is `{verb, session_id, tool}` only — no model, no project path, no flags. The daemon constructs argv from validated enum/regex literals and never passes a string to a shell.

---

## 7. Residual Risks (owner-tagged)

| # | Residual risk | Owner | Note |
|---|---------------|-------|------|
| R1 | **Stolen user key while a device is already armed** can enqueue a resume on that armed device. | Sentinel / product | Irreducible given the feature. Contained by: per-device opt-in (only armed devices are targets), pending-cap, full audit, live revocation, and the fact that v1 can only run `resume` (no arbitrary exec). Mitigation roadmap: step-up/confirmation on enqueue, push-style approval, short opt-in TTLs — future tickets. |
| R2 | **Multi-replica enqueue frequency limiting** is not enforced until Forge provides an edge/shared-store limiter. | Forge | DB-backed pending-cap is the only frequency control in v1. File Forge ticket. |
| R3 | **Server impersonation to daemon** (no TLS pinning). | Sentinel | Relies on HTTPS trust to `api.sessionfs.dev`. Pinning deferred. |
| R4 | **Device secret theft from disk** (local malware reading the `0o600` file). | Sentinel | Equivalent to API-key theft on a compromised host; out of scope for v1 (host compromise is game-over regardless). Rotation endpoint provided. |

---

## 8. Review Checklist (Codex + Shield-SR verify the Atlas implementation against this)

**Auth & boundary**
- [ ] All 7 new routes use `Depends(get_current_user)`; NONE use `require_scope`; no `remote_exec` scope added.
- [ ] Service-key request to each route returns 403 `service_key_not_allowed` (test present).
- [ ] Enqueue authz checks `device.user_id == caller.id` AND `session.user_id == caller.id` AND `device.allow_remote` AND `device.revoked_at IS NULL`.
- [ ] Org-admin enqueue onto a member's device is DENIED (negative test present).
- [ ] Cross-user device/command IDs return 404 (not 403, not data).

**Command integrity / no-string-to-shell**
- [ ] `verb` allowlist is exactly `["resume"]`; other verbs → 422 (test).
- [ ] `session_id` validated with `SESSION_ID_RE` at enqueue AND in the daemon (both sites tested with an injection payload, e.g. `ses_x; rm -rf` rejected).
- [ ] `tool` validated against the closed enum at enqueue AND in the daemon; capture-only tools rejected.
- [ ] Daemon builds argv as a list of validated literals; no `shell=True`, no string concat, no `os.system` (code-grep test).
- [ ] Daemon ignores any field outside the v1 schema.

**TTL / claim / replay**
- [ ] `expires_at` is server-computed; client-supplied TTL is ignored/rejected.
- [ ] Claim uses `UPDATE ... WHERE status='pending' AND expires_at > now AND device_id=...` with `rowcount != 1` → 409 (race test present).
- [ ] Expired command is never claimable (lazy-expiry test).
- [ ] Re-executing an already-executed `cmd_id` is refused by the daemon (replay test).
- [ ] Result post for a terminal command is rejected (status-guard test).

**Opt-in / device lifecycle**
- [ ] New device defaults `allow_remote=False` (DB default + server_default).
- [ ] Opt-in is per-device; arming requires a deliberate `allow-remote` call.
- [ ] Opt-out propagates: daemon stops executing pending commands for the device on next poll (test).
- [ ] Device secret returned once, stored hashed, never echoed in list/detail/error/log.
- [ ] Rotate + revoke endpoints work; revoked device cannot poll/claim/be-targeted.

**Audit**
- [ ] `remote_command_events` rows written for enqueue/claim/execution-start/result/failure/opt-in/register/revoke, in the same transaction as the state change.
- [ ] `actor_user_id` is FK `SET NULL`; `actor_email_snapshot`, `device_name_snapshot`, `session_id_snapshot`, `tool_snapshot`, `verb_snapshot` are plain strings that survive deletion (test deletes user/device, asserts events remain).
- [ ] `source_ip` captured via `_client_ip`.

**Rate limit / DoS**
- [ ] Per-device pending-command cap (≤ 5) enforced via DB count; enqueue beyond cap → 429 (test).
- [ ] No reliance on in-memory `SlidingWindowRateLimiter` for enqueue frequency; Forge ticket filed for edge limiter.

**CSRF / browser**
- [ ] Enqueue accepts bearer-header auth only; rejects cookie auth; no token in query string.
- [ ] No auto-enqueue on page load; explicit gesture required (Prism).
- [ ] Frame-protection headers cover the remote-resume control (Prism/Forge).

---

## 9. Hand-offs

- **Atlas:** implement §5 verbatim; routes use `get_current_user` (not scoped), house rowcount-1 claim, `remote_command_events` audit table + migration, daemon poll/claim/exec path with double-validation and list-argv exec.
- **Forge:** edge/platform enqueue + poll rate limiting that survives multi-replica Cloud Run (R2); confirm frame-protection headers for the dashboard control.
- **Prism:** explicit-gesture enqueue UI, bearer-header-only, no auto-enqueue, clickjacking headers (§5.2).
- **Shield-SR:** verify §6 denials and §8 checklist at pre-release; this is RCE-grade surface — bias severity upward.
