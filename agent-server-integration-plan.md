# Agent ↔ Management Console Integration — Phased Plan

> **Where this file lives:** Plan mode only allowed me to write here
> (`~/.claude/plans/...`). Per the brief, copy this file to the **repo root** as the
> first execution step (e.g. `agent-server-integration-plan.md`) so it travels with
> the code.
>
> **How to use:** Each phase below is executed in its **own session** and gets a
> **detailed sub-plan there** (this file stays at architecture/decision level). Do
> **not** execute yet. Phases are ordered so each one is independently verifiable.

---

## Context (why this work)

The repo contains both an endpoint DLP agent and a brand-new **management/control
server** (`src/management_console/`). The server was built in parallel with agent
changes, so the agent↔server contract **drifted and is broken in a few places**.
Goal: make the server the central control plane for the fleet — push policies,
push all hot-reloadable agent config, and manage the App Control (WDAC) channel —
plus produce a **pre-tested, step-by-step deployment + verification path** (server
in Docker on the dev box; agent on the clean Win11 Home VM) the user can follow
without prior server knowledge.

### Enrollment already works (corrected understanding)

The intended (and working) enrollment is **admin-driven**, not agent self-register:
1. Admin clicks **Create New Agent** in the UI → `POST /agents/register` carries the
   admin's JWT (axios interceptor), so the `is_admin_user` guard passes *by design*.
2. Admin copies the new agent's `id` (shown under the hostname in `Agents.vue`) into
   `config.yaml` `server.agent_id`.
3. Agent calls `PATCH /agents/{id}/heartbeat` — an **open endpoint** — and receives
   policies. Works today. (The agent's *automatic* `/register` fallback for an empty
   `agent_id` would 403, but that path isn't the workflow and isn't a blocker.)

In the colleague's demo the only failure was the **policy-YAML quoting bug** (the
server-delivered policy left `patterns`/`context_words`/`keywords` strings
unquoted) — a translation bug fixed in **Phase 1**, not an enrollment problem.

### Stack (verified, current, not deprecated)

| Side | Tech |
|---|---|
| Server backend | FastAPI 0.135.3, Starlette 1.0.0, Uvicorn 0.44.0, **Python 3.13** |
| Server DB/ORM | PostgreSQL **18-alpine**, SQLAlchemy 2.0.35, **Alembic 1.18.4**, asyncpg 0.31.0; ids are **UUIDv7** (`uuid6` lib) |
| Server auth | python-jose (JWT, admin UI), bcrypt; **add `secrets`-based per-agent token (stdlib)** |
| Server upload | **python-multipart 0.0.26 already present** (no new dep for file upload) |
| Frontend | Vue 3.5.32, Element Plus 2.13.7, Vite 8, Pinia, Axios |
| Deploy | `src/management_console/docker-compose.yml` (db / backend:8000 / frontend:80, nginx proxies `/api/v1`→backend) |
| Agent client | `orchestrator/cloud_bridge.py` — stdlib `urllib.request` only (no new dep) |

New deps introduced: **`pefile`** on the server only (Phase 4; pure-Python,
dependency-free, cross-platform — verified it extracts `InternalName` from PE
`StringFileInfo` on Linux). Everything else reuses existing libraries.

---

## Conflicts found during exploration (the "many conflicts")

1. **Server-pushed policies never block (Issue #1 + score mechanism).** The agent
   analyzer (`analyzer/policy.py`) is **score-based**: each policy has `score_base`,
   `score_context_boost`, `user_message`, and an `actions` ladder
   `[(min_score, action), …]` — the **ladder is the only action mechanism**. The
   server model (`server/app/models/policy.py`) has only a single `action` field —
   **no score fields**. `translate_policies` (`cloud_bridge.py:34`) emits `action`
   but **no `actions` ladder**, so `load_policies` (`policy.py:62`) falls back to
   `[(0.0,"allow")]` → **every server policy resolves to ALLOW and never blocks.**
   → Phase 1.
2. **Policy-YAML quoting (the demo's failure).** Server-delivered policy strings
   (`patterns`/`context_words`/`keywords`) must survive YAML round-trip (regex
   backslashes, leading `{`, etc.). Verify/repair in the translation path. → Phase 1.
3. **Violation reporting fails (422).** The agent posts violations with
   `agent_id: ""` and **no top-level `policy_id`** (`dispatcher.py:185-201`, relayed
   verbatim by `cloud_bridge.py:_violation_worker`). The server `ViolationLogCreate`
   **requires** `agent_id: UUID` + `policy_id: UUID` and does `db.get(Policy, …)`. So
   every report is rejected; also the agent can emit several violations per event vs.
   the server's one-policy-per-log shape. → Phase 1.
4. **Agent endpoints are unauthenticated.** `heartbeat`/`config`/`logs`/`violations`
   have no auth; the credential in use is the **UUIDv7 `agent_id`**, which is an
   *identifier* (in URLs/logs, shown in the UI, exported) and only 74 random bits +
   an embedded timestamp — unsuitable as a secret. → Phase 2 (per-agent token).
5. **Agent hot-reloadable config is not exposed (Issue #2).** `AgentResponse`
   returns only `policies`; `ServerConfiguration.settings` holds 4 **server-side**
   knobs, not the agent's 6 hot-reloadable fields. CloudBridge never writes
   `config.yaml`. → Phase 3.
6. **App Control channel absent on the server (Issue #3).** No model/endpoint/UI; the
   agent has a complete local WDAC pipeline (`orchestrator/app_control/*`,
   `dlp-ctl appcontrol …`). → Phase 4.

---

## Settled design decisions (from this planning session)

| # | Decision |
|---|---|
| D1 | **Order: fix the broken core contract first.** P0 deploy+connect → P1 policies(score)+violations → P2 per-agent token auth → P3 config exposure → P4 App Control. |
| D2 | **Policies become faithfully score-based** on the server: expose `user_message`, `score_base`, `score_context_boost`, and an `actions` ladder editor (sensible defaults). `translate_policies` emits the ladder + score fields + correctly-quoted strings so policies actually enforce. |
| D3 | **Per-agent token auth** (keep the manual-enroll UX): when the admin **creates** an agent, the server also returns a **one-time high-entropy token** (`secrets.token_urlsafe`), stored **hashed**. Admin pastes `agent_id` **+ `agent_token`** into `config.yaml`; the agent sends `Authorization: Bearer <token>`; the server guards agent endpoints. Bootstrap-trust (no mTLS) — adequate for the LAN PoC; mTLS/JWT-with-expiry noted as future hardening. |
| D4 | **Config scope = global default + per-group override + per-agent override**, merged `{**global, **group, **agent}` into an *effective* config delivered to the agent. |
| D5 | **App Control: server stores rule *specs*, endpoint builds+compiles+applies.** Admin uploads `.exe/.dll` (server extracts `InternalName` via `pefile`) and/or types `FilePath`/`InternalName` entries, tagged allow/deny. **No hash rules** (slow compile). Binaries are **not** shipped to endpoints. The endpoint assembles XML (`policy_xml.add_file_attrib_rule`/`add_filepath_rule`), auto-merges self-protect, bumps its own `VersionEx`, compiles `ConvertFrom-CIPolicy`, applies via the existing inbox/deployer. Reason: ConfigCI is **Windows-only** (a Linux Docker server cannot compile a `.cip`); version/self-protect are per-endpoint state. |
| D6 | **App Control assignment reuses the policy assignment model** (assign rule sets to groups/agents), for UI/back-end consistency. |

---

## Agent ↔ server contract (target shape)

- **Base URL:** agent `server.url` = `http://<dev-box-ip>:8000`; CloudBridge appends
  `/api/v1/...`. Web UI = `http://<dev-box-ip>:80` (nginx proxies `/api/v1`→backend).
- **Enrollment (unchanged UX):** admin **Create New Agent** in UI → server returns
  `{id, agent_token}` (token shown once); admin pastes both into `config.yaml`.
- **Heartbeat:** `PATCH /agents/{id}/heartbeat` (Bearer agent_token after Phase 2) →
  returns `policies` (score-based) **+ effective `config`** (Phase 3) **+ `app_control`
  specs** (Phase 4). Agent translates/writes locally; existing watchers hot-reload.
- **Telemetry up:** `POST /agents/{id}/logs` (tails), `POST /violation-logs/` (one per
  matched policy), App-Control status (Phase 4) — all Bearer-authed after Phase 2.

---

## Phase 0 — Deploy the server + connect the existing agent (baseline)

> ✅ **DONE + VM-verified (2026-06-26).** Sub-plan: `phase0-deploy-connect-plan.md`.
> Server deployed in Docker; agent built + installed on the clean VM and connected
> over VMware NAT (`server.url=http://192.168.6.1:8000`) → went **ACTIVE**, `last_seen`
> advancing, log tails visible in the console. Harness green (`232 passed, 3 skipped`).
> One **trivial code change** landed: `cloud_bridge.ensure_registered` no longer falls
> back to the admin-only `/register` on a transient heartbeat failure when an
> `agent_id` is configured (+ new `scripts/harness/test_cloud_bridge.py`; the merged
> `server_*` fields were also classified restart-only in the config meta-test). README
> gained **§C — Connect to server**. *Not exercised (deferred to Phase 1): server-pushed
> policy assignment/enforcement — confirmed it delivers but does not block on the
> current agent.*

**Goal:** the user can deploy the server with Docker, log into the UI, create an
agent, paste its `agent_id` into the VM agent's `config.yaml`, and see it go
**ACTIVE** and receive policies. Teaches the setup; establishes a working baseline.
**No code changes** (or trivial only).

**Steps to pre-test in the phase session:**
- `server/.env` from `.env.example`: `SECRET_KEY`, `ALGORITHM=HS256`,
  `INITIAL_ADMIN_*`, `DATABASE_URL` (`...@db:5432/dlpserver`), `CORS_ORIGINS`.
- `docker compose up --build` in `src/management_console/`; `entrypoint.sh` runs
  `alembic upgrade head` then uvicorn; `main.py` lifespan seeds admin + settings.
- Verify `http://localhost:8000/docs`, log into UI at `http://localhost/`, create a
  policy + an agent.
- **Networking (VM→dev box):** VM reaches `http://<dev-box-LAN-ip>:8000` — VMware
  NAT/bridged + a Windows **inbound firewall rule for TCP 8000**.
- VM agent `config.yaml`: `server.{url,enabled:true,agent_id:<pasted>}`; restart
  `DLPAgent`.

**Key files:** `docker-compose.yml`, `server/.env(.example)`, `server/scripts/
entrypoint.sh`, `server/main.py`, `server/app/config.py`; VM `config.yaml`.

**Verification:** ✅ **passed (2026-06-26)** — agent row **ACTIVE** with advancing
`last_seen`; `dlp-agent.log` shows heartbeats; logs appear via `/agents/{id}/logs`.
*(Known: pushed policies are delivered but do not enforce yet — that's Phase 1; the
optional policy-assignment check was skipped for that reason.)*

---

## Phase 1 — Policies become score-based + violations report (core DLP works)

**Goal:** a policy authored in the UI actually **blocks** on the VM and the block
shows up in the server's Violation Logs. Fixes conflicts #1, #2, #3 (decision D2).

**Server scope:**
- `policies` table + `schemas/policy.py`: add `user_message` (text), `score_base`
  (float, default 0.5), `score_context_boost` (float, default 0.5), `actions`
  (JSONB list of `{min_score, action}`, default `[{1.0,block},{0.0,allow_log}]`).
  Decide in-session whether to drop the redundant single `action` or keep it nullable
  for display. Alembic migration (mirror migration `c8d9e0f1a2b3`).
- Violations: keep `ViolationLogCreate {agent_id, policy_id, channel, action,
  details}`; the agent now sends **one POST per matched policy** with a real
  `policy_id` (server UUID) + its own `agent_id`.

**Frontend (`views/Policies.vue`):** replace the single **Action** dropdown with a
**Block Reason / `user_message`** field, `score_base` + `score_context_boost`
numbers, and an **actions ladder editor** (rows of `min_score` + action) with
defaults + inline help ("format-only vs format+context → action").

**Agent scope:**
- `cloud_bridge.py:translate_policies`: emit `user_message`, `score_base`,
  `score_context_boost`, `actions` (list of `{min_score, action}`) so `load_policies`
  builds a real ladder; stop relying on the dead single `action`. **Confirm
  YAML-quoting** of `patterns`/`context_words`/`keywords` round-trips (the demo bug).
- Violation path: fill `agent_id`, **expand `details.violations` → one
  `ViolationLogCreate` per policy** with top-level `policy_id`. (Centralized
  violations require server-authored policies, since local slug ids won't match
  server rows — document it.)

**Key files:** `server/app/models/policy.py`, `server/app/schemas/policy.py`,
Alembic migration, `frontend/src/views/Policies.vue`; `orchestrator/cloud_bridge.py`,
`orchestrator/dispatcher.py` (only if reshaping the emit).

**Verification (VM, README §B):** create a Visa-with-context policy (base 0.5, boost
0.5, ladder block@1.0 / allow_log@0.0), assign to the agent/group; after a heartbeat
`policies.yaml` shows the ladder + quoted patterns; on the VM a card number **with**
context is **BLOCKED** (`user_message` shown), **without** context is allow_logged;
the block appears in the server **Violation Logs** UI with correct agent/policy/
channel. `pytest scripts/harness` green; C# builds (README §A).

---

## Phase 2 — Per-agent token auth (hardening)

**Goal:** close the open-endpoint gap; the agent authenticates with a high-entropy
token instead of relying on a visible UUIDv7. Delivers decision D3.

**Server scope:**
- `agents` table: add `agent_token_hash` (store a **hash**, like passwords). Alembic
  migration.
- On **create** (`POST /agents/register`, still admin-JWT-guarded): generate a
  `secrets.token_urlsafe(32)` token, persist its hash, **return the plaintext once**
  in the response; surface it in the UI create dialog.
- New dependency `get_current_agent` (validates `Authorization: Bearer <token>`
  against the agent's hash) on `heartbeat`, `config`, `agent_logs` POST,
  `violation-logs` POST. Optionally retire/align the agent's dormant auto-`/register`
  path.

**Agent scope:**
- `config.py`: add `server_agent_token`; parse from `server:` section; persist it
  alongside `agent_id` (`cloud_bridge.py:_persist_agent_id`).
- `cloud_bridge.py`: attach `Authorization: Bearer <token>` to every `_request`.
- `config.yaml`: `server.agent_token`.

**Frontend (`views/Agents.vue`):** show the one-time token in the create-agent
result with a copy button + "store now, won't be shown again" note.

**Key files:** `server/app/models/agent.py`, `server/app/schemas/agent.py`,
`server/app/api/v1/agents.py`, `server/app/api/deps.py`, Alembic migration,
`frontend/src/views/Agents.vue`; `orchestrator/cloud_bridge.py`,
`orchestrator/config.py`, `config.yaml`.

**Verification:** create an agent → token shown once; paste `agent_id` + `agent_token`
into the VM `config.yaml`; agent heartbeats successfully; an agent endpoint called
**without** the token returns 401; existing agents must be re-tokened (document the
migration step). Harness green.

---

## Phase 3 — Expose all hot-reloadable agent config (global/group/agent)

**Goal:** the admin sets the 6 hot-reloadable fields remotely; the agent applies them
live without restart. Delivers Issue #2 with decision D4.

The 6 fields (`orchestrator/config.py:_HOT_RELOADABLE_FIELDS`) and their `config.yaml`
homes: `failure_mode` → per-channel (`clipboard`/`browser`/`peripheral_storage.
transfer_agent`).`failure_mode`; `max_file_bytes` → `limits`; `max_extracted_chars`
+ `supported_extensions` → `analyzer`; `analysis_timeout_seconds` +
`drain_timeout_seconds` → `service`.

**Server scope:** storage for 3 levels — global (extend `ServerConfiguration.settings`
with an `agent_config` block), per-group (`agent_groups.config` JSONB), per-agent
(`agents.config_override` JSONB); a **merge service** → effective config; deliver it
in the heartbeat (`config` field on `AgentResponse`) and/or `GET /agents/{id}/config`;
a validation schema for the 6 fields. Alembic migration.

**Frontend:** config editors per level — global in `Settings.vue`, per-group in
`AgentGroups.vue`, per-agent in `Agents.vue` (show effective vs. override).

**Agent scope (`cloud_bridge.py`):** on heartbeat, **merge only the hot-reloadable
source keys** of the effective config into the existing `config.yaml` (atomic write,
preserving all restart-only sections), mapped to the nested keys above. The existing
`ConfigWatcher` → `_handle_config_change` → `config.apply_hot_reload` applies it live.

**Key files:** `server/app/models/{setting,agent,agent_group}.py`, `server/app/
schemas/*`, new merge service, Alembic migration, `frontend/src/views/{Settings,
AgentGroups,Agents}.vue`; `orchestrator/cloud_bridge.py` (+ unchanged
`config.py:apply_hot_reload`).

**Verification:** flip `peripheral_storage.failure_mode` or change
`max_extracted_chars` in the UI; after a heartbeat, `dlp-ctl status` / observed
behavior on the VM reflects it **without restart**; restart-only sections untouched.
`test_config_apply_hot_reload` stays green.

---

## Phase 4 — Expose the App Control (WDAC) channel

**Goal:** admin uploads exes / types path rules, tags allow/deny, assigns to
agents/groups; the endpoint builds + compiles + applies the WDAC policy and reports
status/blocks back. Delivers Issue #3 with decisions D5/D6.

**Server scope:**
- Add **`pefile`** to `requirements.txt`.
- Model: app-control **rule entries** `{id, level: internalname|filepath, value,
  mode: allow|deny, source: upload|manual, original_filename?}`, grouped into a
  ruleset and assignable to groups/agents (mirror policy assignment). Alembic
  migration.
- **Upload endpoint** (`UploadFile` + `python-multipart`): accept `.exe/.dll`, extract
  `InternalName` via `pefile` (warn/return error if none → suggest a FilePath rule),
  create an `internalname` entry. Manual-entry endpoints for `filepath`/
  `internalname` text.
- Deliver allow/deny **specs** in the heartbeat (`app_control` field) or a dedicated
  endpoint; ingest agent-reported **status** (deployed GUID/version, block counts).
- UI `views/AppControl.vue`: upload, list/tag entries, assign, show per-agent status.

**Agent scope:**
- `orchestrator/app_control/builder.py`: new `build_from_specs(allow_specs,
  deny_specs, config)` calling `policy_xml.add_file_attrib_rule("InternalName", …)` /
  `add_filepath_rule(…)` directly (no `collect_files`, **no hashing**), merges
  self-protect, bumps `VersionEx` above `deployer.deployed_version_ex()`, compiles
  `ConvertFrom-CIPolicy`, writes staging, then `apply()` → existing inbox/deployer.
- `cloud_bridge.py`: receive specs → on change, write locally + trigger
  `build_from_specs` + `apply` (in-process; orchestrator is LocalSystem); report
  `deployer.read_status()` back.

**Key files:** `server/app/models/` (+ migration), `server/app/api/v1/` (new router +
upload), `server/requirements.txt`, `frontend/src/views/AppControl.vue` + router/nav;
`orchestrator/app_control/builder.py`, `orchestrator/app_control/policy_xml.py`
(reuse), `orchestrator/cloud_bridge.py`.

**Verification (VM, README §B):** upload an app's exe as **deny**, assign to the agent
→ agent builds+compiles+applies (no reboot); the denied app is **blocked** while
allowed apps run; `dlp-ctl appcontrol status` + the UI show deployed version + block
counts; removing the assignment / `disable` returns to baseline. Self-protect rules
(`install_root\*`, `C:\Program Files\dotnet\*`) are always present so the agent + .NET
interceptors never get blocked.

---

## Cross-cutting notes, risks, and verification discipline

- **Pre-tested commands:** every command handed to the user (Docker, `alembic`,
  `dlp-ctl`, agent build/deploy) is **dry-run/verified inside its phase session
  before hand-off** (plan-mode could not execute). Ground agent-side verification in
  **README §A (dev box) / §B (clean VM)**, not throwaway scripts; update README for
  any new config/behavior.
- **No build breakage:** server changes follow the existing Alembic + Pydantic
  patterns; agent changes keep `pytest scripts/harness` green and the 3 C# apps
  building (`dotnet build`).
- **Dev box safety:** all VM testing uses the clean Win11 Home VM; nothing here
  touches the dev machine's WDAC state. The server runs only in Docker (reversible
  `docker compose down -v`).
- **Security caveats:** the per-agent token is bootstrap-trust (no mTLS) — fine for a
  LAN PoC; note mTLS/JWT-with-expiry as future hardening. App Control: only
  `InternalName`/`FilePath` rules (no hash) — document the installer-exe-vs-installed-
  app `InternalName` mismatch caveat (App-Control plan decision 8b) in the UI/help.
- **Standalone mode stays intact:** `server.enabled: false` keeps the agent fully
  local (policies.yaml, `dlp-ctl appcontrol`) — every phase preserves it.
- **Each phase ends ACTIVE-verified** before the next; later phases assume earlier
  ones landed (P2 retrofits auth onto P0/P1 calls; P4 reuses P0 transport + P3
  patterns).
