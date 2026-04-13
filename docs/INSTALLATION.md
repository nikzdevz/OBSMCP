# Installation Guide

## Recommended install location

Place the repository at:

```text
C:\obsmcp
```

This keeps batch scripts, Task Scheduler paths, and human troubleshooting straightforward.

## Prerequisites

- Windows
- Python `3.11+`
- PowerShell or Command Prompt
- Obsidian installed locally if you want the live vault workflow

## Install steps

From the project root:

```bat
cd /d C:\obsmcp
bootstrap_obsmcp.bat
```

This will:

- create `.venv`
- upgrade `pip`
- install `fastapi` and `uvicorn`

## Start locally

```bat
start_obsmcp.bat
```

This launches `obsmcp` in the background on:

```text
http://127.0.0.1:9300
```

## Stop locally

```bat
stop_obsmcp.bat
```

## Verify local health

```bat
curl http://127.0.0.1:9300/healthz
netstat -ano | findstr :9300
ctx.bat project list
```

## Register a project

The server is installed once, but each repo gets its own centralized workspace under `projects/<project-slug>/`.

Register a repo:

```bat
ctx.bat project register --repo D:\Work\MyApp --name "My App"
```

Inspect the workspace:

```bat
ctx.bat project paths --repo D:\Work\MyApp
```

If the repo already has older repo-local `.context` or `obsidian\vault` content, migrate it:

```bat
ctx.bat project migrate --repo D:\Work\MyApp
```

## Optional security hardening

By default, `obsmcp` binds to `127.0.0.1`, which is the safest local-first default. If you want an extra local token requirement for HTTP requests, set:

```bat
set OBSMCP_API_TOKEN=your-local-token
```

Then pass `Authorization: Bearer your-local-token` to non-health HTTP requests.
