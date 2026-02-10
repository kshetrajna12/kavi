# Kavi

Governed skill forge for self-building systems.

Kavi is a pipeline that proposes, builds, verifies, promotes, and runs **skills** — small, governed units of capability with declared side effects, validated I/O schemas, and auditable provenance. Code generation is delegated to Claude Code in a sandboxed workspace; Kavi owns governance.

## How it works

```
propose  →  build  →  verify  →  promote  →  run
   │           │         │          │          │
 SPEC      BUILD     ruff/mypy   registry   hash-verified
 artifact  PACKET    pytest      + hash     execution
           sandbox   policy      TRUSTED
           + gate    invariants
```

1. **Propose** — declare a skill (name, I/O schema, side-effect class). Writes a SKILL_SPEC artifact.
2. **Build** — generate code via Claude Code in an isolated sandbox. A diff allowlist gate ensures only the skill file and its test are modified.
3. **Verify** — run ruff, mypy, pytest, policy scanner, and invariant checks independently of Claude.
4. **Promote** — elevate to TRUSTED; hash the source file and record it in the registry.
5. **Run** — load the skill at runtime, re-verify the hash, execute with validated input/output.

Failed builds enter a **research → retry** loop (D011): a deterministic classifier extracts failure kind and facts from logs, and an optional LLM advisory (via Sparkstation) proposes a corrected build packet. Escalation triggers (repeated failures, permission widening, security-class issues) require human review.

## Installation

```bash
# Clone and install
git clone https://github.com/kshetrajna12/kavi.git
cd kavi
uv sync
```

Requires Python 3.11+. Uses [uv](https://docs.astral.sh/uv/) for dependency management.

## Quick start

```bash
# 1. Propose a skill
kavi propose-skill \
  --name write_note \
  --desc "Write a markdown note to the vault" \
  --side-effect FILE_WRITE \
  --io-schema-json '{"input": {"title": "str", "body": "str"}, "output": {"path": "str"}}'

# 2. Build it (invokes Claude Code in a sandbox)
kavi build-skill <proposal_id>

# 3. Verify (ruff + mypy + pytest + policy + invariants)
kavi verify-skill <proposal_id>

# 4. Promote to TRUSTED
kavi promote-skill <proposal_id>

# 5. Run it
kavi run-skill write_note --json '{"title": "Hello", "body": "World"}'
```

## CLI commands

| Command | Description |
|---------|-------------|
| `kavi status` | Show configuration (ledger path, registry, vault) |
| `kavi propose-skill` | Create a skill proposal with I/O schema and side-effect class |
| `kavi build-skill <proposal_id>` | Build skill in sandboxed workspace via Claude Code |
| `kavi verify-skill <proposal_id>` | Run all verification gates (ruff, mypy, pytest, policy, invariants) |
| `kavi check-invariants <proposal_id>` | Run structural/scope/safety invariant checks standalone |
| `kavi promote-skill <proposal_id>` | Promote verified skill to TRUSTED (hash stored in registry) |
| `kavi run-skill <name> --json '{...}'` | Run a TRUSTED skill with JSON input |
| `kavi list-skills` | List all TRUSTED skills from the registry |
| `kavi research-skill <build_id>` | Analyze a failed build (deterministic + optional LLM advisory) |

### research-skill options

```bash
kavi research-skill <build_id> [--hint "context"] [--no-advise]
```

- `--hint` — additional context for the research note
- `--no-advise` — skip LLM advisory, use deterministic classification only
- LLM advisory uses the local Sparkstation gateway; degrades gracefully if unavailable

## Architecture

### Ledger (SQLite, schema v4)

The ledger is the single source of truth (D002). Tables:

- **skill_proposals** — name, description, I/O schema, side-effect class, status progression (PROPOSED → BUILT → VERIFIED → TRUSTED)
- **builds** — per-attempt records with attempt lineage (`attempt_number`, `parent_build_id`)
- **verifications** — per-gate pass/fail (ruff, mypy, pytest, policy, invariants)
- **promotions** — audit trail of who approved what
- **artifacts** — content-addressed (SHA256) references to specs, build packets, logs, research notes

### Sandbox build (D009)

Builds run in `/tmp/kavi-build/<build_id>/`:
- Working tree copied (secrets stripped, git remotes removed)
- Claude Code invoked headlessly with `--allowedTools [Edit, Write, Glob, Grep, Read]`
- Diff allowlist gate: only `src/kavi/skills/{name}.py` and `tests/test_skill_{name}.py` may change
- Allowlisted files safe-copied back to canonical repo (rejects symlinks, path traversal)

### Research and retry (D011)

Two-layer failure analysis:
1. **Deterministic classifier** — extracts `FailureKind` (GATE_VIOLATION, TIMEOUT, BUILD_ERROR, VERIFY_LINT, VERIFY_TEST, VERIFY_POLICY, VERIFY_INVARIANT, UNKNOWN) and structured facts from logs
2. **LLM advisory** (optional) — Sparkstation proposes a corrected BUILD_PACKET; bounded input, timeout, graceful degradation if gateway is down

### Sparkstation integration

Local LLM gateway at `http://localhost:8000/v1` (OpenAI-compatible). Used for retry advisory via `kavi.llm.spark`:

- `is_available()` — healthcheck (model list)
- `generate()` — chat completion with timeout, prompt truncation, typed errors
- Falls back to deterministic-only if Sparkstation is unreachable

### Trust chain (D010)

```
propose (spec) → build (sandbox) → verify (5 gates) → promote (hash stored) → run (hash re-verified)
```

At runtime, `load_skill()` re-hashes the source file and compares against the registry. Mismatch → `TrustError`, skill won't execute.

## Project layout

```
src/kavi/
├── cli.py              # Typer CLI entry point
├── config.py           # Path constants + Sparkstation config
├── __init__.py
├── artifacts/
│   └── writer.py       # Content-addressed artifact writer
├── forge/
│   ├── build.py        # Sandbox build, diff gate, Claude invocation
│   ├── invariants.py   # Structural/scope/safety invariant checks
│   ├── paths.py        # Convention-based path derivation (D006)
│   ├── promote.py      # VERIFIED → TRUSTED promotion
│   ├── propose.py      # Skill proposal creation
│   ├── research.py     # Failure classification + LLM advisory (D011)
│   └── verify.py       # 5-gate verification (ruff/mypy/pytest/policy/invariants)
├── ledger/
│   ├── db.py           # Schema, migrations (v1→v4)
│   └── models.py       # Pydantic models + DB operations
├── llm/
│   └── spark.py        # Sparkstation client (healthcheck, generate)
├── policies/
│   └── scanner.py      # Policy scanner (forbidden imports, eval/exec)
└── skills/
    ├── base.py         # BaseSkill ABC + SkillInput/SkillOutput
    ├── loader.py       # Registry loader, trust verification, skill import
    └── write_note.py   # Example skill (FILE_WRITE)

tests/
├── test_artifacts.py
├── test_build_invoke.py
├── test_forge_flow.py
├── test_forge_paths.py
├── test_invariants.py
├── test_ledger.py
├── test_policy_scanner.py
├── test_skill_write_note.py
├── test_skills_loader.py
├── test_spark_client.py    # Spark client unit tests (mocked)
└── test_spark_live.py      # Live Sparkstation tests (@pytest.mark.spark)

docs/
└── decisions.md        # Append-only decision log (D001–D011)
```

## Development

```bash
# Run tests (fast suite, ~3s)
uv run pytest -q

# Run with live Sparkstation tests
uv run pytest -m spark

# Run integration tests (real subprocesses)
uv run pytest -m slow

# Lint
uv run ruff check src/ tests/

# Type check
uv run mypy
```

### Test markers

| Marker | Description | Default |
|--------|-------------|---------|
| (none) | Unit tests, mocked, fast | Included |
| `slow` | Integration tests with real subprocesses | Excluded |
| `spark` | Tests requiring live Sparkstation gateway | Excluded |

## Design decisions

All architectural decisions are recorded in [`docs/decisions.md`](docs/decisions.md):

| ID | Decision |
|----|----------|
| D001 | Claude Code as external build backend |
| D002 | Ledger (SQLite) is the single source of truth |
| D003 | Static enforcement only in MVP |
| D004 | Skill progression — simple first |
| D005 | Clean break from kavi-prototype |
| D006 | Convention-based skill path derivation |
| D007 | Invariant checker as separate governance gate |
| D008 | ~~Auto-detect build result~~ (superseded by D009) |
| D009 | Sandboxed build with diff allowlist |
| D010 | Runtime trust enforcement via hash verification |
| D011 | Iteration/retry with two-layer research |

## License

Private.
