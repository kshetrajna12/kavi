# Kavi

Governed skill forge for self-building systems.

## Scope

This repository implements **Kavi Forge** — the governance and trust layer. It manages the full lifecycle of skills: proposing, building, verifying, promoting, and running code-generated capabilities with auditable provenance and enforced side-effect boundaries.

Higher-level layers (autonomous agents, planners, conversational interfaces) are separate concerns that would consume the forge's trusted skill registry. They are not implemented here.

## What Kavi Forge is

- A governed skill lifecycle: propose → build → verify → promote → run
- Auditable trust: every skill is content-addressed, hash-verified at runtime, and traceable through a ledger
- Bounded code generation: Claude Code generates skill code in an isolated sandbox; a diff allowlist gate constrains what it can touch; five independent verification gates run before anything is trusted

## What Kavi Forge is not

- An autonomous agent or planner
- A dialogue system or prompt framework
- An orchestration layer

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

Failed builds enter a research → retry loop: a deterministic classifier extracts failure kind and facts, and an optional LLM advisory proposes a corrected build packet. Escalation triggers require human review. Kavi includes a deterministic research → retry loop that has been exercised across all failure classes.

For implementation details, see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). For design rationale, see the [append-only decision log](docs/decisions.md).

## Installation

```bash
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
| `kavi status` | Show configuration |
| `kavi propose-skill` | Create a skill proposal |
| `kavi build-skill <proposal_id>` | Build skill in sandboxed workspace |
| `kavi verify-skill <proposal_id>` | Run all verification gates |
| `kavi check-invariants <proposal_id>` | Run invariant checks standalone |
| `kavi promote-skill <proposal_id>` | Promote to TRUSTED |
| `kavi run-skill <name> --json '{...}'` | Run a TRUSTED skill |
| `kavi consume-skill <name> --json '{...}'` | Execute a skill, emit [ExecutionRecord](docs/ARCHITECTURE.md#consumer-shim), append to log |
| `kavi consume-chain --json '{...}'` | Execute a [deterministic skill chain](docs/ARCHITECTURE.md#execution-chains) with mapped inputs |
| `kavi search-and-summarize --query '...'` | Search notes + summarize top result (convenience chain) |
| `kavi tail-executions [--n N] [--only-failures] [--skill NAME]` | Show recent execution records from JSONL log |
| `kavi list-skills` | List TRUSTED skills |
| `kavi research-skill <build_id>` | Analyze a failed build |

## Project layout

```
src/kavi/
├── cli.py              # Typer CLI entry point
├── config.py           # Path constants + Sparkstation config
├── artifacts/          # Content-addressed artifact writer
├── consumer/           # Consumer shim + chain executor: execute trusted skills with provenance
├── forge/              # Pipeline stages (propose, build, verify, promote, research)
├── ledger/             # SQLite schema, migrations, Pydantic models
├── llm/                # Sparkstation client (healthcheck, generate)
├── policies/           # Policy scanner (forbidden imports, eval/exec)
└── skills/             # BaseSkill ABC, loader + trust verification, skill implementations

docs/
├── ARCHITECTURE.md     # Internal architecture reference
└── decisions.md        # Append-only decision log (D001–D012)
```

## Development

```bash
uv run pytest -q              # Fast suite (~3s, no network)
uv run ruff check src/ tests/ # Lint
uv run mypy                   # Type check
```

## License

Private.
