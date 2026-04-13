# Next Commit Plan

This document captures the most useful next commit after the public repo polish pass.

## Recommended next commit

Recommended primary follow-up:

`VS Code startup integration + output-token strategy surfacing`

Why this is the best next move:

- the new workflow-safety features are already implemented server-side
- the highest leverage now is getting clients to actually use them by default
- this improves the real day-one user experience more than adding another backend feature

## Track A: VS Code integration improvements

Goal:

- make the VS Code / Claude Code / Codex startup flow safer by default

Suggested work:

1. call `resolve_active_project` before continuity-sensitive reads
2. call `get_startup_preflight` on session startup
3. call `get_resume_board` when resuming or reopening a project
4. infer and pass:
   - `session_label`
   - `workstream_key`
   - `client_name`
   - `model_name`
5. default to `resume_strategy=new` when the new prompt is clearly unrelated to the prior workstream

Expected result:

- fewer accidental resumes
- clearer session naming
- better first-run trust in `obsmcp`

## Track B: Output-token strategy improvements

Goal:

- make output-token optimization easier to understand and adopt

Suggested work:

1. expose a compact "recommended output mode" helper for clients
2. add more docs/examples for:
   - `off`
   - `prompt_only`
   - `gateway_enforced`
3. surface token savings in a more visible project dashboard or fast-path response
4. add task-type presets for:
   - review
   - docs
   - debugging
   - architecture

Expected result:

- easier rollout of output-token reduction
- clearer understanding of where token savings really happen

## Recommended commit message

```text
Improve VS Code startup flow and expose output-token strategy defaults
```

## Files likely involved

- `server/service.py`
- `cli/main.py`
- `docs/USAGE.md`
- `docs/ARCHITECTURE.md`
- `config/obsmcp.json`
- IDE/client integration config files
