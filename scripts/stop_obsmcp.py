from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.config import load_config


def main() -> None:
    config = load_config()
    pid_path = config.pid_file
    if not pid_path.exists():
        print("No obsmcp pid file found.")
        return

    pid_value = pid_path.read_text(encoding="utf-8").strip()
    if not pid_value.isdigit():
        print("PID file is invalid. Remove it manually if needed.")
        return

    subprocess.run(["taskkill", "/PID", pid_value, "/T", "/F"], check=False)
    try:
        pid_path.unlink()
    except OSError:
        pass
    print(f"Stopped obsmcp process {pid_value}")


if __name__ == "__main__":
    main()
