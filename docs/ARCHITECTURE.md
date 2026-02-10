# Kavi Forge — Architecture

Internal reference for the forge's implementation. For the high-level overview, see the [README](../README.md). For design rationale, see [decisions.md](decisions.md).

---

## Ledger (SQLite, schema v4)

The ledger is the single source of truth ([D002](decisions.md)). All other representations (registry YAML, markdown artifacts) are derived.

### Tables

| Table | Purpose |
|-------|---------|
| `skill_proposals` | Name, description, I/O schema, side-effect class, status (PROPOSED → BUILT → VERIFIED → TRUSTED) |
| `builds` | Per-attempt records with `attempt_number` and `parent_build_id` for lineage |
| `verifications` | Per-gate pass/fail: ruff, mypy, pytest, policy, invariants |
| `promotions` | Audit trail — who approved, when, from/to status |
| `artifacts` | Content-addressed (SHA256) references to specs, build packets, logs, research notes |
| `schema_version` | Migration tracking |

### Artifact kinds

`SKILL_SPEC`, `BUILD_PACKET`, `BUILD_LOG`, `VERIFICATION_REPORT`, `RESEARCH_NOTE`, `PATCH_SUMMARY`, `NOTE`.

### Migrations

SQLite cannot `ALTER CHECK` constraints, so migrations that widen enum-like checks recreate the table (see migration 3 and 4 patterns in `ledger/db.py`).

---

## Sandbox build (D009)

Builds run in an isolated workspace, never in the canonical repo.

### Flow

1. **Copy working tree** to `/tmp/kavi-build/<build_id>/repo/`. Excludes `.git/`, `.venv/`, secrets (`.env`, `*.pem`, `*.key`, `credentials.json`), databases, caches, and special files (sockets, FIFOs).
2. **Initialize fresh git repo** with a baseline commit. Zero hooks, zero remotes.
3. **Invoke Claude Code** headlessly: `claude -p --output-format text --allowedTools Edit Write Glob Grep Read`. Bash is intentionally excluded. Input is the BUILD_PACKET content via stdin.
4. **Diff allowlist gate** — `git diff --name-only HEAD` + `git ls-files --others` against the sandbox baseline. Changed files must be a strict subset of:
   - `src/kavi/skills/{name}.py`
   - `tests/test_skill_{name}.py`

   Gate fails if anything else changed, or if both required files are missing.
5. **Safe copy-back** — allowlisted files are copied to the canonical repo. Rejects symlinks, path traversal (`..`), absolute paths, and unnormalized paths. Validates resolved destination is under project root.

### Build packets

Generated from the proposal spec. For retries (attempt > 1), enriched with:
- Previous attempt failure analysis
- Research note content
- LLM advisory (if available)

Keyed by `build_id` (unique per attempt), not `proposal_id`.

---

## Verification gates

Five independent checks run via a `ToolRunner` protocol (injectable for testing):

| Gate | Tool | What it checks |
|------|------|---------------|
| ruff | `ruff check` | Linting (style, imports, naming conventions) |
| mypy | `mypy` | Type checking |
| pytest | `pytest -q --tb=short` | Unit tests |
| policy | `policies/scanner.py` | Forbidden imports (`subprocess`, `os.system`), `eval`/`exec`, regex-based patterns from `policy.yaml` |
| invariants | `forge/invariants.py` | Structural (AST: extends BaseSkill, required attrs, side-effect match), scope (git diff: only skill + test files), safety (`__import__()`, `importlib.import_module()`) |

All five must pass for status to advance to VERIFIED. A `SubprocessRunner` handles production execution; tests use a `StubRunner`.

---

## Research and retry (D011)

Iteration is across attempts, not within. Each attempt is epistemically closed (frozen BUILD_PACKET).

### Layer 1 — Deterministic classifier

`classify_failure()` in `forge/research.py` extracts a `FailureKind` and structured facts from build/verify logs:

| FailureKind | Trigger |
|-------------|---------|
| `GATE_VIOLATION` | Diff gate found disallowed files |
| `TIMEOUT` | Build timed out |
| `BUILD_ERROR` | Non-zero exit, missing CLI, etc. |
| `VERIFY_LINT` | ruff or mypy failed |
| `VERIFY_TEST` | pytest failed |
| `VERIFY_POLICY` | Policy scanner found violations |
| `VERIFY_INVARIANT` | Invariant check failed |
| `UNKNOWN` | Could not determine cause |

Produces a `RESEARCH_NOTE` artifact with classification, facts, and log excerpt.

### Layer 2 — LLM advisory (optional)

`advise_retry()` calls Sparkstation to propose a corrected BUILD_PACKET. Bounded input (`SPARK_MAX_PROMPT_CHARS`), enforced timeout, graceful degradation: if Sparkstation is unreachable, falls back to deterministic-only (returns original packet + `AMBIGUOUS` escalation trigger).

### Escalation triggers

| Trigger | Condition |
|---------|-----------|
| `REPEATED_FAILURE` | >= 3 consecutive failed builds |
| `PERMISSION_WIDENING` | Proposed packet introduces escalating keywords (network, money, messaging, secret) |
| `SECURITY_CLASS` | Failure kind is VERIFY_POLICY or VERIFY_INVARIANT |
| `LARGE_DIFF` | > 50% of packet lines changed |
| `AMBIGUOUS` | Failure kind is UNKNOWN, or Sparkstation unavailable |

Non-empty triggers require human review before retry.

### State machine

```
propose → build_1 (fail, stays PROPOSED) → research → build_2 → ... → build_N (success → BUILT)
                                                                         → verify → promote → run
```

`PROPOSED` and `BUILT` are buildable. `BUILT` resets to `PROPOSED` on retry.

---

## Sparkstation integration

Local LLM gateway at `http://localhost:8000/v1` (OpenAI-compatible API). Client in `kavi.llm.spark`.

| Function | Purpose |
|----------|---------|
| `is_available()` | Healthcheck via `client.models.list()`. Returns bool. |
| `generate()` | Chat completion. Truncates prompt to `SPARK_MAX_PROMPT_CHARS`, enforces `SPARK_TIMEOUT`. Raises `SparkUnavailableError` on connection failure, `SparkError` on empty response. |

Configuration in `kavi.config`:

| Constant | Default | Purpose |
|----------|---------|---------|
| `SPARK_BASE_URL` | `http://localhost:8000/v1` | Gateway endpoint |
| `SPARK_MODEL` | `gpt-oss-20b` | Default model for advisory |
| `SPARK_TIMEOUT` | 30s | Request timeout |
| `SPARK_MAX_PROMPT_CHARS` | 8000 | Input truncation bound |

---

## Trust enforcement (D010)

The trust chain spans the full lifecycle:

```
propose (spec) → build (sandbox) → verify (5 gates) → promote (hash stored) → run (hash re-verified)
```

At promote time, `promote_skill()` computes SHA256 of the skill source file and stores it in `registry.yaml`. At runtime, `load_skill()` re-hashes the file and compares. Mismatch raises `TrustError`; the skill will not execute.

Registry entries without a hash field skip verification (backwards compatibility) but emit a warning.

---

## Convention-based paths (D006)

All paths derived from the proposal name:

| What | Pattern |
|------|---------|
| Skill file | `src/kavi/skills/{name}.py` |
| Test file | `tests/test_skill_{name}.py` |
| Module path | `kavi.skills.{name}.{CamelCase}Skill` |

No custom paths supported. Single source of naming truth across build packets, diff gate, verification, and promotion.

---

## Testing

```bash
uv run pytest -q              # Fast suite (~3s, 116+ tests, no network)
uv run pytest -m slow         # Integration tests (real subprocesses)
uv run pytest -m spark        # Live Sparkstation tests (requires gateway)
uv run ruff check src/ tests/ # Lint
uv run mypy                   # Type check
```

| Marker | Scope | Default |
|--------|-------|---------|
| (none) | Unit tests, fully mocked | Included |
| `slow` | Integration tests with real subprocesses | Excluded |
| `spark` | Tests requiring live Sparkstation gateway | Excluded |

---

## Project layout

```
src/kavi/
├── cli.py              # Typer CLI entry point
├── config.py           # Path constants + Sparkstation config
├── artifacts/
│   └── writer.py       # Content-addressed artifact writer
├── forge/
│   ├── build.py        # Sandbox build, diff gate, Claude invocation
│   ├── invariants.py   # Structural/scope/safety invariant checks
│   ├── paths.py        # Convention-based path derivation
│   ├── promote.py      # VERIFIED → TRUSTED promotion
│   ├── propose.py      # Skill proposal creation
│   ├── research.py     # Failure classification + LLM advisory
│   └── verify.py       # 5-gate verification
├── ledger/
│   ├── db.py           # Schema, migrations (v1→v4)
│   └── models.py       # Pydantic models + DB operations
├── llm/
│   └── spark.py        # Sparkstation client
├── policies/
│   └── scanner.py      # Policy scanner
└── skills/
    ├── base.py         # BaseSkill ABC
    ├── loader.py       # Registry loader + trust verification
    └── write_note.py   # Example skill (FILE_WRITE)

tests/                  # See test markers above
docs/
├── ARCHITECTURE.md     # This file
└── decisions.md        # Append-only decision log (D001–D011)
```
