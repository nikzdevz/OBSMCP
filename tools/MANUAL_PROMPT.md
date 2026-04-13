# Manual Prompt For Non-MCP Tools

Use the following instruction block when a tool cannot connect to MCP and cannot run `ctx.bat`.

```text
This project uses obsmcp as a shared continuity layer.

Read these files first:
- .context/PROJECT_CONTEXT.md
- .context/CURRENT_TASK.json
- .context/HANDOFF.md
- .context/DECISIONS.md
- .context/BLOCKERS.json
- .context/RELEVANT_FILES.json
- .context/SESSION_SUMMARY.md

Continue the existing project. Do not restart discovery unless the context says it is necessary.
Preserve prior decisions and blockers.
Focus on the current task and relevant files.
Before you stop, write a concise handoff for the next model or tool.
```

