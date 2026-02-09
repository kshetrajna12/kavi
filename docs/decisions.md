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
