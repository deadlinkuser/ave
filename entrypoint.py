# [NOTE] Container entrypoint initializing data folders, resolving port conflicts, and starting Uvicorn.

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from ave.web import find_available_port


def main() -> None:
    data_dir = Path(os.environ.get("AVE_DATA_DIR", "/data"))
    for sub in ("watch", "output", "config"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    preferred_port = int(os.environ.get("AVE_PORT", "8488"))
    bind_host = os.environ.get("AVE_HOST", "0.0.0.0")
    active_port = find_available_port(preferred_port)

    if active_port != preferred_port:
        print(f"[AVE] Port {preferred_port} in use; automatically switched to port {active_port}.")

    print(f"[AVE] Ready in Idle state on http://127.0.0.1:{active_port}")
    uvicorn.run("ave.web:app", host=bind_host, port=active_port, log_level="warning")


if __name__ == "__main__":
    main()
