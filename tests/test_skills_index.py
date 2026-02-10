"""Tests for SkillsIndex — policy labeling, build_index, formatting."""

from __future__ import annotations

from kavi.agent.skills_index import (
    SkillEntry,
    build_index,
    example_invocation,
    format_entry,
    format_index,
    policy_label,
)
from kavi.consumer.shim import SkillInfo

# ── Fixtures ────────────────────────────────────────────────────────


def _info(
    name: str,
    sec: str,
    desc: str = "",
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    required_secrets: list[str] | None = None,
) -> SkillInfo:
    return SkillInfo(
        name=name,
        description=desc,
        side_effect_class=sec,
        version="1.0.0",
        source_hash="aaa",
        input_schema=input_schema or {},
        output_schema=output_schema or {},
        required_secrets=required_secrets or [],
    )


SKILLS = [
    _info("write_note", "FILE_WRITE", "Write a note"),
    _info("search_notes", "READ_ONLY", "Search notes"),
    _info("http_get_json", "NETWORK", "Fetch JSON", required_secrets=["API_KEY"]),
    _info("read_notes_by_tag", "READ_ONLY", "Read by tag"),
    _info("secret_skill", "SECRET_READ", "Uses secrets"),
]


# ── policy_label tests ──────────────────────────────────────────────


class TestPolicyLabel:
    def test_read_only_is_allowed(self) -> None:
        assert policy_label("READ_ONLY") == "allowed"

    def test_file_write_is_confirm(self) -> None:
        assert policy_label("FILE_WRITE") == "confirm"

    def test_network_is_blocked(self) -> None:
        assert policy_label("NETWORK") == "blocked"

    def test_secret_read_is_blocked(self) -> None:
        assert policy_label("SECRET_READ") == "blocked"

    def test_network_confirm_when_allowed(self) -> None:
        """NETWORK in allowed set but in CONFIRM_SIDE_EFFECTS → confirm."""
        effects = frozenset({"READ_ONLY", "FILE_WRITE", "NETWORK"})
        assert policy_label("NETWORK", effects) == "confirm"

    def test_unknown_effect_is_blocked(self) -> None:
        assert policy_label("UNKNOWN_EFFECT") == "blocked"

    def test_custom_allowed_set(self) -> None:
        effects = frozenset({"READ_ONLY"})
        assert policy_label("READ_ONLY", effects) == "allowed"
        assert policy_label("FILE_WRITE", effects) == "blocked"


# ── build_index tests ───────────────────────────────────────────────


class TestBuildIndex:
    def test_alphabetical_ordering(self) -> None:
        index = build_index(SKILLS)
        names = [e.name for e in index]
        assert names == sorted(names)

    def test_correct_policy_labels(self) -> None:
        index = build_index(SKILLS)
        by_name = {e.name: e for e in index}
        assert by_name["read_notes_by_tag"].policy == "allowed"
        assert by_name["search_notes"].policy == "allowed"
        assert by_name["write_note"].policy == "confirm"
        assert by_name["http_get_json"].policy == "blocked"
        assert by_name["secret_skill"].policy == "blocked"

    def test_preserves_metadata(self) -> None:
        index = build_index(SKILLS)
        by_name = {e.name: e for e in index}
        assert by_name["http_get_json"].required_secrets == ["API_KEY"]
        assert by_name["write_note"].description == "Write a note"

    def test_empty_skills_list(self) -> None:
        assert build_index([]) == []

    def test_custom_allowed_effects(self) -> None:
        all_effects = frozenset({"READ_ONLY", "FILE_WRITE", "NETWORK", "SECRET_READ"})
        index = build_index(SKILLS, all_effects)
        by_name = {e.name: e for e in index}
        assert by_name["http_get_json"].policy == "confirm"
        assert by_name["secret_skill"].policy == "confirm"

    def test_all_entries_are_skill_entry(self) -> None:
        index = build_index(SKILLS)
        assert all(isinstance(e, SkillEntry) for e in index)

    def test_index_length_matches_input(self) -> None:
        assert len(build_index(SKILLS)) == len(SKILLS)


# ── example_invocation tests ────────────────────────────────────────


class TestExampleInvocation:
    def test_string_field(self) -> None:
        entry = SkillEntry(
            name="foo", description="", side_effect_class="READ_ONLY",
            policy="allowed",
            input_schema={
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            },
        )
        assert example_invocation(entry) == 'foo(query="...")'

    def test_integer_field(self) -> None:
        entry = SkillEntry(
            name="bar", description="", side_effect_class="READ_ONLY",
            policy="allowed",
            input_schema={
                "required": ["count"],
                "properties": {"count": {"type": "integer"}},
            },
        )
        assert example_invocation(entry) == "bar(count=1)"

    def test_multiple_required_fields(self) -> None:
        entry = SkillEntry(
            name="baz", description="", side_effect_class="READ_ONLY",
            policy="allowed",
            input_schema={
                "required": ["a", "b"],
                "properties": {
                    "a": {"type": "string"},
                    "b": {"type": "integer"},
                },
            },
        )
        result = example_invocation(entry)
        assert result == 'baz(a="...", b=1)'

    def test_only_required_fields_shown(self) -> None:
        entry = SkillEntry(
            name="qux", description="", side_effect_class="READ_ONLY",
            policy="allowed",
            input_schema={
                "required": ["path"],
                "properties": {
                    "path": {"type": "string"},
                    "optional_field": {"type": "integer", "default": 5},
                },
            },
        )
        result = example_invocation(entry)
        assert "optional_field" not in result
        assert 'path="..."' in result

    def test_no_required_fields(self) -> None:
        entry = SkillEntry(
            name="noop", description="", side_effect_class="READ_ONLY",
            policy="allowed",
            input_schema={"properties": {"x": {"type": "string"}}},
        )
        assert example_invocation(entry) == "noop()"

    def test_enum_uses_first_value(self) -> None:
        entry = SkillEntry(
            name="e", description="", side_effect_class="READ_ONLY",
            policy="allowed",
            input_schema={
                "required": ["style"],
                "properties": {
                    "style": {"type": "string", "enum": ["bullet", "paragraph"]},
                },
            },
        )
        assert example_invocation(entry) == 'e(style="bullet")'

    def test_array_field(self) -> None:
        entry = SkillEntry(
            name="a", description="", side_effect_class="READ_ONLY",
            policy="allowed",
            input_schema={
                "required": ["hosts"],
                "properties": {
                    "hosts": {"type": "array", "items": {"type": "string"}},
                },
            },
        )
        assert example_invocation(entry) == "a(hosts=[])"

    def test_anyof_picks_non_null(self) -> None:
        entry = SkillEntry(
            name="x", description="", side_effect_class="READ_ONLY",
            policy="allowed",
            input_schema={
                "required": ["val"],
                "properties": {
                    "val": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "null"},
                        ],
                    },
                },
            },
        )
        assert example_invocation(entry) == 'x(val="...")'

    def test_empty_schema(self) -> None:
        entry = SkillEntry(
            name="empty", description="", side_effect_class="READ_ONLY",
            policy="allowed",
        )
        assert example_invocation(entry) == "empty()"


# ── format_entry tests ──────────────────────────────────────────────


class TestFormatEntry:
    def test_contains_name_and_policy_icon(self) -> None:
        entry = SkillEntry(
            name="foo", description="A skill", side_effect_class="READ_ONLY",
            policy="allowed",
        )
        result = format_entry(entry)
        assert "foo" in result
        assert "[auto]" in result
        assert "READ_ONLY" in result
        assert "A skill" in result

    def test_confirm_icon(self) -> None:
        entry = SkillEntry(
            name="bar", description="", side_effect_class="FILE_WRITE",
            policy="confirm",
        )
        assert "[confirm]" in format_entry(entry)

    def test_blocked_icon(self) -> None:
        entry = SkillEntry(
            name="baz", description="", side_effect_class="NETWORK",
            policy="blocked",
        )
        assert "[blocked]" in format_entry(entry)

    def test_secrets_shown(self) -> None:
        entry = SkillEntry(
            name="sec", description="", side_effect_class="NETWORK",
            policy="blocked", required_secrets=["API_KEY", "TOKEN"],
        )
        result = format_entry(entry)
        assert "API_KEY" in result
        assert "TOKEN" in result
        assert "Secrets:" in result

    def test_no_secrets_line_when_empty(self) -> None:
        entry = SkillEntry(
            name="nosec", description="", side_effect_class="READ_ONLY",
            policy="allowed",
        )
        assert "Secrets:" not in format_entry(entry)

    def test_example_line_present(self) -> None:
        entry = SkillEntry(
            name="x", description="", side_effect_class="READ_ONLY",
            policy="allowed",
            input_schema={
                "required": ["q"],
                "properties": {"q": {"type": "string"}},
            },
        )
        assert "Example:" in format_entry(entry)
        assert 'x(q="...")' in format_entry(entry)


# ── format_index tests ──────────────────────────────────────────────


class TestFormatIndex:
    def test_empty_list(self) -> None:
        assert format_index([]) == "No skills available."

    def test_groups_by_policy(self) -> None:
        index = build_index(SKILLS)
        output = format_index(index)
        assert "Available skills (auto-execute):" in output
        assert "Requires confirmation:" in output
        assert "Blocked (opt-in required):" in output

    def test_auto_section_contains_read_only_skills(self) -> None:
        index = build_index(SKILLS)
        output = format_index(index)
        auto_section = output.split("Requires confirmation:")[0]
        assert "read_notes_by_tag" in auto_section
        assert "search_notes" in auto_section

    def test_confirm_section_contains_file_write(self) -> None:
        index = build_index(SKILLS)
        output = format_index(index)
        parts = output.split("Blocked (opt-in required):")
        confirm_section = parts[0].split("Requires confirmation:")[1]
        assert "write_note" in confirm_section

    def test_blocked_section_contains_network(self) -> None:
        index = build_index(SKILLS)
        output = format_index(index)
        blocked_section = output.split("Blocked (opt-in required):")[1]
        assert "http_get_json" in blocked_section

    def test_omits_empty_sections(self) -> None:
        """If no skills in a group, that section header is absent."""
        read_only_only = [_info("reader", "READ_ONLY", "Reads stuff")]
        index = build_index(read_only_only)
        output = format_index(index)
        assert "Available skills (auto-execute):" in output
        assert "Requires confirmation:" not in output
        assert "Blocked (opt-in required):" not in output

    def test_stable_output_across_calls(self) -> None:
        """Same input → same output (no randomness)."""
        index = build_index(SKILLS)
        assert format_index(index) == format_index(index)
