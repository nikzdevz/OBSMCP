# Troubleshooting Guide

## Port `9300` already in use

Check:

```bat
netstat -ano | findstr :9300
```

If another process owns the port, stop it or change the port only if you are willing to break the fixed `obsmcp` requirement. The safe default is to keep `9300` reserved for `obsmcp`.

## Obsidian not running

That is fine. The filesystem vault still syncs. Open Obsidian later and point it at either:

- `projects/<project-slug>/vault` for a single project
- `hub/vault` for the central dashboard

## Obsidian Local REST API not available

That is also fine. `obsmcp` does not depend on it.

## Startup task fails

Check:

- Task Scheduler history for the `obsmcp` task
- `logs/startup.log`
- whether the install path moved after the task was created

Reinstall with:

```bat
uninstall_task_scheduler.bat
install_task_scheduler.bat
```

## Batch file path issues

Keep the project in a stable location, preferably `C:\obsmcp`. If you move it, reinstall the Task Scheduler task.

## Permission issues

Make sure your user can write to:

- `projects\<project-slug>\data\db`
- `logs`
- `projects\<project-slug>\.context`
- `projects\<project-slug>\vault`
- `hub\vault`

## Sync issues or stale `.context` files

Force a sync:

```bat
ctx.bat --project D:\Work\MyApp sync
```

If files still look stale, inspect:

- `projects/<project-slug>/data/db/obsmcp.sqlite3`
- `logs/obsmcp-error.log`

## Corrupted local DB

Recovery approach:

1. Stop `obsmcp`
2. Copy the newest file from `projects/<project-slug>/data/backups`
3. Replace `projects/<project-slug>/data/db/obsmcp.sqlite3`
4. Start `obsmcp`
5. Run `ctx.bat --project D:\Work\MyApp sync`

## Resume or handoff is missing after an interrupted session

Use the recovery flow:

```bat
ctx.bat --project D:\Work\MyApp audit
ctx.bat --project D:\Work\MyApp resume
ctx.bat --project D:\Work\MyApp recover --session SESSION-REPLACE-ME --actor claude-recovery
```

This generates a best-effort emergency handoff and a fresh resume packet from the persisted state.

## Server starts but health check fails

Inspect:

- `logs/startup.log`
- `logs/obsmcp.log`
- `logs/obsmcp-error.log`

Then run:

```bat
stop_obsmcp.bat
start_obsmcp.bat
```

If the hidden or detached launcher still does not keep the server alive, start `obsmcp` in its own console window:

```powershell
Start-Process -FilePath cmd.exe -ArgumentList '/k','cd /d D:\Projects\obsmcp && .venv\Scripts\python.exe -m server.main'
```

That path is less elegant, but it is a reliable Windows fallback because the server stays attached to its own process window instead of the calling shell.
