# [NOTE] Discovers video files in /watch using natural sorting, active-folder affinity, and stability checks.

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Optional

IGNORED_DIRS = {"_originals", "_failed"}
INCOMPLETE_SUFFIXES = {".part", ".tmp", ".crdownload", ".!qb"}
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".m4v",
    ".ts", ".m2ts", ".webm", ".wmv", ".flv",
}

_NATURAL_SPLIT_RE = re.compile(r"(\d+)")


class QueueScanner:
    # [NOTE] Scans /watch deterministically so active folders and earlier episodes always process first.

    def __init__(self, watch_dir: Path) -> None:
        self.watch_dir = Path(watch_dir)
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        (self.watch_dir / "_originals").mkdir(exist_ok=True)
        (self.watch_dir / "_failed").mkdir(exist_ok=True)

    @staticmethod
    def natural_sort_key(rel_path: Path) -> list[object]:
        # [NOTE] Splits path strings into text and integer tokens so 'Episode 2' precedes 'Episode 10'.
        parts = _NATURAL_SPLIT_RE.split(rel_path.as_posix().lower())
        return [int(token) if token.isdigit() else token for token in parts]

    def list_candidates(self, priority_top_folder: str | None = None) -> list[Path]:
        # [NOTE] Walks /watch excluding _originals/_failed; prioritizes active top-level folder if set.
        candidates: list[Path] = []

        for root, dirs, files in os.walk(self.watch_dir):
            dirs[:] = [
                d for d in dirs
                if d not in IGNORED_DIRS and not d.startswith(".")
            ]

            root_path = Path(root)
            for filename in files:
                if filename.startswith("."):
                    continue

                file_path = root_path / filename
                lower_name = filename.lower()

                if any(lower_name.endswith(suffix) for suffix in INCOMPLETE_SUFFIXES):
                    continue

                if file_path.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue

                candidates.append(file_path)

        def _sort_key(p: Path) -> tuple[int, list[object]]:
            rel = p.relative_to(self.watch_dir)
            in_priority = 0 if (priority_top_folder and len(rel.parts) > 1 and rel.parts[0] == priority_top_folder) else 1
            return (in_priority, self.natural_sort_key(rel))

        candidates.sort(key=_sort_key)
        return candidates

    def is_file_stable(self, file_path: Path, wait_seconds: float = 3.0) -> bool:
        # [NOTE] Compares file size and mtime across two checks 3 seconds apart to ensure transfers finished.
        try:
            if not file_path.is_file():
                return False
            first_stat = file_path.stat()
            if first_stat.st_size <= 0:
                return False

            time.sleep(wait_seconds)

            if not file_path.is_file():
                return False
            second_stat = file_path.stat()

            return (
                first_stat.st_size == second_stat.st_size
                and first_stat.st_mtime_ns == second_stat.st_mtime_ns
            )
        except OSError:
            return False

    def next_ready_file(self, stability_wait: float = 3.0, priority_top_folder: str | None = None) -> Optional[Path]:
        # [NOTE] Re-scans /watch and returns the highest-priority stable video file, or None if queue is empty.
        for candidate in self.list_candidates(priority_top_folder=priority_top_folder):
            if self.is_file_stable(candidate, wait_seconds=stability_wait):
                return candidate
        return None

    def cleanup_empty_folders(self) -> int:
        # [NOTE] Removes empty subdirectories bottom-up while strictly protecting _originals and _failed.
        removed_count = 0

        for root, dirs, files in os.walk(self.watch_dir, topdown=False):
            current = Path(root)
            if current == self.watch_dir:
                continue

            rel_parts = current.relative_to(self.watch_dir).parts
            if not rel_parts or rel_parts[0] in IGNORED_DIRS:
                continue

            visible_entries = [
                entry for entry in current.iterdir()
                if not entry.name.startswith(".") and entry.name != "Thumbs.db"
            ]
            if not visible_entries:
                try:
                    for hidden in current.iterdir():
                        hidden.unlink(missing_ok=True)
                    current.rmdir()
                    removed_count += 1
                except OSError:
                    pass

        return removed_count
