# Universal AI Instructions

This project uses `obsmcp` as the universal continuity layer.

Always do the following first:

1. Read `.context/PROJECT_CONTEXT.md`
2. Read `.context/CURRENT_TASK.json`
3. Read `.context/HANDOFF.md`
4. Read `.context/DECISIONS.md`
5. Read `.context/BLOCKERS.json`
6. Read `.context/RELEVANT_FILES.json`

If MCP access is available, query `obsmcp` on `http://127.0.0.1:9300/mcp`.

If MCP access is not available, use `ctx.bat`.

Primary goals:

- preserve continuity across models
- do not reset the project state
- reduce repeated explanation
- log meaningful progress
- create a handoff before stopping

