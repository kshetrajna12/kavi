---
name: session-start
description: Run at the beginning of a work session to check git state, pull latest, run tests, and show available work.
---

Run this checklist in order. Report results concisely. Do not give advice or suggest next steps — that's the advisor's job.

1. **Git state**
   ```bash
   git status
   ```

2. **Pull latest**
   ```bash
   git pull --rebase
   ```

3. **Test baseline**
   ```bash
   uv run pytest -q
   ```
   If any tests fail, flag this immediately — fixing failures is the first priority.

4. **Lint check**
   ```bash
   uv run ruff check src/ tests/ 2>&1 | tail -5
   ```

5. **Recent changes**
   ```bash
   git log --oneline -10
   ```

6. **Available work**
   ```bash
   bd ready 2>/dev/null || echo "No beads issues found"
   ```

7. **In-progress work**
   ```bash
   bd list --status=in_progress 2>/dev/null || echo "None"
   ```

Report each step's result. Keep it compact — one or two lines per step. If everything is green, say so and stop.
