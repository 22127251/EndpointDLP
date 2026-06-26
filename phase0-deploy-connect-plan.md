# Phase 0 — Deploy the server + connect the existing agent (implementation + verification plan)

> Sub-plan for **Phase 0** of `agent-server-integration-plan.md`. Scope is taken
> verbatim from that file: *"the user can deploy the server with Docker, log into
> the UI, create an agent, paste its `agent_id` into the VM agent's `config.yaml`,
> and see it go **ACTIVE** and receive policies … **No code changes (or trivial
> only).**"*
>
> **Do not implement yet** — this file is for review. Nothing here was committed;
> the only live action taken while writing it was a throwaway Docker bring-up on the
> dev box that has already been torn down (containers + network removed, DB volume
> preserved — see "Pre-test log").

---

## EXECUTION STATUS (2026-06-26)

| Task | Status |
|---|---|
| **T7a** harden `ensure_registered` + new `scripts/harness/test_cloud_bridge.py` | ✅ **DONE + verified** — `orchestrator/cloud_bridge.py` else-branch added; 5 new unit tests pass. Also fixed a **pre-existing** harness failure: the merged `server_*` config fields were unclassified in `test_config_apply_hot_reload.py`'s meta-test — classified them restart-only (+`server:` in `_BASE`). **Full harness now `232 passed, 3 skipped`** (was 222 + my 5 cloud_bridge + 5 server_* cases; the 3 skips are the elevated admin-pipe tests). |
| **T6** README "§C — Connect to server" | ✅ **DONE** — added §C.1–C.6 + `server.*` added to the Appendix restart-only list. |
| **T1** deploy server (Docker) | ✅ **DONE + running now** — `docker compose up -d --build`; all 3 containers Up, `api=200`, `ui=200`. Left running for your T3/T5. |
| **T2** firewall rule (TCP 8000) | ✅ **DONE + MANUALLY TESTED** (2026-06-26) — rule added (elevated); VM→`192.168.6.1:8000` `Test-NetConnection` succeeded. |
| **T3** create agent + copy UUID (UI) | ✅ **DONE + MANUALLY TESTED** (2026-06-26) — agent created in the UI, UUID pasted into the VM config. *(Optional policy assign NOT done — Phase 1.)* |
| **T4** build + install bundle on VM | ✅ **DONE + MANUALLY TESTED** (2026-06-26) — bundle built (VS 2026 Developer PowerShell) + installed on the clean VM. |
| **T5** connect on VM (edit config + `Restart-Service`) | ✅ **DONE + MANUALLY TESTED** (2026-06-26) — `server:` edited + `Restart-Service DLPAgent`; agent went **ACTIVE**, `last_seen` advancing, log tails visible in the console. |
| **T7b** `.env` hygiene | ⏸️ **DEFERRED** (decision). |

**Phase 0 COMPLETE + VM-verified (2026-06-26).** Every step passed end-to-end. The single un-exercised item is the **optional** policy create/assign + its delivery to the VM's `policies.yaml` — intentionally skipped because server-pushed policies don't enforce on the current agent (that whole path is **Phase 1**).

Code/doc changes this session: `orchestrator/cloud_bridge.py` (M), `scripts/harness/test_cloud_bridge.py` (new), `scripts/harness/test_config_apply_hot_reload.py` (M — pre-existing-gap fix), `README.md` (M, §C). **Not committed** (commit only on your say-so).

---

## 0. Terms used in this plan (read once)

- **Dev box** — your Windows 11 machine with the full toolchain (VS 2026, Docker
  Desktop, the repo). The **server** runs here, in Docker.
- **VM** — the clean Windows 11 Home VMware guest with **no** dev tools. The
  **agent** runs here. Networked to the dev box via **VMware NAT**.
- **Server / Management Console** — the FastAPI + Vue + Postgres stack under
  `src/management_console/` (`docker-compose.yml` → `db` / `backend:8000` /
  `frontend:80`).
- **Agent** — the installed DLP endpoint agent (the `DLPAgent` Windows service +
  interceptors) built and deployed per `README.md`.
- **CloudBridge** — `orchestrator/cloud_bridge.py`, the agent's HTTP client to the
  server. It reads the `server:` block of `config.yaml`, **prepends the base URL
  and appends `/api/v1/...`** to every call, heartbeats, and writes policies
  locally. Key consequence used throughout: **`server.url` must be the bare origin
  `http://<host>:8000` with no `/api/v1` suffix** (CloudBridge adds it).
- **Heartbeat** — `PATCH /api/v1/agents/{id}/heartbeat`. It is an **open endpoint**
  (no auth in Phase 0); it marks the agent **ACTIVE**, advances `last_seen`, and
  returns the agent's assigned **policies**. This is the call that "connects" the
  agent.
- **Admin enrollment** — the intended workflow: an **admin** creates the agent row
  in the UI (carrying the admin JWT), copies the generated **agent UUID**, and
  pastes it into the VM's `config.yaml`. The agent never self-registers in the
  normal flow.
- **Restart-only vs hot-reloadable** — most `config.yaml` fields hot-reload on save
  / `dlp-ctl reload`. The **`server:` block is restart-only**: it is read once at
  process start (`orchestrator/__main__.py:258`) and is **not** in
  `_HOT_RELOADABLE_FIELDS`. **Cause → effect:** editing `server.*` does nothing
  until you **`Restart-Service DLPAgent`**.

---

## 1. What I verified live before writing this (pre-test log, dev box, 2026-06-26)

I brought the whole stack up with `docker compose up -d --build` (your approved
form), exercised the exact contract Phase 0 depends on, then tore it down. Results:

| # | Check (command form I actually ran) | Result |
|---|---|---|
| 1 | `docker compose up -d --build` in `src/management_console` | **Both images build**; `node:26-alpine` pulled + `vite v8.0.10` built 1670 modules clean; `python:3.13-slim` backend; network `management_console_default` created; 3 containers **Up**, ports `8000/5432/80` published on `0.0.0.0`. exit 0. |
| 2 | backend startup log | `alembic upgrade head` ran (clean linear chain `a548ed3f0678 → b7c3d4e5f6a1 → c8d9e0f1a2b3`); lifespan seeded admin + settings; `Uvicorn running on 0.0.0.0:8000`. |
| 3 | `GET http://localhost:8000/openapi.json` | `EndpointDLP 1.0.0` (backend reachable, `/docs` served). |
| 4 | `GET http://localhost/` | HTTP **200** (frontend/nginx serving the SPA). |
| 5 | `POST /api/v1/auth/login {admin/admin123}` | **200**, JWT returned (the seeded admin from `server/.env`). |
| 6 | `POST /api/v1/agents/register` (admin JWT, `status:inactive`) | **201**, returns agent `id` (UUIDv7), `status=inactive`. |
| 7 | `PATCH /api/v1/agents/{id}/heartbeat` (**no auth** — what the agent sends) | **200**; `status` flips **inactive → active**; `last_seen` **advances**; body carries `policies`. |
| 8 | `POST /api/v1/agents/{id}/logs` (**no auth** — what the agent sends) | **201**. |
| 9 | `GET /api/v1/agents/{id}` (admin JWT) | `status=active`, `last_seen` advanced. |
| 10 | create policy → `POST /policies/{id}/assign-agents [<agent>]` → heartbeat | heartbeat now returns **`num_policies=1`** (policy delivery works). |
| 11 | ran the agent's real `orchestrator.cloud_bridge.translate_policies()` on the server's heartbeat output | produced a `policies.yaml` body — **confirms the Phase-1 gap** (see §6): it emits `action: block` but **no `actions` ladder / score fields**, so the agent's `load_policies` falls back to allow → *delivered but not enforced yet*. |
| 12 | DB persistence | backend logged **"Admin user already exists"** → the named volume `management_console_postgres_data` **persisted** prior data across runs (mount-at-PGDATA works; no compose change needed — see §6 risk note). |
| 13 | `docker compose down` (no `-v`) | containers + network removed, **volume preserved**, zero leftover `dlp_*` containers. |
| 14 | `New-NetFirewallRule … -LocalPort 8000 … -WhatIf` | command **valid** (WhatIf accepted, nothing created); no `DLP Server 8000` rule exists yet. |
| 15 | `Get-NetIPAddress` | **VMware NAT adapter `VMnet8` = `192.168.6.1`** → this is the host IP a NAT VM uses; agent `server.url` = `http://192.168.6.1:8000`. |

**Not pre-testable in the authoring session — subsequently MANUALLY TESTED by the
user on the VM (2026-06-26), all passed:**
- Building the bundle (`README` A.3 python-embed, A.4 C#/C++) — needs a **VS 2026
  Developer PowerShell**. ✅ Built + installed on the clean VM.
- Installing on the VM and the **live VM→`192.168.6.1:8000` round-trip**. ✅ Probe
  succeeded; agent connected and went **ACTIVE**.
- Clicking through the **browser UI** — ✅ login + create-agent done in the UI; the
  agent row showed ACTIVE with advancing `last_seen` and pushed log tails.
- **Sole exception (not tested):** the optional policy create/assign + delivery to the
  VM's `policies.yaml` — skipped on purpose (server-pushed policies don't enforce on
  the current agent → Phase 1).

---

## 2. Implementation tasks

Phase 0 is "no code changes (or trivial only)", so the tasks are deploy / connect /
document, plus a README addition and **one trivial code change** (T7a, the
`ensure_registered` hardening — confirmed in scope). `.env` hardening is **deferred**
(T7b) per the decision to keep the working PoC `.env`.

### T1 — Deploy the server stack on the dev box  ✅ pre-tested (§1 rows 1–4)
- **Goal:** server reachable at `http://localhost:8000` (API) and `http://localhost/`
  (UI) on the dev box.
- **Steps:**
  1. `server/.env` **already exists** and is functional (admin `admin123`,
     `CORS_ORIGINS=["*"]`, `ALGORITHM=HS256`, `SECRET_KEY=your_secret_key`). Compose
     overrides `DATABASE_URL` to `…@db:5432/dlpserver`, so the localhost value in
     `.env` is irrelevant inside the container. **No edit required to deploy.**
     (Optional hardening → T7.)
  2. From `src\management_console`: **`docker compose up -d --build`**.
  3. `entrypoint.sh` waits for Postgres, runs `alembic upgrade head`, then uvicorn;
     `main.py` lifespan seeds the admin + the `ServerConfiguration` row.
- **Files (no change):** `docker-compose.yml`, `server/.env`, `server/Dockerfile`,
  `frontend/Dockerfile`, `frontend/nginx.conf`, `server/scripts/entrypoint.sh`,
  `server/main.py`.

### T2 — Open VM→dev-box connectivity (NAT + firewall)  ⚠️ firewall cmd validated; live probe NOT tested
- **Goal:** the VM can reach `http://192.168.6.1:8000`.
- **Why `192.168.6.1`:** with VMware **NAT**, the dev box's `VMnet8` adapter
  (`192.168.6.1`) is the host address reachable from the NAT subnet. Docker
  publishes `8000` on `0.0.0.0`, so it answers on that interface **once the Windows
  firewall allows inbound TCP 8000**.
- **Steps:**
  1. Add the inbound rule (elevated PowerShell, dev box):
     `New-NetFirewallRule -DisplayName 'DLP Server 8000' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Profile Any`
  2. From the VM, probe: `Test-NetConnection 192.168.6.1 -Port 8000` → expect
     `TcpTestSucceeded : True`.
- **Note:** the admin can drive the UI from the **dev box's own browser**
  (`http://localhost/`), so an inbound rule for **port 80 is optional** (only needed
  to open the UI *from the VM*).

### T3 — Admin enrollment: create the agent (and a test policy)  ✅ API path pre-tested (§1 rows 5–6,10); UI click-through not browser-tested
- **Goal:** an agent row exists; you hold its **UUID**.
- **Steps (UI, dev box):**
  1. Browse `http://localhost/`, log in `admin` / `admin123`.
  2. *(Optional but recommended so §6's "delivered" is visible)* **Policies → create**
     a Visa-with-context policy, then assign it to the agent (or its group).
  3. **Agents → create**. Set a hostname; **choose status `inactive`** (so you can
     watch it flip to ACTIVE on first heartbeat — a cleaner signal). Save.
  4. Copy the agent's **UUID** shown under the hostname (`Agents.vue` renders
     `row.id` under the name).
- **Gotcha (timing):** a created agent's `last_seen` defaults to its creation time
  and the offline-checker flips ACTIVE→OFFLINE after `HEARTBEAT_INTERVAL_SECONDS`
  (seeded **300 s**). Creating it **`inactive`** sidesteps this; either way the
  first heartbeat sets it ACTIVE.

### T4 — Build + install the agent on the clean VM (prerequisite — "nothing yet")  ❌ NOT re-tested here (needs VS Dev PowerShell + VM); covered by README ✅ markers
- **Goal:** `DLPAgent` installed and running on the VM in **standalone** mode first
  (proves the agent itself is healthy before adding the server).
- **Steps (follow `README.md` exactly — do not re-derive):**
  - Dev box: A.2 venv → **A.3** `prepare-python-embed.ps1` → **A.4**
    `prepare-install-payload.ps1` (VS 2026 Developer PowerShell) → **A.6**
    `package-bundle.ps1` → produces `dist\DLP\` + `dist\DLP.zip`.
  - VM: **B.1** install .NET 10 Desktop Runtime → **B.2** right-click
    `install.cmd` → Run as administrator → `Get-Service DLPAgent` = **Running**.
- **Why separate:** isolates "agent works" from "server connects". If a decision
  test (README A.8/B.3) passes in standalone, any later failure is networking/config,
  not the agent.
- **Sequencing:** land **T7a first** so the hardened `cloud_bridge.py` is compiled
  into the embed bundle here — otherwise the VM would run the old `ensure_registered`.

### T5 — Connect the installed agent to the server  ⚠️ config parsing pre-tested; live restart NOT tested (needs VM)
- **Goal:** the installed agent heartbeats the server and goes ACTIVE.
- **Steps (VM, elevated):**
  1. Edit **`C:\Program Files\DLP\config.yaml`** → `server:` block:
     ```yaml
     server:
       url: "http://192.168.6.1:8000"   # bare origin; CloudBridge adds /api/v1
       agent_id: "<UUID copied in T3>"
       heartbeat_interval: 30
       log_sync_interval: 300
       enabled: true                    # flips standalone → cloud mode
     ```
  2. **`Restart-Service DLPAgent`** (the `server:` block is **restart-only** — a save
     alone won't take effect).
- **Files (no change):** the **installed** `config.yaml` (the bundle copies the
  `server:` block through verbatim — confirmed in `installer.build_bundle_config`).
- **Ordering rule (important):** complete **T1 + T2** (server up, firewall open,
  probe green) **before** the restart. If the server is unreachable at agent start
  *and* an `agent_id` is set, `ensure_registered()` falls through to the admin-only
  `/register`, gets **403**, and the bridge goes **standalone until the next
  restart** (it won't self-heal mid-run). See T7.

### T6 — Document the server-connect path in README  📝 doc-only (trivial change)
- **Goal:** the README (the teammate's zero-context guide) covers connecting to the
  server, not just standalone.
- **Change:** add a short **"§C — Connect the agent to the Management Console"**
  section: the `docker compose up -d --build` deploy, the NAT IP + firewall rule, the
  admin "create agent + copy UUID" step, the `config.yaml server:` edit +
  `Restart-Service DLPAgent`, and an explicit note that **`server.enabled: false`
  keeps the agent fully standalone** (every existing README flow is unchanged). This
  is the only intended *repo* change in Phase 0 and touches **no code**.

### T7a — Harden `ensure_registered` (transient-failure path)  ✅ committed (trivial code change)
- **Goal:** remove the "server-down-at-startup → silent standalone until restart"
  footgun. When an `agent_id` **is configured**, a *transient* heartbeat failure
  (network down / 5xx / auth) must **keep** the configured id and let the heartbeat
  loop retry — **not** fall through to the admin-only `/register` (which 403s and
  drops the bridge to standalone until the next service restart).
- **Change (one localized `else:` branch in `orchestrator/cloud_bridge.py`,
  `ensure_registered`):**
  ```python
          if self._agent_id:
              status, _ = self._patch(f"/api/v1/agents/{self._agent_id}/heartbeat")
              if status == 200:
                  log.info("Cloud bridge: agent_id %s verified", self._agent_id)
                  return self._agent_id
              if status == 404:
                  log.warning("Agent %s not found on server, re-registering", self._agent_id)
                  self._agent_id = ""
              else:                                   # ← NEW
                  # Transient (network/5xx/auth): trust the admin-configured id and
                  # let the heartbeat loop retry; do NOT fall back to /register.
                  log.warning(
                      "Cloud bridge: heartbeat for configured agent_id %s returned %s; "
                      "keeping it and retrying via the heartbeat loop",
                      self._agent_id, status,
                  )
                  return self._agent_id
  ```
  The existing `/register` path below is now reached **only** when `agent_id` is
  empty (or was just cleared by a real 404) — i.e. the genuine auto-register case,
  unchanged. The `404 → re-register` behaviour is untouched.
- **Scope guard:** behaviour with `agent_id` empty (true standalone / auto-register)
  is unchanged; `server.enabled: false` path is untouched.
- **Verification:**
  - **New unit test** `scripts/harness/test_cloud_bridge.py` (the harness has **zero**
    `cloud_bridge` coverage today — confirmed — so this is purely additive): build a
    `CloudBridge` with a configured `agent_id`, stub `_patch` to return `(0, "")`
    (network down), assert `ensure_registered()` **returns the configured id** and
    `_post` was **never** called with `/register`; second case: `_patch` returns
    `(404, "")` → id cleared and the register path is attempted.
  - **Smoke:** `python -c "import orchestrator.cloud_bridge"` (no import/build error),
    then `python -m pytest scripts\harness -q` → still **`222 passed, 3 skipped`**
    plus the new test. *(Run at implement time — flagged NOT-YET-RUN here because we
    are not implementing in this session.)*

### T7b — `.env` hygiene  ⏸️ DEFERRED (decision: leave the working PoC `.env`)
- Not done in Phase 0. Future note only: before any non-PoC use, set a real
  `SECRET_KEY`, change the admin password, and narrow `CORS_ORIGINS`. Harmless to
  leave for the LAN PoC (the UI is same-origin through nginx, so `CORS=["*"]` never
  triggers; JWT is a bearer header, not a cookie).

---

## 3. Order of execution

```
T7a (harden ensure_registered + pytest) ─► T4 (build+install agent on VM, standalone) ─┐
T1 (deploy server) ─┐                                                                   │
T2 (firewall+probe) ─┼─► T3 (create agent, copy UUID) ─► T5 (edit config + restart) ─► VERIFY (§5)
                     └──────────────────────────────────────────────────────────────►┘
T6 (README) lands alongside T7a. T7b is deferred.
```
**Key sequencing:** T7a must precede T4 so the embed bundle ships the hardened agent.

---

## 4. Files touched in Phase 0

- **Deploy/connect needs no code change.** All server + agent source is used
  **as-is** (verified building + running, §1).
- **Code change (T7a):** `orchestrator/cloud_bridge.py` (`ensure_registered`, one
  `else:` branch) + **new** `scripts/harness/test_cloud_bridge.py`.
- **Repo doc change (T6):** `README.md` (new "§C — Connect to server" section).
- **Edited on the VM (not repo):** the **installed**
  `C:\Program Files\DLP\config.yaml` `server:` block (T5).

---

## 5. Verification runbook (replicable, step-by-step)

Two parts, **both now MANUALLY TESTED (2026-06-26)**. **Part A (dev-box server)** —
the commands below are the ones run on the dev box (Git Bash form; `docker`/`curl.exe`
behave identically from PowerShell). **Part B (VM agent)** — run on the clean VM; all
steps passed (sole exception: the optional policy-delivery bullet, deferred to
Phase 1).

### Part A — server is up and the agent contract works  ✅ MANUALLY TESTED (dev box, 2026-06-26)

> Run from the repo root unless noted. `docker compose` commands are shell-agnostic.

**A-0. Agent code-change check (T7a).** ✅ DONE (2026-06-26)
```
python -c "import orchestrator.cloud_bridge"          # import OK
python -m pytest scripts\harness -q                   # 232 passed, 3 skipped
```

**A-1. Bring it up.**
```
cd src\management_console
docker compose up -d --build
docker compose ps
```
*Expect:* `dlp_db_container`, `dlp_backend_container`, `dlp_frontend_container` all
`Up`, with `0.0.0.0:8000->8000`, `0.0.0.0:5432->5432`, `0.0.0.0:80->80`.

**A-2. Backend migrated + seeded.**
```
docker logs dlp_backend_container 2>&1 | grep -iE "alembic|admin|Uvicorn running"
```
*Expect:* an alembic line, an admin line (`created` on first run / `already exists`
later), and `Uvicorn running on http://0.0.0.0:8000`.

**A-3. API + UI reachable.**
```
curl.exe -s -o NUL -w "api=%{http_code}\n" http://localhost:8000/openapi.json
curl.exe -s -o NUL -w "ui=%{http_code}\n"  http://localhost/
```
*Expect:* `api=200`, `ui=200`. (Or browse `http://localhost:8000/docs` and
`http://localhost/` and log in `admin`/`admin123`.)

**A-4. Simulate the agent's exact calls** (this is what proves "it will connect").
Git Bash:
```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# create an agent (admin JWT), inactive so the flip is visible
AID=$(curl -s -X POST http://localhost:8000/api/v1/agents/register \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"hostname":"verify-phase0","status":"inactive"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "agent=$AID"

# the heartbeat the agent sends (NO auth) — must flip ACTIVE + advance last_seen
curl -s -X PATCH http://localhost:8000/api/v1/agents/$AID/heartbeat \
  | python -c "import sys,json;d=json.load(sys.stdin);print('status=',d['status'],'last_seen=',d['last_seen'],'num_policies=',len(d['policies']))"

# the log push the agent sends (NO auth)
curl -s -o /dev/null -w "logs=%{http_code}\n" -X POST http://localhost:8000/api/v1/agents/$AID/logs \
  -H "Content-Type: application/json" -d '{"events_tail":"x","agent_log_tail":"y"}'
```
*Expect (observed today):* `status= active`, a fresh `last_seen`, `num_policies=` the
count you assigned (0 if none), `logs=201`.

**A-5. Clean up the verify rows** (keeps the DB tidy; optional):
```bash
curl -s -X DELETE http://localhost:8000/api/v1/agents/$AID -H "Authorization: Bearer $TOKEN" -o /dev/null -w "del=%{http_code}\n"
```

### Part B — the VM agent connects  ✅ MANUALLY TESTED (clean VM, 2026-06-26)

**B-1. Standalone sanity (T4).** On the VM after install: `Get-Service DLPAgent` →
**Running**; run one README **B.3** decision test (e.g. clipboard card+context →
blocked). *Expect:* standalone DLP works before adding the server.

**B-2. Connectivity (T2).** On the VM: `Test-NetConnection 192.168.6.1 -Port 8000`.
*Expect:* `TcpTestSucceeded : True`. *(If False: confirm the dev-box firewall rule
from T2, that `docker compose ps` shows `8000` published, and that the VM is on the
NAT subnet `192.168.6.x`.)*

**B-3. Connect (T5).** Edit `C:\Program Files\DLP\config.yaml` `server:` block as in
T5; `Restart-Service DLPAgent`.

**B-4. Agent-side evidence.** On the VM:
```
Get-Content C:\ProgramData\DLP\logs\dlp-agent.log -Tail 60
```
*Expect:* a `Cloud bridge: started (agent_id=…, heartbeat=30s, …)` line and recurring
heartbeat activity (no repeated `Heartbeat failed`/`running standalone`).

**B-5. Server-side evidence (dev box UI).**
- **Agents** page: the row shows **ACTIVE** with `last_seen` advancing every ~30 s
  (refresh after a minute).
- **Agent Logs** page (or `GET /api/v1/agents/{id}/logs` with admin JWT): tail lines
  from the VM appear (`events` / `agent_log`).
- If you assigned a policy in T3: `C:\Program Files\DLP\analyzer\policies.yaml` on the
  VM gets the auto-generated header + the delivered policy after a heartbeat.
  *(NOT exercised on 2026-06-26 — optional, deferred to Phase 1.)*

**B-6. Pass criteria (Phase 0 done):** ✅ met (2026-06-26) — agent **ACTIVE** with
advancing `last_seen`; `dlp-agent.log` shows heartbeats; pushed logs visible
server-side. **Known & expected:** a pushed policy is delivered but does *not* block
yet — that is Phase 1 (§6).

---

## 6. Risks, gotchas & known-deferred items

- **Pushed policies don't enforce yet (by design).** Empirically confirmed (§1
  row 11): `translate_policies` emits `action` but no `actions` ladder / `score_*` /
  `user_message`, so the agent's score-based `load_policies` falls back to allow.
  **→ Phase 1.** Don't treat "card not blocked from a server policy" as a Phase 0
  failure.
- **Regex escaping is corrupted on the wire (new finding — for Phase 1).** A regex
  like `\b4…\b` comes back from the heartbeat as JSON `"\b4…"` (single backslash),
  which `json.loads` turns into a **backspace `0x08`** *before YAML is involved*. So
  the demo's "quoting bug" is really a **server-side serialization** issue (how
  `patterns` are stored/encoded), not only the agent's YAML quoting. Record this in
  the Phase 1 sub-plan; **out of scope for Phase 0.**
- **`server:` is restart-only.** Editing it without `Restart-Service DLPAgent` looks
  like "nothing happened." (§0, T5.)
- **Server-down-at-startup footgun.** With an `agent_id` set but the server
  unreachable when the service starts, `ensure_registered` → admin-only `/register`
  → 403 → standalone until next restart. Mitigated by the T5 ordering rule; optional
  permanent fix in T7a.
- **Postgres 18 volume is non-idiomatic but works.** The compose mounts the named
  volume **at** PGDATA (`/var/lib/postgresql/18/docker`); the documented PG18
  default moved the VOLUME to `/var/lib/postgresql`. Mounting at PGDATA **does
  persist** (verified, §1 row 12). The known "silent data-loss" PG18 footgun only
  bites when people mount the *old* `/var/lib/postgresql/data` path — which this
  compose does not. **No change needed for Phase 0**; a future hardening could switch
  the target to `/var/lib/postgresql`.
- **CORS is a non-issue here.** The UI is same-origin through nginx
  (`/api/v1` → `backend:8000`) and auth is a bearer header, so `CORS_ORIGINS=["*"]`
  never triggers. (Tidy it in T7b before any non-PoC use.)
- **Offline flip during the create→connect gap.** Harmless; the first heartbeat
  restores ACTIVE (§T3 gotcha).

---

## 7. Rollback / teardown (fully reversible)

- **Server (standard = clean, per decision 4):** from `src\management_console`,
  **`docker compose down -v`** removes containers + network **and wipes the DB
  volume**, so every `docker compose up -d --build` starts from a fresh DB
  (admin + settings re-seeded by the lifespan; create the agent + policy again).
  Use plain `docker compose down` (no `-v`) only when you deliberately want to keep
  the DB between runs. Optional image cleanup:
  `docker image rm management_console-backend management_console-frontend`.
- **Firewall:** `Remove-NetFirewallRule -DisplayName 'DLP Server 8000'`.
- **Agent:** set `server.enabled: false` + `Restart-Service DLPAgent` to return to
  pure standalone, or uninstall per README A.10 / B.4.
- **Dev box is untouched** otherwise: no WDAC/state changes; the server lived only in
  Docker.

---

## 8. Decisions — RESOLVED (2026-06-26)

1. **T6 README section** — ✅ **YES, add it now** (new "§C — Connect to server").
2. **T7a `ensure_registered` hardening** — ✅ **YES, include in Phase 0** (trivial
   code change; precise diff + new unit test in T7a).
3. **T7b `.env` hygiene** — ⏸️ **LEAVE the working PoC `.env`** (deferred).
4. **DB teardown policy** — ✅ **`docker compose down -v` (clean each run)** is the
   runbook standard (§7).

Phase 0 scope is now frozen: T1–T6 + T7a (T7b deferred). **Still not implemented** —
awaiting your go-ahead to start coding T7a / writing the T6 README section.
