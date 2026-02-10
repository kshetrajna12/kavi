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
4. **Diff allowlist gate** — `git diff --name-only HEAD` + `git ls-files --others` against the sandbox baseline. Changed files must be a strict subset of the allowed paths. Gate fails if anything outside the allowlist changed, or if both required files are missing.

   | Path | Required | Purpose |
   |------|----------|---------|
   | `src/kavi/skills/{name}.py` | Yes | Skill implementation |
   | `tests/test_skill_{name}.py` | Yes | Skill tests |
   | `src/kavi/llm/spark.py` | No | Sparkstation client (D012) |
   | `src/kavi/config.py` | No | Configuration constants (D012) |
   | `tests/test_spark_client.py` | No | Spark client tests (D012) |
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
| invariants | `forge/invariants.py` | Structural (AST: extends BaseSkill, required attrs, side-effect match), scope (git diff: only skill + test + runtime support files), safety (`__import__()`, `importlib.import_module()`), runtime boundary (D012: runtime support modules must not import forge/ledger/policies) |

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

## Failure Canon

The failure canon (`tests/test_failure_drill.py`) is a deterministic drill suite that exercises the research → retry loop across every failure class. Each drill engineers a specific, realistic defect — a forbidden import, an unused variable, a failing test, a gate violation — and proves that the forge classifies it correctly, produces a research note, and self-corrects on the next attempt without permission widening.

| Drill | FailureKind | Defect |
|-------|-------------|--------|
| VERIFY_LINT | `VERIFY_LINT` | Unused import triggers ruff F401 |
| VERIFY_TYPE | `VERIFY_LINT` | Stubbed mypy failure |
| VERIFY_TEST | `VERIFY_TEST` | Stubbed pytest failure |
| VERIFY_POLICY | `VERIFY_POLICY` | `import subprocess` triggers policy scanner |
| GATE_VIOLATION | `GATE_VIOLATION` | Disallowed file in diff gate |

Each drill follows the same arc: propose → build₁ → write flawed code → verify₁ (FAIL) → research (classify + facts) → build₂ (enriched packet) → write fixed code → verify₂ (PASS) → promote (TRUSTED). The gate violation drill fails at the build stage rather than verification, exercising a different code path.

A `DrillRunner` provides selective real tooling — ruff, the policy scanner, and invariant checks always run against real code — while mypy and pytest are stubbed for speed and determinism. This is proof, not aspiration: the canon runs in the default test suite on every commit.

---

## Consumer shim

The consumer shim (`kavi.consumer.shim`) is the runtime interface for downstream systems that consume the trusted skill registry. It sits outside the forge pipeline — it does not propose, build, verify, or promote. It only executes skills that have already earned TRUSTED status.

### What it does

1. **Lists trusted skills** — `get_trusted_skills()` loads the registry, trust-verifies each skill, and returns structured metadata including JSON schemas derived from the Pydantic input/output models.
2. **Executes a named skill** — `consume_skill()` loads a skill with trust verification, validates the JSON input against the skill's declared schema, executes it, and returns a structured `ExecutionRecord`.
3. **Captures provenance** — Every execution produces an `ExecutionRecord` containing: skill name, source hash, side-effect class, input/output JSON, timestamps, and success/error status. This is the audit trail.

### What it does NOT do

- No planning, tool selection, or LLM involvement
- No memory or conversation state
- No permission widening or policy changes
- No NETWORK side effects

### ExecutionRecord

| Field | Type | Description |
|-------|------|-------------|
| `v` | int | Record schema version (currently `1`) |
| `execution_id` | str | Unique ID (uuid4 hex), auto-generated |
| `parent_execution_id` | str \| None | Optional chain to a prior execution |
| `skill_name` | str | Name of the executed skill |
| `source_hash` | str | SHA256 hash verified at load time |
| `side_effect_class` | str | Governance-declared side-effect class |
| `input_json` | dict | Validated input passed to the skill |
| `output_json` | dict \| None | Skill output (None on failure) |
| `success` | bool | Whether execution completed without error |
| `error` | str \| None | Error type and message if failed |
| `started_at` | str | ISO 8601 UTC timestamp |
| `finished_at` | str | ISO 8601 UTC timestamp |

### Execution log persistence

Every `consume-skill` invocation appends its `ExecutionRecord` to an append-only JSONL file (default `~/.kavi/executions.jsonl`). The `ExecutionLogWriter` in `consumer/log.py`:

- Creates parent directories if missing.
- Appends atomically via `open` + `O_APPEND` + `fsync`.
- Never reads back; tolerates malformed existing lines.

File format: one JSON object per line, matching the `ExecutionRecord` schema above. Example:

```jsonl
{"execution_id":"a1b2c3...","parent_execution_id":null,"skill_name":"write_note","source_hash":"357f...","side_effect_class":"FILE_WRITE","input_json":{...},"output_json":{...},"success":true,"error":null,"started_at":"2025-06-15T10:30:00+00:00","finished_at":"2025-06-15T10:30:01+00:00"}
```

### CLI

```bash
kavi consume-skill <name> --json '{"key": "value"}'         # execute + log
kavi consume-skill <name> --json '...' --log-path /tmp/x.jsonl  # custom log path
kavi consume-skill <name> --json '...' --no-log             # skip logging

kavi tail-executions                                         # last 20 records
kavi tail-executions --n 5 --only-failures --skill write_note
```

`consume-skill` prints the `ExecutionRecord` as JSON and appends to the log. Exits non-zero on failure. `tail-executions` reads and filters the JSONL log.

### Boundary

The consumer shim depends on:
- `skills/loader.py` — `load_skill()` for trust-verified loading, `list_skills()` for registry enumeration
- `skills/base.py` — `BaseSkill.validate_and_run()` for schema validation and execution

It does NOT depend on the ledger, forge, or any build infrastructure.

---

## Shipped skills

| Skill | Side Effect | Description |
|-------|-------------|-------------|
| `write_note` | FILE_WRITE | Write a markdown note to the vault |
| `read_notes_by_tag` | READ_ONLY | Find notes matching a tag |
| `summarize_note` | READ_ONLY | Summarize a vault note via Sparkstation with graceful fallback |

### summarize_note

Uses Sparkstation (local LLM gateway) at runtime with strict schema output and graceful fallback.

**Input**: `path` (vault-relative), `style` ("bullet" | "paragraph"), `max_chars` (default 12000), `timeout_s` (default 12.0).

**Output**: `path`, `summary`, `key_points` (list[str]), `truncated` (bool), `used_model` (model name or "fallback"), `error` (str | None).

**Behavior**:
1. Validates path is within `vault_out/` — rejects traversal, absolute paths, symlinks, non-existent files.
2. Reads content as UTF-8; truncates to `max_chars` if needed.
3. Calls `kavi.llm.spark.generate()` with a JSON-output prompt; parses response.
4. On any Sparkstation failure (unavailable, timeout, bad JSON): deterministic fallback — first ~500 chars prefixed with `[Fallback summary]`, `key_points=[]`, `used_model="fallback"`, `error` populated.

---

## Project layout

```
src/kavi/
├── cli.py              # Typer CLI entry point
├── config.py           # Path constants + Sparkstation config
├── artifacts/
│   └── writer.py       # Content-addressed artifact writer
├── consumer/
│   ├── shim.py         # Consumer runtime: load, validate, execute, audit
│   └── log.py          # Append-only JSONL execution log
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
    ├── write_note.py        # Skill (FILE_WRITE)
    ├── read_notes_by_tag.py # Skill (READ_ONLY)
    └── summarize_note.py    # Skill (READ_ONLY, Sparkstation)

tests/
├── test_failure_drill.py  # Failure canon (see above)
└── ...                    # See test markers above
docs/
├── ARCHITECTURE.md     # This file
└── decisions.md        # Append-only decision log (D001–D011)
```
