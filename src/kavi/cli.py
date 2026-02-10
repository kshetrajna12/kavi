"""Kavi CLI — entry point for all commands."""

from __future__ import annotations

import json
import sqlite3

import typer
from rich import print as rprint
from rich.table import Table

from kavi import __version__


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"kavi {__version__}")
        raise typer.Exit()


app = typer.Typer(
    name="kavi",
    help="Governed skill forge for self-building systems.",
    no_args_is_help=True,
)


def _get_conn() -> sqlite3.Connection:
    from kavi.config import LEDGER_DB
    from kavi.ledger.db import init_db

    return init_db(LEDGER_DB)


@app.callback()
def main(
    version: bool | None = typer.Option(  # noqa: N803
        None, "--version", "-V", callback=version_callback, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Kavi — governed skill forge."""


@app.command()
def status() -> None:
    """Show Kavi status and configuration."""
    from kavi.config import LEDGER_DB, REGISTRY_PATH, VAULT_OUT

    typer.echo(f"kavi {__version__}")
    typer.echo(f"  ledger:   {LEDGER_DB}")
    typer.echo(f"  registry: {REGISTRY_PATH}")
    typer.echo(f"  vault:    {VAULT_OUT}")


@app.command("propose-skill")
def propose_skill_cmd(
    name: str = typer.Option(..., help="Skill name"),
    desc: str = typer.Option(..., "--desc", help="Skill description"),
    side_effect: str = typer.Option(..., "--side-effect", help="Side effect class"),
    io_schema_json: str = typer.Option(
        ..., "--io-schema-json", help="I/O schema as JSON string"
    ),
) -> None:
    """Create a new skill proposal."""
    from kavi.config import ARTIFACTS_OUT
    from kavi.forge.propose import propose_skill
    from kavi.ledger.models import SideEffectClass

    conn = _get_conn()
    try:
        sec = SideEffectClass(side_effect)
    except ValueError:
        typer.echo(f"Invalid side-effect class: {side_effect}")
        typer.echo(f"Valid: {', '.join(e.value for e in SideEffectClass)}")
        raise typer.Exit(1)

    proposal, artifact = propose_skill(
        conn, name=name, description=desc,
        io_schema_json=io_schema_json, side_effect_class=sec,
        output_dir=ARTIFACTS_OUT,
    )
    conn.close()
    rprint(f"[green]Proposal created:[/green] {proposal.id}")
    rprint(f"  Name: {proposal.name}")
    rprint(f"  Status: {proposal.status.value}")
    rprint(f"  Spec: {artifact.path}")


@app.command("research-skill")
def research_skill_cmd(
    build_id: str = typer.Argument(help="Failed build ID to analyze"),
    hint: str | None = typer.Option(None, "--hint", help="Additional context for research"),
    advise: bool = typer.Option(True, "--advise/--no-advise", help="Include LLM advisory"),
) -> None:
    """Analyze a failed build and produce a research note (D011)."""
    from kavi.config import ARTIFACTS_OUT

    conn = _get_conn()
    from kavi.forge.research import research_skill

    try:
        analysis, artifact = research_skill(
            conn, build_id=build_id, output_dir=ARTIFACTS_OUT, user_hint=hint,
        )
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(1)

    rprint(f"[green]Research complete:[/green] {analysis.kind.value}")
    rprint(f"  Attempt: {analysis.attempt_number}")
    for fact in analysis.facts:
        rprint(f"  - {fact}")
    rprint(f"  Note: {artifact.path}")

    if advise:
        from kavi.forge.research import advise_retry
        from kavi.ledger.models import get_artifacts_for_related, get_build

        build = get_build(conn, build_id)
        if build is None:
            conn.close()
            return
        # Find the original build packet
        artifacts = get_artifacts_for_related(conn, build.proposal_id)
        original_packet = ""
        for art in artifacts:
            if art.kind.value == "BUILD_PACKET":
                from pathlib import Path

                p = Path(art.path)
                if p.exists():
                    original_packet = p.read_text(encoding="utf-8")
                    break

        if original_packet:
            from kavi.llm.spark import SparkUnavailableError

            try:
                proposed, triggers = advise_retry(
                    conn, analysis=analysis,
                    original_packet=original_packet,
                    output_dir=ARTIFACTS_OUT,
                )
                if triggers:
                    trigger_str = ", ".join(t.value for t in triggers)
                    rprint(f"\n[yellow]Escalation triggers:[/yellow] {trigger_str}")
                    rprint("[yellow]Human review required before retry.[/yellow]")
                else:
                    rprint("\n[green]LLM advisory ready.[/green] No escalation triggers.")
            except SparkUnavailableError:
                rprint("\n[yellow]Sparkstation unavailable.[/yellow]")
                rprint("Proceeding with deterministic research only.")
            except Exception as e:
                rprint(f"\n[yellow]LLM advisory failed:[/yellow] {e}")
                rprint("Proceeding with deterministic research only.")

    conn.close()
    rprint("\n[yellow]Next:[/yellow] kavi build-skill <proposal_id>")


@app.command("build-skill")
def build_skill_cmd(
    proposal_id: str = typer.Argument(help="Proposal ID to build"),
    invoke: bool = typer.Option(True, "--invoke/--no-invoke", help="Invoke Claude Code CLI"),
    timeout: int = typer.Option(600, "--timeout", help="Build timeout in seconds"),
) -> None:
    """Build a skill via Claude Code in a sandboxed workspace (D009)."""
    from pathlib import Path

    from kavi.config import ARTIFACTS_OUT, PROJECT_ROOT
    from kavi.forge.build import build_skill, invoke_claude_build

    conn = _get_conn()
    build, artifact = build_skill(
        conn, proposal_id=proposal_id, output_dir=ARTIFACTS_OUT,
    )
    rprint(f"[green]Build started:[/green] {build.id}")
    rprint(f"  Branch: {build.branch_name}")
    rprint(f"  Build packet: {artifact.path}")

    if not invoke:
        conn.close()
        rprint("\n[yellow]Next:[/yellow] Run Claude Code with the build packet, then:")
        rprint(f"  kavi verify-skill {proposal_id}")
        return

    rprint("\n[yellow]Invoking Claude Code in sandbox...[/yellow]")
    from kavi.ledger.models import get_proposal
    proposal = get_proposal(conn, proposal_id)
    assert proposal is not None  # guaranteed by build_skill succeeding

    success, sandbox_path = invoke_claude_build(
        conn,
        build=build,
        proposal_name=proposal.name,
        build_packet_path=Path(artifact.path),
        project_root=PROJECT_ROOT,
        output_dir=ARTIFACTS_OUT,
        timeout=timeout,
    )
    conn.close()

    if success:
        rprint("[green]Build succeeded![/green] Allowlisted files copied to repo.")
        rprint(f"\n[yellow]Next:[/yellow] kavi verify-skill {proposal_id}")
    else:
        rprint("[red]Build failed.[/red] Check build log in artifacts_out/")
        if sandbox_path:
            rprint(f"  Sandbox preserved at: {sandbox_path}")
        raise typer.Exit(1)



@app.command("verify-skill")
def verify_skill_cmd(
    proposal_id: str = typer.Argument(help="Proposal ID to verify"),
) -> None:
    """Run verification checks on a built skill."""
    from kavi.config import ARTIFACTS_OUT, POLICY_PATH, PROJECT_ROOT
    from kavi.forge.verify import verify_skill
    from kavi.policies.scanner import Policy

    conn = _get_conn()
    policy = Policy.from_yaml(POLICY_PATH)
    verification, artifact = verify_skill(
        conn, proposal_id=proposal_id,
        policy=policy, output_dir=ARTIFACTS_OUT,
        project_root=PROJECT_ROOT,
    )
    conn.close()

    color = "green" if verification.status.value == "PASSED" else "red"
    rprint(f"[{color}]Verification: {verification.status.value}[/{color}]")
    rprint(f"  ruff:       {'PASS' if verification.ruff_ok else 'FAIL'}")
    rprint(f"  mypy:       {'PASS' if verification.mypy_ok else 'FAIL'}")
    rprint(f"  pytest:     {'PASS' if verification.pytest_ok else 'FAIL'}")
    rprint(f"  policy:     {'PASS' if verification.policy_ok else 'FAIL'}")
    rprint(f"  invariants: {'PASS' if verification.invariant_ok else 'FAIL'}")
    rprint(f"  Report: {artifact.path}")


@app.command("check-invariants")
def check_invariants_cmd(
    proposal_id: str = typer.Argument(help="Proposal ID to check"),
) -> None:
    """Run invariant checks (structural, scope, safety) on a skill."""
    from kavi.config import PROJECT_ROOT
    from kavi.forge.invariants import check_invariants
    from kavi.forge.paths import skill_file_path
    from kavi.ledger.models import get_proposal

    conn = _get_conn()
    proposal = get_proposal(conn, proposal_id)
    conn.close()
    if proposal is None:
        typer.echo(f"Proposal '{proposal_id}' not found")
        raise typer.Exit(1)

    skill_file = skill_file_path(proposal.name, PROJECT_ROOT)
    result = check_invariants(
        skill_file,
        expected_side_effect=proposal.side_effect_class.value,
        proposal_name=proposal.name,
        project_root=PROJECT_ROOT,
    )

    color = "green" if result.ok else "red"
    rprint(f"[{color}]Invariants: {'PASS' if result.ok else 'FAIL'}[/{color}]")
    rprint(f"  structural: {'PASS' if result.structural_ok else 'FAIL'}")
    rprint(f"  scope:      {'PASS' if result.scope_ok else 'FAIL'}")
    rprint(f"  safety:     {'PASS' if result.safety_ok else 'FAIL'}")
    if result.violations:
        rprint("\n[red]Violations:[/red]")
        for v in result.violations:
            line = f" (line {v.line})" if v.line else ""
            rprint(f"  [{v.check}] {v.message}{line}")
    if not result.ok:
        raise typer.Exit(1)


@app.command("promote-skill")
def promote_skill_cmd(
    proposal_id: str = typer.Argument(help="Proposal ID to promote"),
) -> None:
    """Promote a verified skill to TRUSTED."""
    from kavi.config import PROJECT_ROOT, REGISTRY_PATH
    from kavi.forge.promote import promote_skill

    conn = _get_conn()
    promotion = promote_skill(
        conn, proposal_id=proposal_id,
        project_root=PROJECT_ROOT, registry_path=REGISTRY_PATH,
    )
    conn.close()
    rprint("[green]Skill promoted to TRUSTED[/green]")
    rprint(f"  Approved by: {promotion.approved_by}")
    rprint(f"  Registry updated: {REGISTRY_PATH}")


@app.command("list-skills")
def list_skills_cmd() -> None:
    """List all TRUSTED skills from the registry."""
    from kavi.config import REGISTRY_PATH
    from kavi.skills.loader import list_skills

    skills = list_skills(REGISTRY_PATH)
    if not skills:
        typer.echo("No trusted skills registered.")
        return

    table = Table(title="Trusted Skills")
    table.add_column("Name")
    table.add_column("Description")
    table.add_column("Side Effect")
    table.add_column("Version")
    for s in skills:
        table.add_row(
            s.get("name", ""),
            s.get("description", ""),
            s.get("side_effect_class", ""),
            s.get("version", ""),
        )
    rprint(table)


@app.command("run-skill")
def run_skill_cmd(
    skill_name: str = typer.Argument(help="Name of the skill to run"),
    input_json: str = typer.Option(..., "--json", help="Input as JSON string"),
) -> None:
    """Run a TRUSTED skill with JSON input."""
    from kavi.config import REGISTRY_PATH
    from kavi.skills.loader import load_skill

    try:
        raw_input = json.loads(input_json)
    except json.JSONDecodeError as e:
        typer.echo(f"Invalid JSON input: {e}")
        raise typer.Exit(1)

    from kavi.skills.loader import TrustError

    try:
        skill = load_skill(REGISTRY_PATH, skill_name)
    except TrustError as e:
        rprint(f"[red]Trust verification failed:[/red] {e}")
        rprint("The skill file has been modified since it was promoted.")
        rprint("Re-verify and re-promote the skill to update the trusted hash.")
        raise typer.Exit(1)

    result = skill.validate_and_run(raw_input)
    rprint(json.dumps(result, indent=2))


@app.command("consume-skill")
def consume_skill_cmd(
    skill_name: str = typer.Argument(help="Name of the trusted skill to consume"),
    input_json: str = typer.Option(..., "--json", help="Input as JSON string"),
) -> None:
    """Execute a trusted skill and emit an auditable ExecutionRecord as JSON."""
    from kavi.config import REGISTRY_PATH
    from kavi.consumer.shim import consume_skill

    try:
        raw_input = json.loads(input_json)
    except json.JSONDecodeError as e:
        typer.echo(f"Invalid JSON input: {e}")
        raise typer.Exit(1)

    record = consume_skill(REGISTRY_PATH, skill_name, raw_input)
    rprint(json.dumps(record.model_dump(), indent=2))
    if not record.success:
        raise typer.Exit(1)
