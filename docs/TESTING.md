# Testing Guide

## Automated tests

Run:

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Manual health test

```bat
curl http://127.0.0.1:9300/healthz
```

Expected:

- JSON response
- `port` is `9300`
- `db_exists` is `true`

## Project registration test

```bat
ctx.bat project register --repo D:\Work\MyApp --name "My App"
ctx.bat project paths --repo D:\Work\MyApp
ctx.bat hub sync
```

Expected:

- a workspace appears under `projects/<project-slug>/`
- a repo bridge file appears at `D:\Work\MyApp\.obsmcp-link.json`
- the hub vault is refreshed

## CLI test

```bat
ctx.bat --project D:\Work\MyApp task create "Test continuity" --description "Verify ctx write path"
ctx.bat --project D:\Work\MyApp session open --actor tester --client cli --model local --project-path D:\Work\MyApp --initial-request "Verify continuity" --goal "Exercise session tracking"
ctx.bat --project D:\Work\MyApp status
ctx.bat --project D:\Work\MyApp current
ctx.bat --project D:\Work\MyApp audit
ctx.bat --project D:\Work\MyApp resume
```

## Obsidian sync test

1. Run `ctx.bat --project D:\Work\MyApp note "Testing daily note sync"`
2. Open `projects/<project-slug>/vault/Daily/<today>.md`
3. Confirm the entry appears

## Semantic knowledge test

```bat
ctx.bat --project D:\Work\MyApp atlas generate
ctx.bat --project D:\Work\MyApp describe module server\service.py
ctx.bat --project D:\Work\MyApp describe symbol generate_resume_packet --module server\service.py --type function
ctx.bat --project D:\Work\MyApp knowledge search "resume packet"
```

Expected:

- semantic descriptions are returned
- `projects/<project-slug>/vault/Research/Architecture Map.md` exists
- `projects/<project-slug>/vault/Research/Module Summaries.md` exists
- `projects/<project-slug>/vault/Research/Feature Map.md` exists
- `projects/<project-slug>/vault/Research/Symbol Knowledge/` contains generated notes

## Tiered context and delta test

```bat
ctx.bat --project D:\Work\MyApp compact --profile fast --max-tokens 1200
ctx.bat --project D:\Work\MyApp compact --profile balanced --max-tokens 2500
ctx.bat --project D:\Work\MyApp compact --profile deep --max-tokens 4500
ctx.bat --project D:\Work\MyApp delta
```

Expected:

- the compact commands return progressively richer context variants
- repeated calls on unchanged state should return quickly from cache
- `delta` shows only changes since the latest handoff/session reference
- `projects/<project-slug>/.context/HOT_CONTEXT.md` exists
- `projects/<project-slug>/.context/BALANCED_CONTEXT.md` exists
- `projects/<project-slug>/.context/DEEP_CONTEXT.md` exists
- `projects/<project-slug>/.context/DELTA_CONTEXT.md` exists

## Background scan job test

```bat
ctx.bat --project D:\Work\MyApp atlas generate --background
ctx.bat --project D:\Work\MyApp atlas jobs
ctx.bat --project D:\Work\MyApp atlas wait SCAN-REPLACE-ME --wait-seconds 60
```

Expected:

- the first command returns a `SCAN-...` job ID quickly
- `atlas jobs` shows the job as `queued`, `running`, or `completed`
- `atlas wait` eventually returns `completed`
- after completion, `ctx.bat --project D:\Work\MyApp atlas status` shows the updated atlas metadata

## Reboot persistence test

1. Run `install_task_scheduler.bat`
2. Reboot or sign out and back in
3. Run `curl http://127.0.0.1:9300/healthz`
4. Confirm the server is up without manual start

## Multi-tool continuity test

1. Register the repo and inspect the workspace paths
2. Create a task and set it current
3. Open a session
4. Log work and create a handoff
5. Close the session
6. Open another tool that can read files
7. Confirm it can continue from `projects/<project-slug>/.context`

## Cross-model handoff test

1. In tool A, open a session and create a task
2. In tool A, log work and run `ctx.bat --project D:\Work\MyApp handoff ...`
3. In tool A, close the session
4. In tool B, read `projects/<project-slug>/.context/HANDOFF.md` and `projects/<project-slug>/.context/CURRENT_TASK.json`
5. Continue work without re-explaining the project
6. In tool B, append more work, create the next handoff, and close the session

## Interrupted-session recovery test

1. Open a session and log at least one work entry
2. Do not close the session cleanly
3. Run `ctx.bat --project D:\Work\MyApp recover --session SESSION-REPLACE-ME --actor claude-recovery`
4. Confirm an emergency handoff and resume packet are written into the project workspace
