# [NOTE] Background worker thread orchestrating scanner -> analyzer -> runner -> cooldown -> summary reports.

from __future__ import annotations

import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from .analyzer import MediaAnalyzer, ProbeError
from .config import ConfigManager
from .models import EngineState, Preset
from .presets import PresetManager
from .runner import EncodeRunner
from .scanner import QueueScanner


class BatchWorker:
    # [NOTE] Manages the lifecycle of batch transcoding jobs in a background thread for FastAPI.

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.watch_dir = self.base_dir / "watch"
        self.output_dir = self.base_dir / "output"
        self.config_dir = self.base_dir / "config"

        self.config_mgr = ConfigManager(self.config_dir)
        self.preset_mgr = PresetManager(self.config_dir)
        self.scanner = QueueScanner(self.watch_dir)
        self.analyzer = MediaAnalyzer(self.output_dir)
        self.runner = EncodeRunner(self.watch_dir, self.output_dir, self.config_dir)

        self.preset_mgr.load_all()
        if self.preset_mgr.startup_notice:
            self.runner.status.banner_message = self.preset_mgr.startup_notice

        self._wake_event = threading.Event()
        self._shutdown_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._batch_saved_bytes: dict[Path, list[tuple[str, int, int]]] = {}
        self.active_top_folder: str | None = None

    def start_background_loop(self) -> None:
        # [NOTE] Spawns the daemon worker thread in IDLE state on container boot (or restarts if dead).
        if self._thread and self._thread.is_alive():
            return
        self._shutdown_event.clear()
        self._thread = threading.Thread(target=self._loop, name="ave-batch-worker", daemon=True)
        self._thread.start()

    def trigger_start(self, preset_id: str | None = None) -> None:
        # [NOTE] Resets stop flags, ensures worker thread is alive, and starts a fresh batch cleanly.
        self.start_background_loop()
        cfg = self.config_mgr.load()
        chosen_id = preset_id or cfg.default_preset_id
        self._batch_saved_bytes.clear()
        self.active_top_folder = None
        self.runner.start_new_batch(active_preset_id=chosen_id)
        self._wake_event.set()

    def request_stop(self, hard: bool = False) -> None:
        # [NOTE] Delegates Stop After Current File (hard=False) or Hard Stop (hard=True) to EncodeRunner.
        self.runner.request_stop(hard=hard)

    def clear_originals(self) -> tuple[int, int]:
        # [NOTE] Deletes archived source files in /watch/_originals; returns (files_deleted, bytes_reclaimed).
        originals_dir = self.watch_dir / "_originals"
        deleted_files = 0
        reclaimed_bytes = 0
        if not originals_dir.exists():
            return 0, 0

        for item in list(originals_dir.iterdir()):
            if item.is_file():
                reclaimed_bytes += item.stat().st_size
                item.unlink(missing_ok=True)
                deleted_files += 1
            elif item.is_dir():
                for sub in item.rglob("*"):
                    if sub.is_file():
                        reclaimed_bytes += sub.stat().st_size
                        deleted_files += 1
                shutil.rmtree(item, ignore_errors=True)

        return deleted_files, reclaimed_bytes

    def get_reclaimable_originals_bytes(self) -> int:
        # [NOTE] Calculates total size of /watch/_originals for the Web GUI "Clear Originals (X GB)" button.
        originals_dir = self.watch_dir / "_originals"
        if not originals_dir.exists():
            return 0
        return sum(f.stat().st_size for f in originals_dir.rglob("*") if f.is_file())

    def _loop(self) -> None:
        # [NOTE] Main worker loop: waits in IDLE until triggered, then drains active folders and queue.
        while not self._shutdown_event.is_set():
            self._wake_event.wait(timeout=1.0)
            self._wake_event.clear()

            while self.runner.status.state == EngineState.RUNNING and not self._shutdown_event.is_set():
                if self.active_top_folder:
                    remaining_in_active = [
                        p for p in self.scanner.list_candidates()
                        if len(p.relative_to(self.watch_dir).parts) > 1
                        and p.relative_to(self.watch_dir).parts == self.active_top_folder
                    ]
                    if not remaining_in_active:
                        self.active_top_folder = None

                candidates = self.scanner.list_candidates(priority_top_folder=self.active_top_folder)
                self.runner.status.pending_count = len(candidates)

                next_file = self.scanner.next_ready_file(
                    stability_wait=3.0,
                    priority_top_folder=self.active_top_folder,
                )
                if next_file is None:
                    self.active_top_folder = None
                    self.runner._stop_requested = False
                    self.runner._hard_stop_requested = False
                    self.scanner.cleanup_empty_folders()
                    self._flush_batch_summaries()
                    self.runner.status.pending_count = 0
                    self.runner.status.state = EngineState.IDLE
                    self.runner._save_status()
                    break

                rel_parts = next_file.relative_to(self.watch_dir).parts
                if len(rel_parts) > 1:
                    self.active_top_folder = rel_parts

                cfg = self.config_mgr.load()
                preset: Preset = self.preset_mgr.get_preset(self.runner.status.active_preset_id)

                try:
                    media = self.analyzer.probe(next_file, self.watch_dir)
                except ProbeError as exc:
                    self._quarantine_unreadable(next_file, preset, str(exc))
                    continue

                plan = self.analyzer.build_plan(media, preset, enable_ram_scratch=cfg.enable_ram_scratch)
                success = self.runner.execute_plan(plan)

                if success and plan.output_path.exists():
                    out_size = plan.output_path.stat().st_size
                    folder_key = plan.output_path.parent
                    self._batch_saved_bytes.setdefault(folder_key, []).append(
                        (plan.output_path.name, media.size_bytes, out_size)
                    )

                if self.runner.status.state in {EngineState.PAUSING, EngineState.STOPPED}:
                    self.runner._stop_requested = False
                    self.runner._hard_stop_requested = False
                    self.runner.status.state = EngineState.STOPPED
                    self.runner._save_status()
                    break

                if cfg.thermal_cooldown_sec > 0 and self.scanner.list_candidates():
                    self.runner.status.state = EngineState.COOLDOWN
                    self.runner._save_status()
                    time.sleep(min(30, max(0, cfg.thermal_cooldown_sec)))
                    if self.runner.status.state == EngineState.COOLDOWN:
                        self.runner.status.state = EngineState.RUNNING

    def _quarantine_unreadable(self, bad_file: Path, preset: Preset, reason: str) -> None:
        rel = bad_file.relative_to(self.watch_dir)
        dest = self.runner._unique_destination(self.watch_dir / "_failed" / rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(bad_file), str(dest))
        report = dest.with_suffix(dest.suffix + ".error.txt")
        report.write_text(
            f"AVE Pre-Encode Probe Failure\n"
            f"Timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
            f"Source File: {rel}\n"
            f"Preset: {preset.name}\n"
            f"Reason: {reason}\n",
            encoding="utf-8",
        )
        self.runner.status.failed_count += 1
        self.runner._save_status()

    def _flush_batch_summaries(self) -> None:
        cfg = self.config_mgr.load()
        if not cfg.write_batch_summaries or not self._batch_saved_bytes:
            return

        for folder, entries in self._batch_saved_bytes.items():
            total_in = sum(e for e in entries)
            total_out = sum(e for e in entries)
            saved_mb = max(0.0, (total_in - total_out) / (1024 * 1024))
            lines = [
                f"AVE Batch Summary — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"Files Processed: {len(entries)}",
                f"Total Space Saved: {saved_mb:.1f} MB",
                "-" * 50,
            ]
            for name, in_b, out_b in entries:
                lines.append(f"{name}: {in_b / (1024*1024):.1f} MB -> {out_b / (1024*1024):.1f} MB")

            if folder == self.output_dir:
                summary_path = self.output_dir / f"_root_files_{saved_mb:.0f}MB_saved.txt"
            else:
                summary_path = folder / "_ave_summary.txt"
            summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        self._batch_saved_bytes.clear()
