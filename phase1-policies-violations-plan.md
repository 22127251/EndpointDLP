# Phase 1 — Policies enforce (score ladder) + faithful violation reporting (detailed sub-plan)

> Sub-plan of `agent-server-integration-plan.md` **Phase 1**. Architecture/decisions
> live there; this is the executable detail for one session.
> **Status: NOT implemented — for review first.** No code changed yet.
> Companion of `phase0-deploy-connect-plan.md` (Phase 0 = DONE + VM-verified).
>
> **Execution status (2026-06-26):** CODE COMPLETE + SERVER-SIDE VERIFIED. All code
> written; verified via Docker:
> - **Agent harness: 243 passed / 3 skipped.**
> - **Migration** `56fbe7339e73` applies cleanly; **`alembic check` shows ZERO diffs**
>   for everything Phase 1 touches (policies / violation_logs / violation_policy_matches)
>   — empty-diff proof the migration matches the models. (`alembic check` also surfaced
>   *pre-existing* drift unrelated to Phase 1: three `agent_logs` indexes + an
>   `onupdate='CASCADE'` mismatch on `policy_agent_assignments.policy_id` introduced by
>   the earlier `c8d9e0f1a2b3` migration — left for a separate cleanup, not folded here.)
> - **Server tests: 11 passed** (policy ladder round-trip, control-char 422, violation
>   parent+children, allow_log, fail_open allow + reason filter, fail_closed block,
>   deleted-policy→null, missing-agent 404, Alembic up/down round-trip).
> - **Frontend image builds** (Vite `✓ built`).
> - **Live e2e smoke (21 checks)** against the running server: login → ladder policy →
>   agent → assign → heartbeat delivers ladder/scores/user_message → `translate_policies`
>   builds a real ladder → violation event (block + fail_open allow) stored as
>   parent+children → read back (agent_hostname, policy_name, count-as-field,
>   context_words_triggered) → audit filter `?reason=oversize` finds the fail_open allow.
>
> Stack torn down (`docker compose down -v`). **VM end-to-end (§F): DONE + VM-verified
> (2026-06-27)** — the rebuilt agent enforces server-authored score-ladder policies on
> the clean VM (block-with-context / allow_log-without), and the console's Violation
> Logs show the events with their matched policies + reason filter. (First VM attempt
> surfaced the §I regex double-backslash issue, since fixed and re-verified.)
> **Phase 1 COMPLETE.**

## Done / remaining checklist

- [x] **A. Server — policies** (model/schema, ladder, control-char validator) — verified
- [x] **B. Server — violations** (event parent + `violation_policy_matches` child, endpoint) — verified
- [x] **Migration** `56fbe7339e73` (autogenerate-clean; `alembic check` zero diffs for Phase-1 tables) — verified
- [x] **C. Frontend** (`Policies.vue` ladder editor, `ViolationLogs.vue` event+matches+reason filter) — image builds
- [x] **D. Agent** (`translate_policies`, dispatcher one-event emit, `cloud_bridge` worker, `events` rename) — harness 243/3
- [x] **E. Tests** — server 11 passed + agent harness 243/3 + live e2e smoke 21/21
- [x] **G. Docs** — README §C phase note updated (tagged pending VM re-verify)
- [x] **I. Regex double-backslash fix** (found during the first VM attempt) — reference
  `analyzer/policies.yaml` switched to single-quoted scalars; UI pattern hint + a
  double-`\` submit confirm in `Policies.vue`; server logs a warning. Harness 243/3,
  12 server tests, frontend builds.
- [x] **F. VM end-to-end pass — DONE + VM-verified (2026-06-27)**
- [ ] *(optional, out of scope)* separate cleanup migration for the pre-existing
  `agent_logs` indexes + `policy_agent_assignments` `onupdate` drift

## Goal

A policy authored in the Management Console UI must behave on the VM agent **exactly
as it does in standalone mode**:
- **`block` → BLOCK** (enforced; `user_message` shown to the user).
- **`allow_log` → logged** in the agent's `events.jsonl` **and** surfaced in the
  console's Violation Logs.
- **Failure-mode outcomes are recorded with their `reason`**, both directions:
  `fail_closed` → BLOCK and **`fail_open` → ALLOW** (e.g. an oversize/timeout/
  unsupported file that the channel lets through). Both must reach the console and be
  **queryable by `reason`** for audit.

Guiding principle: **the console's violation log is a faithful projection of
`events.jsonl`.** Every *notable* decision the agent records — anything carrying a
policy match (`violations`) **or** a failure `reason` — becomes one server record with
the same shape the agent emits (one event, its final `decision`, the honest list of
every policy that triggered; no fabricated "deciding policy"). A truly clean ALLOW (no
match, no `reason`) is **not** recorded.

Fixes integration-plan conflicts **#1** (no score ladder → never blocks), **#3**
(violation reporting 422 + wrong shape), **#2** (pattern corruption), a latent
**#keyword** bug, and closes the **no-server-tests** gap.

---

## Decisions locked (planning Q&A)

| # | Decision |
|---|---|
| L1 | **Full ladder editor** in Policies.vue: `score_base`, `score_context_boost`, editable `{min_score → action}` rungs, `user_message`. |
| L2 | **Drop the single `action` column** on the server Policy. The `actions` ladder is the sole action mechanism. Keep the `PolicyAction` enum (rung dropdown + violation child rows). |
| L3 | **Violations are event-centric, normalized, and faithful to the agent.** One POST per event; the server stores it as a parent `violation_logs` row (the decision) + one `violation_policy_matches` child row per triggered policy. **No deciding/placeholder policy_id.** |
| L4 | **A "violation" = any notable `events.jsonl` decision** = it has `violations` **or** a `reason`. Covers: policy **block**; **allow_log** (parent `decision=ALLOW` + children); **`fail_closed`** block (parent + `reason` + 0 children); **`fail_open`** allow (parent `decision=ALLOW` + `reason` + 0 children). All queryable by `reason`. |
| L5 | **Migration via `alembic revision --autogenerate`** (real generated id, auto `down_revision`), then hand-edit to insert the `action→actions` backfill before the column drop + verify the JSONB `server_default`. Existing 3 migrations untouched. |
| L6 | **Add a server test suite** (pytest + pytest-asyncio + httpx `AsyncClient`/`ASGITransport`) against a dedicated `dlpserver_test` DB on the compose Postgres, covering the Phase-1 surface + an Alembic up/down round-trip. |
| L7 | **Notable-only** violation logging: record an event iff it has `violations` **or** a `reason`. A clean ALLOW (no match, no reason) is not sent. The child field is named **`context_words_triggered`** (server + agent + `events.jsonl`). |

---

## Findings this plan is built on (verified against current code)

1. **Conflict #1 (core blocker).** `analyzer/policy.py:62` builds the ladder from
   `actions:`; absent → `[(0.0,"allow")]`. Server `Policy` has only `action`, no
   `score_*`/`actions`. `translate_policies` (`cloud_bridge.py:34`) emits `action`,
   no ladder → every server policy resolves to ALLOW.
2. **Conflict #3 (violations) + the agent's true unit.**
   `orchestrator/events.py::record_decision` runs **once per request** and emits
   `decision` (BLOCK/ALLOW) + `reason` (category) + a flat `violations` list, each
   entry `{policy_id, action, count, with_context, context_words}`. **No single
   deciding policy** — the decision is the strongest `action`. `count` = shape
   matches; `with_context` = how many matches had a context-word boost; the context
   words are the **distinct words that triggered the boost** (→ to be renamed
   `context_words_triggered`, since it is *not* the policy's full context list).
   Crucially, a **`fail_open` ALLOW carries a `reason`** (the failure token) with
   `decision=ALLOW` — it is its own `events.jsonl` line. The current dispatcher
   callback (`dispatcher.py:185`) emits `agent_id:""`, no `policy_id`, a list under
   `details`, and only fires `if violations` → drops `fail_open` allows and
   `fail_closed` blocks; server `ViolationLogCreate` rejects the shape anyway.
3. **Conflict #2 — TWO distinct issues (the reframe was incomplete).**
   (a) *Control char at store time:* a backspace `0x08` stored in a pattern serialises
   to JSON `"\b…"` and the agent restores the backspace → guard at create/update +
   defensive strip in `translate_policies` (done). The JSON→YAML pipeline itself is
   byte-clean for a correct single-backslash regex.
   (b) *Regex-escaping representation mismatch (found on the first VM attempt — see §I):*
   the stored pattern had **double** backslashes (`\\b\\d{12}\\b`) because it was copied
   from the reference file's **double-quoted** YAML form (where `\\` is the escaping for
   a single `\`). `yaml.dump` emits it as a **plain** scalar, and plain YAML keeps
   backslashes **literal**, so re2 got a double-backslash regex that never matches →
   silent ALLOW. No YAML-quoting change in the agent fixes this (PyYAML dump/load is
   identity); the fix is at the **input/representation** boundary. → **§I**.
4. **Latent #keyword bug.** Engine handles only `regex`/`denylist`
   (`engine.py:157,172`); server `type="keyword"` does nothing → map
   `keyword → denylist` in `translate_policies`.

**Stack checks:** `env.py` already sets `target_metadata = Base.metadata` → autogenerate
works. Pydantic v2 `Field(example=)` deprecated → `examples=`/`json_schema_extra`.
`httpx==0.28.1` already a server dep → tests add only `pytest`+`pytest-asyncio`.

---

## A. Server — policies

### A1. Model `server/app/models/policy.py`
- **Remove** `action`. **Add** `user_message` (Text, server_default `''`),
  `score_base` (Float, NOT NULL, server_default `'0.5'`), `score_context_boost`
  (Float, NOT NULL, server_default `'0.5'`), `actions` (JSONB, NOT NULL, default
  `list`, server_default canonical
  `[{"min_score":1.0,"action":"block"},{"min_score":0.0,"action":"allow_log"}]`).

### A2. Schemas `server/app/schemas/policy.py`
- Keep the enums. Add
  `PolicyActionRung{ min_score: float = Field(ge=0.0); action: PolicyAction }`.
- `PolicyCreate`/`PolicyUpdate`: drop `action`; add `user_message`, `score_base`,
  `score_context_boost`, `actions: list[PolicyActionRung]` (Create defaults canonical);
  add a `field_validator("patterns","keywords","context_words", mode="after")`
  rejecting C0 control chars (`\x00–\x1f`).
- `PolicyResponse`/`PolicyDetailResponse`: drop `action`; add the four fields.

### A3. Policies API/metadata — no structural change (confirm the new fields round-trip;
`policy_actions` still feeds the rung dropdown).

---

## B. Server — violations (event + matches, faithful to `events.jsonl`)

### B1. Models
- **`violation_log.py` (parent = the event):** `id`, `agent_id` FK→agents (CASCADE),
  `channel`, `decision` (String — `"BLOCK"`/`"ALLOW"`), `reason` (String, **nullable**
  — `policy_violation`/`oversize`/`text_cap`/`unsupported_format`/`timeout`/
  `analysis_error`/`malformed`; null on allow_log/clean), `details` (JSONB —
  `{req_id, name, url, elapsed_ms}`), `created_at`. **Remove** the old single
  `policy_id` + `action` columns. `matches = relationship(… cascade="all, delete-orphan")`.
- **`violation_policy_match.py` (child = one triggered policy):** `id` (uuid7),
  `violation_log_id` FK→violation_logs (CASCADE), `policy_id` FK→policies
  (**SET NULL**, nullable), `action` (String), `count` (Integer),
  `with_context` (Integer), **`context_words_triggered`** (JSONB list). Add its import
  to `alembic/env.py`.

### B2. Schemas `server/app/schemas/violation_log.py`
- `ViolationPolicyMatchCreate { policy_id: UUID | None, action: str, count: int = 0,
  with_context: int = 0, context_words_triggered: list[str] = [] }`.
- `ViolationLogCreate { agent_id: UUID, channel: str, decision: str,
  reason: str | None = None, details: dict = {},
  matches: list[ViolationPolicyMatchCreate] = [] }`.
- `ViolationPolicyMatchResponse` (+ resolved `policy_name`) and `ViolationLogResponse
  { …, matches: [...] }`.

### B3. Endpoint `server/app/api/v1/violation_logs.py`
- `POST /violation-logs/`: validate the agent; create the **parent** + **N children**
  in one transaction. Per match `db.get(Policy, policy_id)`; if missing → child with
  `policy_id=NULL` (keep action/count/context). Proper status codes (no 201-with-error).
- `GET /violation-logs/`: `selectinload(matches)` + each match's `policy`; pagination;
  searchable by `decision` / **`reason`** / hostname (so audit-by-reason works).

---

## C. Frontend

### C1. `views/Policies.vue` (ladder editor)
- Remove the single **Action** select. Add **Block Reason (`user_message`)** text;
  **score_base**/**score_context_boost** `el-input-number` (step 0.1, 0–2) with help;
  **Actions ladder** = repeatable `{min_score → action}` rows (default canonical,
  high→low, add/remove). `form` drops `action`, adds the four fields; `handleSubmit`
  sorts rungs desc, strips empties. List **ACTION** column → ladder summary (top-rung
  tag + tooltip).

### C2. `views/ViolationLogs.vue` (event + matches)
- Rework to **event rows** (time, agent/host, channel, `decision`, `reason`, file/url)
  with an **expandable** matched-policies detail (policy name, action, count,
  with_context, **context_words_triggered**). Add a **`reason` filter** for audit.
  *(Read the current file in-session before editing.)*

---

## D. Agent

### D1. `cloud_bridge.py :: translate_policies`
- Emit `user_message`, `score_base`, `score_context_boost`, `actions` (pass through).
  Stop emitting the dead single `action`. Map `keyword → denylist`. Defensive
  control-char strip on `patterns`/`keywords`/`context_words`.

### D2. Violation reporting — one event, faithful, no fan-out
- **`dispatcher.py`**: build the structured per-policy list **once** and feed **both**
  `record_decision` (`events.jsonl`) and the cloud callback — guarantees the server
  mirrors the audit log. **Rename the field `context_words` → `context_words_triggered`**
  in that list (so it appears renamed in `events.jsonl` *and* the server; update the
  `record_decision` docstring + any harness/README reference to the old key).
- Callback payload becomes the **event** shape:
  `{channel, decision, reason, details:{req_id,name,url,elapsed_ms},
  matches:[{policy_id, action, count, with_context, context_words_triggered}, …]}`
  (drop `agent_id:""`/`action:"block"`). **Fire when `violations` OR `reason`** — so
  `fail_open` allows and `fail_closed` blocks (empty matches, `reason` set) are
  reported alongside policy blocks and allow_log hits (per L4). A clean ALLOW (no
  match, no `reason`) does not fire — notable events only.
- **`cloud_bridge.py` `_violation_worker`**: POST **one** `/api/v1/violation-logs/`
  per event; set `agent_id = self._agent_id`; pass `channel`/`decision`/`reason`/
  `details`/`matches` through. **No per-policy loop.** `report_violation` unchanged.

---

## E. Tests

### E1. Server suite (new) — `server/tests/` + `server/requirements-dev.txt`
- Deps (dev-only): `pytest`, `pytest-asyncio` (`httpx` already present).
- `conftest.py`: `AsyncClient(ASGITransport(app))` + a DB-session override against a
  `dlpserver_test` DB on the compose Postgres (`localhost:5432`); schema per session,
  rollback per test.
- Tests (Phase-1 surface):
  - policy create with ladder + round-trip (`actions` JSONB preserved);
  - **control-char pattern → 422**;
  - heartbeat returns policies carrying the ladder;
  - violation POST (policy block) → **1 parent + N children**, `decision=BLOCK`,
    `reason=policy_violation`;
  - **allow_log** event → parent `decision=ALLOW`, child `action=allow_log`;
  - **`fail_open` allow** (empty `matches`, `decision=ALLOW`, `reason=oversize`) →
    1 parent + 0 children, and it is **returned when filtering `GET ?reason=oversize`**;
  - **`fail_closed` block** (empty `matches`, `decision=BLOCK`, `reason=timeout`) →
    1 parent + 0 children;
  - a match with a deleted `policy_id` → child stored with `policy_id NULL`.
  - **Migration round-trip**: `alembic upgrade head` then `downgrade -1` reverses.

### E2. Agent harness (`scripts/harness`)
- `translate_policies`: emits ladder + scores + `user_message`; `keyword→denylist`;
  strips control chars; round-trips to a Policy whose `resolve_action` blocks at 1.0 /
  allow_logs at 0.0.
- Callback → `_violation_worker`:
  - event matching **A** (block, count 1000) **+ B** (allow_log, count 5) ⇒ **one POST**,
    `agent_id` filled, `decision=BLOCK`, `matches=[A,B]` with each policy's action/
    count/with_context/**context_words_triggered** (proving one-POST-per-event; count is
    a field, never a row multiplier);
  - **`fail_open` allow** (timeout) ⇒ one POST, `decision=ALLOW`, `reason=timeout`,
    `matches=[]`;
  - **`fail_closed` block** ⇒ one POST, `decision=BLOCK`, `reason=timeout`, `matches=[]`.
- Assert the `events.jsonl` line uses the renamed `context_words_triggered` key.
- Whole suite stays green; C# `dotnet build`/`dotnet test` unaffected (sanity).

---

## F. VM end-to-end pass — YOUR STEPS (server-side already verified)

> **Precondition — rebuild + reinstall the agent on the VM.** The enforcement fix is
> **agent-side** (`translate_policies` now emits the ladder; the OLD installed agent
> would still resolve every server policy to ALLOW). So the VM must run the **new**
> agent: rebuild the bundle (README **§A.4** `dotnet build` + `msbuild`, then **§A.6**
> `package-bundle.ps1`) and reinstall on the VM (README **§B.2**). If the VM agent
> isn't currently pointed at the server, also do README **§C.4** (`config.yaml`
> `server:` → `enabled: true` + `agent_id` + `url`, then `Restart-Service DLPAgent`).

1. **Server up:** from `src\management_console`, `docker compose up -d --build`
   (the `--build` picks up the **updated frontend** with the pattern hint + double-`\`
   confirm; entrypoint runs `alembic upgrade head`). Confirm `/docs` shows the new
   Policy (ladder, no `action`) + violation shapes; log into the UI (`admin`/`admin123`).
2. **Author a policy** (Policies → Create): Visa pattern
   `\b4\d{3} ?\d{4} ?\d{4} ?\d{4}\b`, context words `credit card`,`visa`, range 120,
   Block Reason `Credit card number (Visa) detected`, base 0.5 / boost 0.5, ladder
   `≥1.0 block / ≥0.0 allow_log`. **Assign** it to the VM agent (or its group).
   > ⚠️ **Enter patterns with SINGLE backslashes** (`\b\d{12}\b`, not `\\b\\d{12}\\b`).
   > If you copy from `analyzer/policies.yaml`, copy the text **between the
   > single-quotes** (now single-backslash). The UI warns if you paste a
   > double-backslash form — heed it. (This is the §I fix; the first attempt failed
   > because a double-backslash CCCD pattern was pasted in.)
3. **Delivery (VM):** after a heartbeat, `C:\Program Files\DLP\analyzer\policies.yaml`
   shows the ladder + `score_base`/`score_context_boost` + `user_message` + clean
   patterns (no control-char corruption).
4. **Enforcement (VM):** a file/clipboard/upload with a card **+** a context word →
   **BLOCK** (popup/clipboard/Note shows the Block Reason); the same card **without**
   context → allow_logged (not blocked).
5. **Violation Logs UI:** the block shows as **one event** (decision **BLOCK**,
   channel, `reason=policy_violation`) that **expands** to the matched policy with its
   `count` / `with_context` / `context_words_triggered`; the **allow_log** hit appears
   as a `decision=ALLOW` event; force a **`fail_closed` block** (set
   `analyzer.max_extracted_chars: 100`, `dlp-ctl reload`, copy long text) → appears
   with `reason=text_cap`; optionally a **`fail_open` allow** (set that channel's
   `failure_mode: fail_open`) → `decision=ALLOW` + `reason`, found via the **reason
   filter**. Restore config + `dlp-ctl reload` when done.
6. **Teardown:** `docker compose down -v` (server) / `server.enabled: false` +
   `Restart-Service DLPAgent` (back to standalone) — both in README §C.6.

*(Server-side already proven in-session: migration + `alembic check`, 11 server tests,
frontend build, agent harness 243/3, and a 21-check live e2e smoke of the exact
heartbeat → translate → violation flow. Steps 4–5 are the on-device behaviors only a
real VM can show.)*

---

## G. Docs
- **README §C**: policies now **enforce** (ladder) and the console mirrors
  `events.jsonl` — block / allow_log / `fail_open`-allow / `fail_closed`-block all
  recorded and **queryable by `reason`**. Add the checks above to §C.5.
- Note the `events.jsonl` field rename `context_words` → `context_words_triggered`
  wherever the audit format is described (§A.8/§A.9 as needed).
- Add a server-test how-to (`dlpserver_test`, `pytest server/tests`).

---

## H. Risks / watch-items
- **One migration, several changes** (policy column swap + violation reshape + new
  child table). Review the autogenerate diff; insert the `action→actions` backfill
  before the policy `action` drop; verify JSONB `server_default`.
- **`events.jsonl` key rename** is an audit-format change — update the harness
  assertion + any README field listing that names `context_words`.
- **`actions` JSONB shape** must round-trip UI→API→DB→heartbeat→YAML→`load_policies`
  (E2 pins it).
- **ViolationLogs.vue** is a genuine event+matches rework, not a rename.
- **Volume**: allow_log + `fail_open` allows add event rows (clean ALLOWs are not
  recorded); the server cleanup loop bounds retention.
- Per-agent **auth is Phase 2**; agent endpoints stay open here. **Standalone mode**
  untouched — every change is cloud-path only.

---

## I. Regex double-backslash fix (found on the first VM attempt) — VERIFIED

**Symptom:** a UI-authored CCCD policy delivered but did **not** block; the generated
`policies.yaml` had `- \\b\\d{12}\\b` (plain, double backslash). Manually re-quoting it
`- "\\b\\d{12}\\b"` made it block.

**Root cause:** the pattern was copied from the reference `analyzer/policies.yaml`,
where it is written in **double-quoted** YAML (`"\\b\\d{12}\\b"`) — and there `\\` is the
*escape* for one `\`, so the real regex value is the single-backslash `\b\d{12}\b`.
Copying the visible text into the plain UI field stores the **doubles literally**. The
agent's `yaml.dump` then writes a **plain** scalar, and plain YAML keeps backslashes
literal, so re2 receives `\\b\\d{12}\\b` (match a literal backslash) → never matches →
ALLOW. (Reproduced: plain double → no match; single → match; manual re-quote collapses
`\\`→`\` → match; forcing the agent to quote does **not** help — PyYAML re-escapes
faithfully, so dump/load is identity.)

**Fix (3 parts, all verified):**
1. **Reference `analyzer/policies.yaml` → single-quoted scalars** (`'\b\d{12}\b'`): the
   visible text now *is* the regex (single backslash, no escaping), so copy-into-UI is
   correct. Behaviour unchanged for standalone (same parsed values; harness 243/3,
   re2 still matches).
2. **`Policies.vue`:** a hint under the pattern field ("enter the raw regex with single
   backslashes … do NOT double-escape") **+** a non-blocking submit confirm if any
   pattern contains `\\`.
3. **Server `policies.py`:** `create`/`update` log a **warning** (never block, never
   auto-rewrite) when a pattern contains `\\` — a genuinely-intended escaped pattern is
   preserved. New server test `test_double_backslash_pattern_warns_not_blocks` (12
   server tests pass).
