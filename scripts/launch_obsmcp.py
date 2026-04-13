from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.config import load_config
from server.utils import is_port_open


DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def main() -> None:
    root = ROOT
    config = load_config()
    config.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = config.log_dir / "startup.log"

    if is_port_open(config.host, config.port):
        print(f"obsmcp already appears to be listening on {config.host}:{config.port}")
        return

    python_exe = root / ".venv" / "Scripts" / "python.exe"
    executable_path = (
        python_exe
        if python_exe.exists()
        else Path(sys.executable)
    )
    command = [str(executable_path), "-u", "-m", "server.main"]

    with log_path.open("a", encoding="utf-8") as log_handle:
        log_handle.write(f"Launching obsmcp via {' '.join(command)}\n")
        log_handle.flush()

    process = subprocess.Popen(
        command,
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        env={**os.environ, "OBSMCP_NO_CONSOLE": "1"},
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
    )
    config.pid_file.write_text(f"{process.pid}\n", encoding="utf-8")

    for _ in range(10):
        time.sleep(0.5)
        if is_port_open(config.host, config.port):
            break
    if is_port_open(config.host, config.port):
        print(f"obsmcp started on http://{config.host}:{config.port}")
    else:
        print(f"obsmcp launch requested, but port {config.port} did not open yet. Check {log_path}")


if __name__ == "__main__":
    main()
