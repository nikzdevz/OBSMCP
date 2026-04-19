"""First-run configuration CLI invoked by ``start.bat`` / ``start.sh``.

Accepts flags so the batch launcher can pass the prompted values
non-interactively::

    python -m obsmcp.obsmcp_setup --configure \
        --project "D:\\Projects\\MyProject" \
        --url "" --token ""

When ``--configure`` is omitted, runs an interactive prompt.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import config_path, configure, load_config


def _prompt(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def interactive() -> int:
    print("=" * 48)
    print("  OBSMCP First-Run Setup")
    print("=" * 48)
    print()
    current = load_config()
    project = _prompt("Enter project path", current.project_path)
    if not project:
        print("Project path is required.", file=sys.stderr)
        return 1
    if not Path(project).expanduser().exists():
        print(f"Warning: {project} does not exist yet — will be created on first scan.")
    print()
    print("Optional: cloud sync configuration (leave blank for standalone mode).")
    backend = _prompt("Backend URL", current.backend_url)
    token = _prompt("API token", current.api_token) if backend else ""
    cfg = configure(project, backend, token)
    print()
    print(f"Configuration saved to {config_path()}")
    print(f"Mode: {cfg.mode.upper()}")
    print(f"Agent ID: {cfg.agent_id}")
    if cfg.mode == "standalone":
        print(f"Local dashboard will start at http://localhost:{cfg.local_ui_port}")
    else:
        print(f"Syncing to {cfg.backend_url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OBSMCP setup")
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--project", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--token", default="")
    args = parser.parse_args(argv)

    if args.configure:
        if not args.project:
            print("--project is required with --configure", file=sys.stderr)
            return 1
        cfg = configure(args.project, args.url, args.token)
        print(f"Configuration saved to {config_path()}")
        print(f"Mode: {cfg.mode.upper()}")
        return 0
    return interactive()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
