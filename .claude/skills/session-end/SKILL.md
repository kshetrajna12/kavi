---
name: session-end
description: Run at the end of a work session. Runs quality gates, syncs beads, commits, pushes, and verifies. Work is NOT done until push succeeds.
---

Run this checklist in order. Do NOT skip steps. Do NOT stop before push succeeds.

1. **Check for uncommitted changes**
   ```bash
   git status
   ```
   If working tree is clean and up to date with origin, report "Nothing to do" and stop.

2. **Quality gates** (only if code changed)
   ```bash
   uv run ruff check --fix src/ tests/
   uv run pytest -q
   ```
   If tests fail, STOP. Report the failures. Do not commit broken code.

3. **Check for debug prints or stray TODOs**
   Search for `print(` in src/ (excluding known logging). Search for `TODO` without context. Flag anything suspicious but do not auto-remove.

4. **Stage and commit**
   Review what changed. Prefer small commits that preserve bisectability — one commit per coherent change. Do NOT include "Co-Authored-By" or "Generated with Claude Code" lines.

5. **Sync beads**
   ```bash
   bd sync 2>/dev/null || echo "No beads changes"
   ```

6. **Push**
   ```bash
   git pull --rebase && git push
   ```
   If push fails, resolve and retry. Do NOT stop until push succeeds.

7. **Verify**
   ```bash
   git status
   ```
   Must show "up to date with origin". If not, something went wrong — diagnose.

8. **Documentation check**
   If any of these changed, flag which docs need updating:
   - CLI commands → README.md
   - Database schema → ARCHITECTURE.md + decisions.md
   - New modules → README.md + ARCHITECTURE.md
   - Design decisions → decisions.md

9. **File issues for remaining work**
   If there's unfinished work, create beads issues before ending.

10. **Hand-off prompt**
    Generate a continuation prompt for the next session:
    ```
    **Completed this session:**
    - [what was done]

    **Next:**
    - [what to do next]

    Key files: [relevant files]
    ```

Report each step. The session is complete ONLY when step 7 shows clean and pushed.
