# Kavi Forge — Architecture

Internal reference for the forge's implementation. For the high-level overview, see the [README](../README.md). For design rationale, see [decisions.md](decisions.md).

---

## Ledger (SQLite, schema v5)

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

SQLite cannot `ALTER CHECK` constraints, so migrations that widen enum-like checks recreate the table (see migration 3, 4, and 5 patterns in `ledger/db.py`).

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
| `embed()` | Batch text embeddings. Returns `list[list[float]]`, sorted by index. Raises `SparkUnavailableError` on connection failure, `SparkError` on empty response. |

Configuration in `kavi.config`:

| Constant | Default | Purpose |
|----------|---------|---------|
| `SPARK_BASE_URL` | `http://localhost:8000/v1` | Gateway endpoint |
| `SPARK_MODEL` | `gpt-oss-20b` | Default model for advisory |
| `SPARK_EMBED_MODEL` | `bge-large` | Default model for embeddings |
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
uv run pytest -q              # Fast suite (~3s, 619 tests, no network)
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
- No autonomous NETWORK side effects (NETWORK skills require user confirmation)

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

## Execution chains

The chain executor (`kavi.consumer.chain`) runs a fixed sequence of skill steps with deterministic input mapping between them. No LLM planning or auto-mapping — purely explicit.

### ChainSpec

A chain is defined by a `ChainSpec` containing ordered `ChainStep` entries and `ChainOptions`:

```json
{
  "steps": [
    {
      "skill_name": "search_notes",
      "input": {"query": "machine learning", "top_k": 5}
    },
    {
      "skill_name": "summarize_note",
      "input_template": {"style": "bullet"},
      "from_prev": [
        {"to_field": "path", "from_path": "results.0.path"}
      ]
    }
  ],
  "options": {"stop_on_failure": true}
}
```

### Input mapping

Each step specifies either `input` (full JSON) or `input_template` + `from_prev` (mapped from prior outputs). Mappings use dot-path extraction:

| Path | Meaning |
|------|---------|
| `field` | `output["field"]` |
| `field.subfield` | `output["field"]["subfield"]` |
| `results.0.path` | `output["results"][0]["path"]` |

By default, `from_prev` references the immediately previous step. Set `from_step_index` on a `FieldMapping` to reference an earlier step by index.

### Execution semantics

1. Steps run sequentially. Each produces an `ExecutionRecord`.
2. **Mapping gate**: If a dot-path extraction fails (missing key, out-of-range index), the step produces a FAILURE record without invoking the skill.
3. **Schema validation gate**: After mapping, input is validated against the skill's declared schema. Type or missing-field errors produce a FAILURE record without invocation.
4. **Skill execution**: If mapping + validation pass, executes via `consume_skill()` (reuses trust verification).

### Lineage

- Step 0 has `parent_execution_id = None`.
- Step *i* > 0 defaults `parent_execution_id` to step *i*-1's `execution_id`.
- Override with `parent_index` on a `ChainStep` to reference a different step.

### Stop behavior

- `stop_on_failure=true` (default): chain halts after the first failed step.
- `stop_on_failure=false`: subsequent steps run, but mappings referencing failed step outputs fail cleanly with a descriptive error.

### CLI

```bash
# Generic chain execution
kavi consume-chain --json '{"steps": [...], "options": {...}}'

# Convenience: search + summarize top result
kavi search-and-summarize --query "machine learning" --top-k 5 --style bullet
```

Both commands log all `ExecutionRecord`s to the JSONL execution log and exit non-zero if any step fails.

---

## Execution replay

The replay command (`kavi replay`) re-runs a past execution safely and audibly. It loads an `ExecutionRecord` from the JSONL log, validates that the skill is still TRUSTED with a matching source hash, then re-executes with the exact same input via `consume_skill()`.

### Validation

Before replaying, three checks must pass:

1. **Record exists** — the execution_id must be found in the JSONL log.
2. **Skill in registry** — the skill must still be present and TRUSTED.
3. **Hash match** — the registry hash must match the hash recorded at original execution time. If the skill source has changed since the original execution, replay refuses.

### Output

A new `ExecutionRecord` is produced with:
- A fresh `execution_id`
- `parent_execution_id` set to the original execution's ID
- All other fields populated normally by `consume_skill()`

The new record is appended to the JSONL log (unless `--no-log` is passed).

### CLI

```bash
kavi replay --execution-id abc123...         # replay and log
kavi replay --execution-id abc123... --no-log  # replay without logging
```

Example output:

```
Replayed summarize_note from aaa111bbb222… → ccc333ddd444…
{ ... full ExecutionRecord JSON ... }
```

---

## Session inspection

The session command (`kavi session`) provides a read-only view of an execution chain as a human-readable tree. It reads the JSONL log, builds a graph from `parent_execution_id` linkage, and renders a compact tree.

### Graph construction

1. Starting from the given execution_id, walk backward via `parent_execution_id` to find the root.
2. From the root, walk forward to collect all descendants.
3. Sort by `started_at` for deterministic ordering.
4. Handle branching (multiple children of the same parent) gracefully.

### Tree output

Each node shows: skill name, success/failure marker, shortened execution_id, duration, and error message (if failed).

```
Session:
  search_notes ✅  (id=abcd12345678…)  [1.2s]
    summarize_note ✅ (id=efgh12345678…)  [3.4s]
      write_note ❌ (id=ijkl12345678…)  [0ms]  needs confirmation
```

### CLI

```bash
kavi session --execution-id abc123...        # tree view from specific execution
kavi session --latest                        # tree view from most recent execution
kavi session --execution-id abc123... --json # raw JSON records instead of tree
```

---

## Shipped skills

| Skill | Side Effect | Description |
|-------|-------------|-------------|
| `write_note` | FILE_WRITE | Write a markdown note to the vault |
| `read_notes_by_tag` | READ_ONLY | Find notes matching a tag |
| `summarize_note` | READ_ONLY | Summarize a vault note via Sparkstation with graceful fallback |
| `search_notes` | READ_ONLY | Semantic search over vault notes via bge-large embeddings with lexical fallback |
| `http_get_json` | NETWORK | Fetch JSON from a URL via stdlib urllib.request (D013: SECRET_READ governance) |
| `create_daily_note` | FILE_WRITE | Create or append a timestamped entry to today's daily note |

### summarize_note

Uses Sparkstation (local LLM gateway) at runtime with strict schema output and graceful fallback.

**Input**: `path` (vault-relative), `style` ("bullet" | "paragraph"), `max_chars` (default 12000), `timeout_s` (default 12.0).

**Output**: `path`, `summary`, `key_points` (list[str]), `truncated` (bool), `used_model` (model name or "fallback"), `error` (str | None).

**Behavior**:
1. Validates path is within `vault_out/` — rejects traversal, absolute paths, symlinks, non-existent files.
2. Reads content as UTF-8; truncates to `max_chars` if needed.
3. Calls `kavi.llm.spark.generate()` with a JSON-output prompt; parses response.
4. On any Sparkstation failure (unavailable, timeout, bad JSON): deterministic fallback — first ~500 chars prefixed with `[Fallback summary]`, `key_points=[]`, `used_model="fallback"`, `error` populated.

### search_notes

Semantic search over vault markdown files using Sparkstation `bge-large` embeddings. First skill to exercise the D012 expanded diff allowlist — the forge built both the skill and the `embed()` infrastructure in `spark.py` in a single sandbox pass.

**Input**: `query` (str), `top_k` (1–20, default 5), `max_chars` (default 12000), `timeout_s` (default 8.0), `include_snippet` (default true), `tag` (optional filter).

**Output**: `query`, `results` (list of `{path, score, title, snippet}`), `truncated_paths`, `used_model` (model name or "lexical-fallback"), `error` (str | None).

**Behavior**:
1. Enumerates `vault_out/**/*.md` — skips symlinks, traversal, non-UTF-8 files. Applies optional tag filter via `#tag` heuristic.
2. Reads each note as UTF-8; truncates to `max_chars` (records in `truncated_paths`).
3. Calls `kavi.llm.spark.embed()` for query + all note contents; ranks by cosine similarity.
4. On Sparkstation unavailable: deterministic lexical fallback (case-insensitive token match), `used_model="lexical-fallback"`, `error="SPARKSTATION_UNAVAILABLE"`.
5. Returns top-k results sorted by score descending.

---

## Chat v0

The agent layer (`kavi.agent`) is a bounded conversational interface over trusted skills. It is NOT an autonomous agent — it executes at most one action per user message and never retries.

### Boundaries

- **At most one execution per message**: either a single `consume_skill` call, or a fixed 2-step `consume_chain` (search → summarize).
- **No loops, no retries, no multi-step planning.**
- **Anchor-based session context (D015)**: the REPL maintains a sliding window of up to 10 anchors from prior execution results. Users can refer to previous results with "that", "it", "again", `ref:last`, or `ref:last_<skill>`. In single-turn mode (`kavi chat -m`), no session is maintained.
- **Transparent**: every response includes the parsed intent, planned action, and execution records.

### Supported intents (v0)

| Intent | Kind | Execution |
|--------|------|-----------|
| Search and summarize | `search_and_summarize` | 2-step chain: `search_notes` → `summarize_note` |
| Write a note | `write_note` | Single skill: `write_note` (requires confirmation) |
| Generic skill invocation | `skill_invocation` | Single skill by name (e.g. `summarize_note`, `http_get_json`) |
| Refine/correct | `transform` | Re-invoke target skill with field overrides (resolver → `skill_invocation`) |
| Help / skills listing | `help` | Returns formatted skills index (no execution) |

Anything else returns `kind="unsupported"` with a help message listing available commands and skills.

### Architecture

```
User message
    ↓
parse_intent()    ← Sparkstation (one call) OR deterministic fallback
    ↓                (detects ref patterns: "that"/"it"/"again" → ref:last)
ParsedIntent      ← discriminated union: search_and_summarize | write_note | skill_invocation | help | unsupported
    ↓
resolve_refs()    ← binds ref:last / ref:last_<skill> to anchor values (D015)
    ↓
intent_to_plan()  ← purely deterministic, no LLM
    ↓
PlannedAction     ← SkillAction (single skill) or ChainAction (ChainSpec)
    ↓
chat policy gate  ← blocks skills whose side-effect class isn't in allowed set
    ↓
execute           ← consume_skill() or consume_chain()
    ↓
extract_anchors() ← updates session with new anchors from execution records
    ↓
AgentResponse     ← intent + plan + records + session + error
```

### Chat policy gate

`CHAT_DEFAULT_ALLOWED_EFFECTS` controls which side-effect classes the chat layer will execute. Currently: `{READ_ONLY, FILE_WRITE}`. Skills with `NETWORK` or `SECRET_READ` effects are blocked by default — callers must explicitly opt in via the `allowed_effects` parameter.

### Side-effect confirmation

- **READ_ONLY** skills execute immediately.
- **FILE_WRITE**, **NETWORK**, and **SECRET_READ** skills require explicit confirmation:
  - Single-turn mode (`kavi chat -m "..."`): returns `needs_confirmation=true` without executing. Pass `--confirmed` to pre-confirm.
  - REPL mode: prompts the user and proceeds only on "yes". On confirmation, executes the **stashed plan** via `execute_plan()` — no re-parsing, no session drift.

### Parser fallback

When Sparkstation is unavailable or returns unparseable JSON, the parser falls back to deterministic heuristics:
- `summarize <path>` → `SkillInvocationIntent(summarize_note)`
- `summarize that/it/the result` → `SkillInvocationIntent` with `ref:last` (D015)
- `write <title>\n<body>` → `WriteNoteIntent`
- `write that [to a note]` → `SkillInvocationIntent(write_note)` with `ref:last` (D015)
- `daily <content>` / `add to daily: <content>` → `SkillInvocationIntent(create_daily_note)`
- `but paragraph`/`make it bullet`/`no, paragraph` → `TransformIntent` with style override
- `try X.md instead`/`no, X.md` → `TransformIntent` with path override
- `again [paragraph]` / `do it again` → re-run with `ref:last` (D015)
- `search/find for that/it` → `SearchAndSummarizeIntent` with `ref:last` (D015)
- `search/find again` → `SearchAndSummarizeIntent` with `ref:last_search` (D015)
- `search/find <query>` → `SearchAndSummarizeIntent`
- `<skill_name> <json>` → `SkillInvocationIntent` (generic, for any registered skill)
- Anything else → `UnsupportedIntent`

### CLI

```bash
kavi chat -m "summarize notes/ml.md"     # single-turn, prints AgentResponse JSON
kavi chat -m "add to daily note: ..." --confirmed  # single-turn with pre-confirmed side effects
kavi chat                                 # interactive REPL
```

In the REPL, `search <query>` shows a compact table (rank, score, path, title). Use `search! <query>` to also include the top-result snippet. Full `AgentResponse` JSON is always printed after the table.

---

## Operational tooling

`kavi doctor [--json]` validates the local environment and prints actionable fixes. Checks:

| Check | What | Failure severity |
|-------|------|-----------------|
| Config paths | Vault dir, registry file, execution log writability | fail |
| Registry integrity | YAML parses, skills importable, hashes match (trust drift detection) | fail |
| Sparkstation | Gateway reachable (short timeout) | warn (not fatal) |
| Toolchain | Python >=3.11, uv, ruff on PATH | warn/fail |
| Log sanity | JSONL parseable, malformed line count | warn |

Implementation in `kavi.ops.doctor` — pure functions returning `CheckResult` models. Does not import forge code or mutate anything.

---

## Project layout

```
src/kavi/
├── cli.py              # Typer CLI entry point
├── config.py           # Path constants + Sparkstation config
├── agent/
│   ├── core.py         # AgentCore: handle_message orchestrator
│   ├── models.py       # ParsedIntent, PlannedAction, AgentResponse, SessionContext
│   ├── parser.py       # LLM intent parser + deterministic fallback + ref detection
│   ├── planner.py      # Deterministic intent-to-plan mapping
│   ├── resolver.py     # Reference resolver: resolve_refs, extract_anchors (D015)
│   └── skills_index.py # Registry-driven skill discoverability + formatting
├── artifacts/
│   └── writer.py       # Content-addressed artifact writer
├── consumer/
│   ├── shim.py         # Consumer runtime: load, validate, execute, audit
│   ├── chain.py        # Deterministic skill chain executor
│   ├── log.py          # Append-only JSONL execution log
│   ├── replay.py       # Execution replay (re-run with trust checks)
│   └── session.py      # Session view (execution chain tree)
├── forge/
│   ├── build.py        # Sandbox build, diff gate, Claude invocation
│   ├── invariants.py   # Structural/scope/safety invariant checks
│   ├── paths.py        # Convention-based path derivation
│   ├── promote.py      # VERIFIED → TRUSTED promotion
│   ├── propose.py      # Skill proposal creation
│   ├── research.py     # Failure classification + LLM advisory
│   └── verify.py       # 5-gate verification
├── ledger/
│   ├── db.py           # Schema, migrations (v1→v5)
│   └── models.py       # Pydantic models + DB operations
├── llm/
│   └── spark.py        # Sparkstation client (generate + embed)
├── ops/
│   └── doctor.py       # Healthcheck (kavi doctor)
├── policies/
│   └── scanner.py      # Policy scanner
└── skills/
    ├── base.py         # BaseSkill ABC
    ├── loader.py       # Registry loader + trust verification
    ├── write_note.py        # Skill (FILE_WRITE)
    ├── read_notes_by_tag.py # Skill (READ_ONLY)
    ├── summarize_note.py    # Skill (READ_ONLY, Sparkstation)
    ├── search_notes.py      # Skill (READ_ONLY, Sparkstation embeddings)
    ├── http_get_json.py     # Skill (NETWORK, SECRET_READ governance)
    └── create_daily_note.py # Skill (FILE_WRITE, append-mode daily notes)

tests/
├── test_failure_drill.py  # Failure canon (see above)
└── ...                    # See test markers above
docs/
├── ARCHITECTURE.md     # This file
└── decisions.md        # Append-only decision log (D001–D016)
```
