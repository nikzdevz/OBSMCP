from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.config import load_config


def main() -> None:
    config = load_config()
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    backup_path = config.backup_dir / f"obsmcp-{timestamp}.sqlite3"
    shutil.copy2(config.database_path, backup_path)
    print(f"Created backup at {backup_path}")


if __name__ == "__main__":
    main()
