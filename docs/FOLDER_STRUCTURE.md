# Folder Structure

Recommended production install path: `C:\obsmcp`

This repository is portable, but `C:\obsmcp` is the cleanest Windows deployment path because the batch scripts, scheduled task, and mental model stay simple.

## Layout

```text
C:\obsmcp\
  server\            Python MCP server, state layer, sync engine
  cli\               ctx CLI implementation
  scripts\           operational helpers for launch, stop, backup
  config\            JSON config and example integration snippets
  logs\              rotating global server logs and startup logs
  docs\              install, usage, architecture, testing, troubleshooting docs
  templates\
    obsidian\        note templates and examples
    context\         context and prompt templates
  registry\
    projects.json    global registry of known projects
  hub\
    vault\           central Obsidian hub vault for all projects
  projects\
    <project-slug>\
      project.json   per-project manifest
      data\
        db\          per-project SQLite database
        json\        per-project exported snapshots
        backups\     per-project backups
        exports\     per-project export bundles
      .context\      per-project continuity files
      vault\         per-project Obsidian vault
      sessions\      per-session folders with metadata, worklog, and handoff files
      logs\          per-project logs
  tests\             local automated verification
  tools\             universal instructions and helper assets
```

## Folder purpose

- `server`: the production code that owns state, sync, routing, recovery, and MCP handling
- `cli`: the `ctx` command surface for shells and non-MCP tools
- `scripts`: Windows operational actions such as detached launch and backup
- `config`: editable settings without touching code
- `logs`: global server, error, and startup logs
- `docs`: the operator handbook
- `templates`: reusable note, prompt, and continuity templates
- `registry/projects.json`: global list of registered repos and their centralized workspaces
- `hub/vault`: top-level dashboard vault for all projects
- `projects/<project-slug>/data/db`: source-of-truth SQLite file for that project
- `projects/<project-slug>/data/json`: per-project snapshots for debugging or external integrations
- `projects/<project-slug>/data/backups`: copy-based backup targets for that project
- `projects/<project-slug>/data/exports`: markdown/json export bundles
- `projects/<project-slug>/.context`: the minimum viable continuity package every tool should read first
- `projects/<project-slug>/vault`: the human-facing project brain in Markdown
- `projects/<project-slug>/sessions`: durable session folders for recovery and handoff
- `projects/<project-slug>/logs`: project-local logs
- `tests`: lightweight regression coverage for state, sync, registry, and recovery behavior
- `tools`: copy-paste onboarding instructions for AI assistants
