# CLAUDE.md

This workspace uses `obsmcp` as the shared continuity layer.

Start here:

1. Read `.context/PROJECT_CONTEXT.md`
2. Read `.context/CURRENT_TASK.json`
3. Read `.context/HANDOFF.md`
4. Read `.context/DECISIONS.md`
5. Read `.context/BLOCKERS.json`

Operating rules:

- Continue the existing project state instead of re-deriving it.
- Treat `.context` as the minimum required continuity package.
- Use `ctx.bat` to log work, create handoffs, and sync files when direct MCP access is missing.
- Record decisions and blockers explicitly.
- Leave a new handoff before ending your turn.

