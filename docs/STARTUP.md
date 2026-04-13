# Startup and Reboot Recovery

## Manual start

```bat
start_obsmcp.bat
```

If the detached launcher is blocked by local Windows policy or shell behavior, use this fallback to keep `obsmcp` alive in its own console window:

```powershell
Start-Process -FilePath cmd.exe -ArgumentList '/k','cd /d D:\Projects\obsmcp && .venv\Scripts\python.exe -m server.main'
```

## Manual stop

```bat
stop_obsmcp.bat
```

## Automatic startup on login

Default recommended method:

```bat
install_task_scheduler.bat
```

This creates a Windows Task Scheduler task named `obsmcp` that runs on logon with a short delay.

## Remove automatic startup

```bat
uninstall_task_scheduler.bat
```

## Why Task Scheduler is the default

- more reliable than the Startup folder for background processes
- easier to inspect and repair
- easy to disable without editing files
- works cleanly with a batch launcher

## Reboot recovery flow

After reboot:

1. Windows logon triggers the `obsmcp` scheduled task
2. `start_obsmcp.bat` launches the detached Python server
3. per-project workspaces under `projects/<project-slug>/` remain intact
4. project `.context`, session folders, and vault files are still available
5. any new tool can resume from the project workspace or the hub vault

## Manual recovery if startup is not enabled

```bat
start_obsmcp.bat
ctx.bat project list
```

If `start_obsmcp.bat` exits but port `9300` does not stay open, use the dedicated console-window fallback above and then verify with `curl http://127.0.0.1:9300/healthz`.

## Logs

Check:

- `logs/startup.log`
- `logs/obsmcp.log`
- `logs/obsmcp-error.log`
- `projects/<project-slug>/logs`
