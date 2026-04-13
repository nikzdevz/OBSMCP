# AGENTS.md

You are operating in a shared multi-model continuity system backed by `obsmcp`.

Before doing substantive work, read in this order:

1. `.context/PROJECT_CONTEXT.md`
2. `.context/CURRENT_TASK.json`
3. `.context/HANDOFF.md`
4. `.context/DECISIONS.md`
5. `.context/BLOCKERS.json`
6. `.context/RELEVANT_FILES.json`
7. `.context/SESSION_SUMMARY.md`
8. Check if a **Code Atlas** exists for this project. Call `get_code_atlas_status()` via MCP or run `ctx.bat atlas status` from the shell. If the atlas does not exist, call `scan_codebase()`. For large or first-time scans, `scan_codebase()` may return a background job instead of a finished atlas; poll `get_scan_job()` or `wait_for_scan_job()` until it completes. The Code Atlas documents every file, function, class, feature, and cross-reference in the project — it gives you a complete structural understanding without reading every source file.
9. For quick orientation: call `get_audit_log(limit=10)` to see recent activity.
10. For sprint planning: use `bulk_task_ops` to batch-create or batch-update tasks.
11. For token-efficient context: use `generate_compact_context_v2(max_tokens=3000)` — includes decision chains, dependency map, session info, and recommended semantic lookups.
12. For low-latency startup or resumed work, use the cached tiered context and delta tools first:
   - `generate_context_profile(profile="fast"|"balanced"|"deep"|"handoff"|"recovery")`
   - `generate_delta_context(...)`
13. When you need targeted understanding instead of rereading large files, use semantic tools first:
   - `describe_module(module_path)`
   - `describe_symbol(symbol_name, module_path?)`
   - `describe_feature(feature_name)`
   - `search_code_knowledge(query)`
   - `get_symbol_candidates(symbol_name)` if a symbol name is ambiguous
   - `get_related_symbols(entity_key)` to expand from one symbol to its neighbors
14. For dependency overview: use `get_blocked_tasks()` and `validate_dependencies()` to check task readiness.

Rules:

- Do not assume you are the first or only agent.
- Continue the current task instead of restarting discovery.
- Preserve continuity notes, blockers, and decisions.
- When you finish a meaningful chunk, log work to `obsmcp`.
- Before you stop, create a handoff for the next model or tool.

Preferred write paths:

- MCP tools if available
- `ctx.bat` if MCP is not available
