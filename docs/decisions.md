# Design Decisions Log

Append-only record of decisions made during Kavi v2 development.
When a decision is superseded, update its status and reference the new one — never delete.

**Status values:** `CURRENT` | `SUPERSEDED by D###` | `REDUNDANT (reason)`

---

## D001: Claude Code as external build backend (2025-02-09)

**Status:** `CURRENT`

**Context:** How does the `build-skill` step generate code?

**Decision:** Shell out to Claude Code (the CLI) as a gated build worker. It generates patches; Kavi verifies and the user approves. No interactive coding in the terminal.

**Rationale:** Claude Code (or Codex) is treated as an external compiler, not a brain. Local Sparkstation models will handle analysis/routing/cheap work later, but code generation goes through a gated external backend.

**Implication:** The `build-skill` command must produce a BUILD_PACKET.md, invoke `claude` CLI, and capture the result as a patch — never execute it directly.

---

## D002: Ledger is the single source of truth (2025-02-09)

**Status:** `CURRENT`

**Context:** Three storage formats exist: SQLite ledger, registry.yaml, and markdown artifacts.

**Decision:** The ledger (SQLite) is canonical. registry.yaml is a derived human-readable view of TRUSTED skills. Markdown artifacts are durable outputs referenced by hash in the ledger.

**Precedence:** ledger > registry.yaml > files

**Rationale:** The ledger is the audit log. YAML and files are the working surface. If they conflict, the ledger wins.

---

## D003: Static enforcement only in MVP (2025-02-09)

**Status:** `CURRENT`

**Context:** How are side-effect classes enforced at runtime?

**Decision:** MVP enforcement is static + structural only:
- Policy scanner (forbidden imports, exec/eval detection)
- Constrained execution environment (path allowlists for FILE_WRITE)
- Explicit side-effect class declarations

No OS-level sandboxing (chroot, seccomp) in v1.

**Rationale:** The goal is governed capability growth, not hostile-code isolation. Sandboxing comes later.

---

## D004: Skill progression — simple first (2025-02-09)

**Status:** `CURRENT`

**Context:** What skills to build to validate the forge pipeline?

**Decision:**
1. First skill: `write_note` (FILE_WRITE, no secrets) — hello-world validation
2. Second skill: `read_notes_by_tag` or `summarize_notes` (READ_ONLY) — stress-tests without secrets
3. Network and secrets skills deferred until forge flow is rock solid

**Rationale:** Each new skill should stress one more invariant. Don't introduce network/secrets until the basic pipeline is proven.

---

## D005: Clean break from kavi-prototype (2025-02-09)

**Status:** `CURRENT`

**Context:** Relationship to the existing kavi-prototype repo.

**Decision:** 100% fresh start. Nothing carried forward except lessons. No agent framework, no memory system, no orchestrator concepts from the prototype.

**Rationale:** The prototype is an archaeological artifact, not a dependency. v2 is a governed spine, not an agent system.

---

## D006: Convention-based skill path derivation (2025-02-09)

**Status:** `CURRENT`

**Context:** `verify-skill` and `promote-skill` required explicit `--skill-file` and `--module-path` arguments, duplicating conventions already established in the build packet.

**Decision:** Derive all paths from the proposal name using `forge/paths.py`:
- Skill file: `src/kavi/skills/{name}.py`
- Test file: `tests/test_skill_{name}.py`
- Module path: `kavi.skills.{name}.{CamelCase}Skill`

Remove `--skill-file` from `verify-skill` CLI and `--skill-file`/`--module-path` from `promote-skill` CLI. Both now take only a proposal ID and derive paths internally.

**Rationale:** Single source of naming truth eliminates drift between build packet expectations and downstream verification/promotion. Reduces CLI surface area and makes the pipeline less error-prone.

**Implication:** All skills must follow the naming convention. Custom paths are no longer supported.

---

## D007: Invariant checker as separate governance gate (2025-02-09)

**Status:** `CURRENT`

**Context:** The policy scanner checks safety (forbidden imports, eval/exec) but not contract conformance — it can't verify that a skill class extends BaseSkill with the correct attributes, that the side_effect_class matches the proposal, or that only expected files were modified.

**Decision:** Add an invariant checker (`forge/invariants.py`) with three sub-checks:
1. **Structural conformance** (AST): class extends BaseSkill, required attrs present, side_effect_class matches proposal
2. **Scope containment** (git diff): only skill + test files modified, protected paths unchanged
3. **Extended safety** (AST): no `__import__()`, no `importlib.import_module()`

Integrated as check #5 in `verify_skill()`. Also available standalone via `kavi check-invariants`.

**Rationale:** Structural governance complements the policy scanner. Together they ensure both "is the code safe?" and "does the code conform to the skill contract?" Keeping them separate preserves single-responsibility.

**Implication:** `all_ok` in verification now requires 5 checks to pass. Schema migrated to v2 with `invariant_ok` column.

---

## D008: Auto-detect build result via conventional paths (2025-02-09)

**Status:** `SUPERSEDED by D009`

**Context:** `mark-build-done` is a manual step that requires the user to remember to run it after Claude Code finishes. With convention-based paths (D006), we can detect build success automatically.

**Decision:** Add `detect_build_result(proposal_name, project_root)` that checks if both `src/kavi/skills/{name}.py` and `tests/test_skill_{name}.py` exist. Remove `mark-build-done` CLI command entirely.

**Rationale:** Eliminates a manual step in the pipeline. Convention-based detection is reliable since D006 established the naming convention as the single source of truth.

**Implication:** `mark-build-done` CLI command removed. The underlying `mark_build_succeeded`/`mark_build_failed` functions remain in `build.py` for programmatic use.

---

## D009: Sandboxed build with diff allowlist (2026-02-09)

**Status:** `CURRENT`

**Context:** D001 established Claude Code as the external build backend. D008 used `detect_build_result()` (file-exists check) to auto-detect build completion. But running Claude Code in the canonical repo working tree is insufficient as a safety boundary — it could modify any file. We need a tighter model.

**Decision:** Build model A' — tools-enabled build in an isolated sandbox workspace.

The `build-skill` flow is:

1. **Build Packet (frozen)** — Generate `BUILD_PACKET_N` containing spec, I/O schema, constraints, allowed paths. The packet is epistemically closed: no new info discovery during the build attempt.

2. **Sandbox workspace** — Copy repo to `/tmp/kavi-build/<attempt_id>/`. Strip git remotes, secrets, and credentials. The sandbox is throwaway.

3. **Headless Claude Code invocation** — Run `claude -p --allowedTools [Edit,Write,Bash(limited)]` in the sandbox. Capture stdout/stderr + tool events as a build log artifact. No web/doc search during build.

4. **Diff allowlist gate** — After Claude exits, run `git diff --name-only` in the sandbox. The changed files must be a strict subset of allowed paths (`src/kavi/skills/{name}.py`, `tests/test_skill_{name}.py`). Fail if anything else changed. Replaces `detect_build_result()`.

5. **Verify in sandbox** — Run ruff/mypy/pytest/policy/invariants inside the sandbox, independently of Claude.

6. **Patch to canonical** — If verify passes, copy only the allowlisted files from sandbox into the canonical repo. Claude Code has zero role in promote, ledger writes, or registry authority.

**Iteration policy:** Iteration is across attempts, not within an attempt. If build/verify fails due to missing knowledge, run a research step (which may use web/docs) to update assumptions and regenerate `BUILD_PACKET_(N+1)`, then run Build Attempt N+1.

**Network policy:** Claude Code requires LLM endpoint egress. The constraint is: allow only LLM endpoint, block everything else. Enforcement is environmental (container or host firewall), not in-process. The build phase itself does not browse docs.

**Rationale:** Direct writes into the canonical repo are not a sufficient safety boundary. A sandbox + diff allowlist + independent verify provides defense in depth without introducing brittle stdout-parsing (model B). Model B (parse LLM output, write files ourselves) is kept as fallback only.

**Implication:**
- `detect_build_result()` replaced by `diff_allowlist_gate()`
- Build runs in throwaway workspace, not project root
- `--allowedTools` controls Claude Code's capabilities (not `--print` meaning "no tools")
- Research and build are separate phases with explicit handoff via build packets

---

## D010: Runtime trust enforcement via hash verification (2026-02-09)

**Status:** `CURRENT`

**Context:** After a skill is promoted to TRUSTED, nothing prevents the source file from being modified between promotion and execution. An attacker (or accidental edit) could change skill behavior without re-verification.

**Decision:** Re-hash the skill source file at load time (`load_skill`). Compare against the SHA256 stored in the registry at promote time. Refuse to execute on mismatch, raising `TrustError`.

**Rationale:** The promote step already computes and stores the hash. Verifying it at runtime closes the trust chain: propose → build → verify → promote (hash stored) → run (hash verified). The cost is one hash per `load_skill` call — negligible.

**Implication:**
- `_verify_trust()` added to `kavi.skills.loader`
- `TrustError` exception surfaced in CLI with remediation guidance
- If a skill file is edited post-promotion, it must be re-verified and re-promoted
- Registry entries without a `hash` field skip verification (backwards compat); now emits warning

---

## D011: Iteration/retry with two-layer research (2026-02-09)

**Status:** `CURRENT`

**Context:** Build attempts can fail (gate violations, timeouts, lint errors, etc.). Without retry, every failure requires manual intervention. D009 established sandboxed builds with diff allowlists, but had no recovery path for failures.

**Decision:** Iteration is ACROSS attempts, not within. Each attempt is epistemically closed (frozen BUILD_PACKET). Research happens between attempts.

**Two-layer research:**
1. **Layer 1 (canonical):** Deterministic failure classifier extracts `failure_kind` + `failure_facts` from build/verify logs. Drives ledger state. Fully testable.
2. **Layer 2 (advisory):** LLM proposes BUILD_PACKET diff based on classification. Engine validates and gates the diff (schema + policy + no permission widening). Human required on escalation.

**Escalation triggers:** repeated same failure (≥3), permission widening, security-class failures, large packet diffs (>50%), ambiguity.

**State machine:** PROPOSED + BUILT are buildable. VERIFIED is stable and never overloaded.
```
propose → build_1 (fail, stays PROPOSED) → research → build_2 → ... → build_N (success → BUILT)
                                                                         → verify → promote → run
```

**Schema:** `builds.attempt_number` + `builds.parent_build_id`. `ArtifactKind.RESEARCH_NOTE` for research output.

**Rationale:** Epistemically closed attempts prevent unbounded iteration within a single build. The two-layer approach separates deterministic (testable) classification from advisory (LLM) suggestions. Escalation triggers ensure human review for high-risk retries.

**Implication:**
- `build_skill()` accepts PROPOSED and BUILT proposals (BUILT resets to PROPOSED on retry)
- `research_skill()` produces RESEARCH_NOTE artifacts from failed builds
- `advise_retry()` calls Sparkstation LLM for advisory packet diffs
- Build packets keyed by `build_id` (unique per attempt, not per proposal)
- CLI: `kavi research-skill <build_id> [--hint] [--advise/--no-advise]`

---

## D012: Expanded diff allowlist for runtime support modules (2026-02-09)

**Status:** `CURRENT`

**Context:** Skills increasingly need shared runtime infrastructure. `summarize_note` uses `kavi.llm.spark.generate()` which existed before the skill was forged. But `search_notes` needs `embed()` which doesn't exist yet. The forge's diff allowlist (D009) only permits `src/kavi/skills/{name}.py` and `tests/test_skill_{name}.py`, so the sandbox build cannot add the embedding function to `spark.py`. This forces manual edits outside the forge, breaking the "no manual code edits" principle.

**Decision:** Expand the diff allowlist to include a tight set of runtime support modules:

| Path | Required | Purpose |
|------|----------|---------|
| `src/kavi/skills/{name}.py` | Yes | Skill implementation |
| `tests/test_skill_{name}.py` | Yes | Skill tests |
| `src/kavi/llm/spark.py` | No | Sparkstation client (embeddings, generation) |
| `src/kavi/config.py` | No | Path constants, model config |
| `tests/test_spark_client.py` | No | Spark client tests |

The skill + test files remain **required** (gate fails if missing). The runtime support modules are **optional** (gate passes whether or not they're touched).

**Boundary enforcement:** To prevent runtime modules from reaching into governance code, a new invariant check verifies that any modified runtime support module does NOT import from `kavi.forge`, `kavi.ledger`, or `kavi.policies`. This keeps the runtime layer clean and prevents privilege escalation.

**Non-goals:**
- No edits to `forge/`, `ledger/`, `policies/`, CLI core (`cli.py`), or `pyproject.toml`
- No arbitrary file additions — only the enumerated paths
- No weakening of existing safety gates (policy scanner, structural invariants, extended safety)

**Rationale:** The forge should be the only path for code changes. Allowing a small, enumerated set of runtime support modules keeps the "no manual edits" rule intact while enabling skills that need shared infrastructure. The import boundary invariant prevents governance-layer contamination.

**Implication:**
- `diff_allowlist_gate()` updated with optional allowed paths
- `_check_scope()` in invariants updated to match
- New `_check_runtime_imports()` invariant added
- Build packet template updated to inform Claude Code about optional files

---

## D013: Secrets and network governance (2026-02-10)

**Status:** `CURRENT`

**Context:** Kavi has four shipped skills (all READ_ONLY or FILE_WRITE). The next skill (`http_get_json`) needs to make network requests and read API keys from environment variables. The governance layer had no support for SECRET_READ as a side-effect class, no mechanism to surface required secrets through the registry, and no detection of accidental secret leaks in skill code.

**Decision:** Multi-layered governance for secrets and network skills:

1. **SECRET_READ enum value** — added to `SideEffectClass`, schema v5 migration (table recreate for CHECK constraint, established pattern from v3/v4).

2. **Required secrets surfacing** — `SkillProposal.required_secrets_json` already existed but promote hardcoded `"required_secrets": []` in the registry. Fixed to propagate `json.loads(proposal.required_secrets_json)`. `SkillInfo` in consumer shim now exposes `required_secrets: list[str]`.

3. **Secret-leak detection** — Best-effort AST rule (`secret_leak`) in policy scanner. Detects `print(os.environ[...])`, `print(os.getenv(...))`, and f-string interpolation of env vars in print/log calls. Always-on, not configurable. Cannot track variable flow (by design — catches obvious patterns).

4. **Confirmation gate expansion** — `CONFIRM_SIDE_EFFECTS` in `agent/constants.py` expanded from `{FILE_WRITE}` to `{FILE_WRITE, NETWORK, SECRET_READ}`. Both network and secret access require explicit user consent.

5. **CLI flag** — `--required-secrets` added to `kavi propose-skill` (JSON list of env var names).

**HTTP library choice:** `urllib.request` (stdlib). No new dependency needed.

**Rationale:** Each layer catches a different failure mode: enum/schema prevents invalid proposals, required_secrets makes secrets visible in the registry, leak detection catches careless print statements, confirmation gate requires user consent for risky operations. Together they enable network+secret skills while maintaining governed capability growth (D004).

**Implication:**
- Schema version 5 (migration required for existing DBs)
- `propose-skill` CLI accepts `--required-secrets`
- Policy scanner now has `secret_leak` rule (always-on)
- Agent chat confirms NETWORK and SECRET_READ skills before execution
- `http_get_json` can be proposed with `--side-effect NETWORK --required-secrets '["API_KEY"]'`

---

## D015: SessionContext — anchor-based reference resolution (2026-02-10)

**Status:** `CURRENT`

**Context:** Chat v0 (D013) is stateless — each `handle_message()` call is independent. Users can't say "summarize that" or "do it again but shorter" because there's no memory of what "that" refers to. Adding full conversation history would violate the bounded/deterministic design (invariant #8), but lightweight references to prior execution results are safe.

**Decision:** Add `SessionContext` — a sliding window of up to 10 `Anchor` objects extracted from execution records. Each successful execution produces one anchor containing the skill name, execution ID, and a curated subset of the output (not the full blob). A deterministic resolver binds ref markers (`last`, `last_<skill>`, `exec:<id>`) to concrete anchor values between parse and plan.

**Components:**
1. **Anchor model** — `kind` (execution|artifact), `label`, `execution_id`, `skill_name`, `data` (curated subset)
2. **SessionContext** — `anchors: list[Anchor]` with `add_from_records()`, `resolve(ref)`, `ambiguous(ref)`
3. **Resolver** (`agent/resolver.py`) — `resolve_refs(intent, session)` runs between parse and plan, replacing ref markers with concrete values. Returns `AmbiguityResponse` if ref is ambiguous.
4. **Parser ref markers** — LLM prompt and deterministic fallback emit `ref:last`, `ref:last_<skill>` for "that"/"it"/"the result"/"again"
5. **REPL accumulation** — `_chat_repl` maintains `SessionContext` across turns, passes to `handle_message`, receives updated session in response

**Ref patterns (deterministic):**
- `last` / `that` / `it` / `the result` → most recent anchor
- `last_<skill>` → most recent anchor for named skill (e.g. `last_search`)
- `exec:<id_prefix>` → anchor by execution ID prefix match

**Key constraints:**
- `session=None` preserves backward compatibility (stateless mode)
- Works without Sparkstation — deterministic fallback handles refs too
- Anchors are capped at 10 (sliding window, oldest evicted first)
- If ref is ambiguous (multiple candidates), return disambiguation prompt instead of guessing
- No persistence — session lives only in REPL memory, dies when process exits

**Rationale:** Anchor-based refs are minimal, deterministic, and inspectable. They don't require conversation history, LLM memory, or persistent state. The resolver is a pure function that can be tested independently. The sliding window prevents unbounded growth.

**Implication:**
- `AgentResponse` gains `session: SessionContext | None` field
- `handle_message()` gains `session: SessionContext | None` parameter
- New file: `src/kavi/agent/resolver.py`
- Parser changes: ref marker detection in both LLM and deterministic modes
- REPL changes: session accumulation across turns
- Single-turn CLI mode remains stateless (`session=None`)

---

## D016: Internal protocol canonical; external formats are boundary adapters (2026-02-10)

**Status:** `CURRENT`

**Context:** Phase 4 multi-turn and future external API surfaces (webhook, WhatsApp/Telegram chat client). Should Kavi adopt OpenAI's Responses API or Conversations API wire format for multi-turn support?

**Decision:** Kavi's internal models (`AgentResponse`, `SessionContext`, `ParsedIntent`, `PlannedAction`, `ExecutionRecord`) remain the canonical protocol. External wire formats (OpenAI Chat Completions, Responses API, WhatsApp webhook format, etc.) are presentation concerns handled by thin adapters at the API boundary.

**Rationale:**
1. **Conceptual mismatch.** OpenAI's formats assume the LLM is the orchestrator — it decides what tool to call. Kavi's model is the opposite: LLM parses (layer 1), planner is deterministic (layer 2), execution is governed (layer 3). Adopting their format would obscure the governance semantics.
2. **Governance features have no analog.** Confirmation gates, proposed-but-not-approved actions, deterministic planning, trust-verified execution, and provenance chains have no representation in OpenAI's wire formats. Bolting them on as non-standard extensions defeats the purpose of adopting a standard.
3. **Version decoupling.** OpenAI's formats evolve frequently (Chat Completions → function calling → tool_choice → Responses API). Internal stability is more valuable than external compatibility.
4. **Adapter flexibility.** A thin translation layer at the API boundary can support multiple external formats simultaneously without changing the core. The adapter is small, testable, and disposable if the external format changes.

**Implication:**
- No structural changes to `kavi.agent` models for external format compatibility
- Future HTTP API surface will have an adapter module (e.g. `kavi.api.adapters`) that translates between internal models and client wire formats
- The adapter is a presentation concern, not a domain concern — it lives at the edge, not in `kavi.agent`

---

## D017: Chat Surface v1 — surface inversion with TalkIntent (2026-02-10)

**Status:** `CURRENT`

**Context:** Kavi Chat v0 was "a governed agent exposed through a CLI" — unmatched input returned errors, confirmations showed raw JSON, all output was structured/mechanical. Users wanted it to feel like a regular chatbot.

**Decision:** Invert the surface: Kavi becomes "a regular chatbot powered by a governed agent." The engine (parse → plan → confirm → execute → log) stays unchanged. The presentation layer changes. Three structural additions:

1. **TalkIntent (effect=NONE).** Default path when no skill matches. Generates conversational response via Sparkstation (graceful fallback to canned help text). Logged as ExecutionRecord with `skill_name="__talk__"`, `side_effect_class="NONE"`. Cannot call tools. May reference SessionContext anchors for context.

2. **Presenter module (`kavi.agent.presenter`).** Template-based formatting for all response types — conversational confirmations ("I'll write this to your notes — okay?"), natural success messages, subtle error text. No LLM formatting pass for boilerplate. LLM is used only where it adds semantic value (TalkIntent, TransformIntent).

3. **PendingConfirmation model.** Formalized confirmation stashing with plan + intent + session snapshot + TTL (5 minutes). `confirm_pending()` validates TTL then executes — no re-parse, no re-resolve. Replaces ad-hoc stashing in CLI code.

**Guardrails (non-negotiable):**
- Confirmation policy remains strictly mechanical: READ_ONLY auto-executes, FILE_WRITE/NETWORK/SECRET_READ require confirmation. Only the *phrasing* changes, never the *gates*.
- `--verbose` / `/verbose` is load-bearing for inspectability (invariant #8). Must fully expose AgentResponse, plan, records, and session state.
- Internal protocol remains canonical (D016 stands). Presenter is a boundary adapter.

**Rationale:**
1. Most user turns are conversation, not tool invocations. TalkIntent makes this a first-class concept instead of an error path.
2. Template-based formatting avoids LLM latency and failure modes for routine output. LLM generation is reserved for where it adds value.
3. Formalized stashing prevents re-parse bugs and enables TTL-based expiry for safety.
4. Verbose mode preserves full inspectability — the governance engine is always visible on demand.

**Implications:**
- `UnsupportedIntent` now reserved for harmful/impossible requests (LLM only). Deterministic parser catch-all returns `TalkIntent`.
- New pseudo-skill `__talk__` appears in execution logs and session anchors.
- REPL version string changes from "Kavi Chat v0" to "Kavi Chat".
- `kavi chat --verbose` and REPL `/verbose` command for full detail mode.
