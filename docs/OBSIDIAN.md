# Obsidian Integration

## Integration mode

The safe default is filesystem-based vault integration. `obsmcp` does not require the Obsidian Local REST API.

Why this is the default:

- fewer moving parts
- no plugin dependency for the core path
- safe local writes using generated machine-owned files
- easier recovery and debugging

## Centralized vault model

`obsmcp` uses two vault layers:

- project vaults under `projects/<project-slug>/vault`
- a central hub vault under `hub/vault`

Project vaults are the canonical note home for each repo. The hub vault is a dashboard used to monitor all registered projects without mixing their detailed notes together.

## What lives in structured state vs Obsidian

### Structured state in SQLite

- current task pointer
- tasks and task metadata
- blockers
- decisions
- work logs
- handoffs
- session summaries
- daily entries
- agent activity

### Obsidian vault

- generated project brief
- generated current task note
- generated status snapshot
- generated latest handoff note
- generated decision index
- generated ADR notes
- generated latest session summary
- appended daily note entries
- generated architecture map
- generated module summaries
- generated feature map
- generated symbol knowledge notes for cached semantic descriptions
- human-created research, debug, SOP, and architecture notes

## Safe write pattern

`obsmcp` only fully rewrites machine-owned generated notes:

- `Projects/Project Brief.md`
- `Projects/Current Task.md`
- `Projects/Status Snapshot.md`
- `Handoffs/Latest Handoff.md`
- `Decisions/Decision Log.md`
- generated `Decisions/ADR-xxxx.md`
- `Sessions/Latest Session Summary.md`
- `Research/Architecture Map.md`
- `Research/Module Summaries.md`
- `Research/Feature Map.md`
- generated `Research/Symbol Knowledge/*.md`

It does not need to overwrite user-authored research or debug notes.

## Project vault structure

```text
projects/<project-slug>/vault/
  Projects/
  Handoffs/
  Decisions/
  Daily/
  Research/
    Symbol Knowledge/
  Debug/
  Sessions/
```

## Hub vault structure

```text
hub/vault/
  Projects Overview.md
  Active Projects.md
```

## Daily note strategy

`ctx.bat --project D:\Work\MyApp note "message"` writes an entry to structured state and syncs it into `projects/<project-slug>/vault/Daily/YYYY-MM-DD.md`.

## Optional future extension

If you later want deeper Obsidian automation, you can add the Local REST API as an optional integration layer, but it should stay optional. The filesystem path is the safer baseline.
