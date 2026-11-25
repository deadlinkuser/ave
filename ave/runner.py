# [NOTE] Executes ffmpeg with live progress telemetry, host-load throttling, stop handling, and post-encode QA.

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .analyzer import MediaAnalyzer, ProbeError
from .builder import CommandBuilder
from .models import BatchStatus, EncodePlan, EngineState, JobAction


class EncodeRunner:
    # [NOTE] Orchestrates job execution, status.json updates, renice/ionice yielding, and _originals/_failed routing.

    def __init__(self, watch_dir: Path, output_dir: Path, config_dir: Path) -> None:
        self.watch_dir = Path(watch_dir)
        self.output_dir = Path(output_dir)
        self.config_dir = Path(config_dir)
        self.originals_dir = self.watch_dir / "_originals"
        self.failed_dir = self.watch_dir / "_failed"

        self.originals_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.status_file = self.config_dir / "status.json"
        self.status = BatchStatus()
        self.builder = CommandBuilder()
        self.validator_probe = MediaAnalyzer(output_dir=self.output_dir)

        self._active_proc: subprocess.Popen[str] | None = None
        self._stop_requested: bool = False
        self._hard_stop_requested: bool = False

    def start_new_batch(self, active_preset_id: str) -> None:
        # [NOTE] Resets per-batch HH:MM:SS timer and counters when a fresh batch begins from Idle/Stopped.
        self._stop_requested = False
        self._hard_stop_requested = False
        self.status = BatchStatus(
            state=EngineState.RUNNING,
            active_preset_id=active_preset_id,
            batch_encode_sec=0,
        )
        self._save_status()

    def request_stop(self, hard: bool = False) -> None:
        # [NOTE] Handles Stop After Current File (hard=False) or Immediate Hard Stop (hard=True).
        self._stop_requested = True
        if hard:
            self._hard_stop_requested = True
            self.status.state = EngineState.STOPPED
            if self._active_proc and self._active_proc.poll() is None:
                try:
                    self._active_proc.terminate()
                except OSError:
                    pass
        else:
            if self.status.state == EngineState.RUNNING:
                self.status.state = EngineState.PAUSING
        self._save_status()

    def execute_plan(self, plan: EncodePlan) -> bool:
        # [NOTE] Runs ffmpeg subprocess, parses -progress pipe:1 into status.json, and handles graceful/hard stops.
        plan.temp_part_path.parent.mkdir(parents=True, exist_ok=True)
        plan.output_path.parent.mkdir(parents=True, exist_ok=True)

        self.status.current_file = str(plan.media.relative_path)
        self.status.current_initial_bytes = plan.media.size_bytes
        self.status.progress_percent = 0.0
        self.status.fps = 0.0
        self.status.speed_multiplier = 0.0
        self.status.file_elapsed_sec = 0
        self.status.file_eta_sec = 0
        if not self._stop_requested:
            self.status.state = EngineState.RUNNING
        self._save_status()

        # [NOTE] Direct passthrough copy (no remux requested).
        if plan.action == JobAction.PASSTHROUGH:
            return self._run_direct_copy(plan)

        cmd = self.builder.build_command(plan)
        file_start_mono = time.monotonic()
        batch_sec_before_file = self.status.batch_encode_sec
        last_courtesy_check = 0.0
        last_status_flush = 0.0
        stderr_tail: list[str] = []

        try:
            self._active_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            self._cleanup_part_file(plan.temp_part_path)
            self.finalize_job(plan, passed=False, error_reason=f"Failed to launch ffmpeg: {exc}")
            return False

        # [NOTE] Parse ffmpeg -progress key=value stream line by line until exit.
        assert self._active_proc.stdout is not None
        for raw_line in self._active_proc.stdout:
            line = raw_line.strip()
            now = time.monotonic()
            elapsed_this_file = int(now - file_start_mono)
            self.status.file_elapsed_sec = elapsed_this_file
            self.status.batch_encode_sec = batch_sec_before_file + elapsed_this_file

            if "=" in line:
                key, val = line.split("=", 1)
                self._apply_progress_metric(key, val, plan.media.duration_sec)

            # [NOTE] Periodically adjust CPU/IO niceness if external host load spikes.
            if now - last_courtesy_check >= 4.0 and self._active_proc.poll() is None:
                self._adjust_system_courtesy(self._active_proc.pid)
                last_courtesy_check = now

            # [NOTE] Flush status.json every ~0.5s to avoid unnecessary disk writes while keeping UI snappy.
            if now - last_status_flush >= 0.5:
                self._save_status()
                last_status_flush = now

        returncode = self._active_proc.wait()
        if self._active_proc.stderr:
            stderr_output = self._active_proc.stderr.read().strip()
            if stderr_output:
                stderr_tail = stderr_output.splitlines()[-5:]
        self._active_proc = None

        # [NOTE] Hard stop cleanup: discard .part file and leave source untouched without quarantining.
        if self._hard_stop_requested:
            self._cleanup_part_file(plan.temp_part_path)
            self.status.current_file = None
            self.status.state = EngineState.STOPPED
            self._save_status()
            return False

        # [NOTE] If error-cleanup remux failed on a passthrough file, fall back to direct copy per v1.0 spec.
        if returncode != 0 and plan.action == JobAction.REMUX_CLEAN:
            self._cleanup_part_file(plan.temp_part_path)
            return self._run_direct_copy(plan)

        if returncode != 0:
            self._cleanup_part_file(plan.temp_part_path)
            reason = " | ".join(stderr_tail) if stderr_tail else f"ffmpeg exited with code {returncode}"
            self.finalize_job(plan, passed=False, error_reason=reason)
            return False

        # [NOTE] Post-encode QA validation (>100 KB, valid video stream, duration within 2s).
        passed, qa_message = self.validate_output(plan)
        if not passed:
            self._cleanup_part_file(plan.temp_part_path)
            self.finalize_job(plan, passed=False, error_reason=qa_message)
            return False

        self.finalize_job(plan, passed=True)
        return True

    def validate_output(self, plan: EncodePlan) -> tuple[bool, str]:
        # [NOTE] Verifies output > 100 KB, valid video stream exists, and duration matches source within 2 seconds.
        part_path = plan.temp_part_path
        if not part_path.is_file():
            return False, "Encoded temporary output file is missing."

        size_bytes = part_path.stat().st_size
        if size_bytes <= 100 * 1024:
            return False, f"Encoded output is too small ({size_bytes} bytes; minimum 100 KB required)."

        try:
            out_media = self.validator_probe.probe(part_path, part_path.parent)
        except ProbeError as exc:
            return False, f"Output failed ffprobe validation: {exc}"

        duration_diff = abs(out_media.duration_sec - plan.media.duration_sec)
        if duration_diff > 2.0:
            return (
                False,
                f"Duration mismatch: source={plan.media.duration_sec:.1f}s vs output={out_media.duration_sec:.1f}s",
            )

        return True, "OK"

    def finalize_job(self, plan: EncodePlan, passed: bool, error_reason: str = "") -> None:
        # [NOTE] Moves temp .part file to final output and routes source to /watch/_originals or /watch/_failed.
        if passed:
            plan.output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(plan.temp_part_path), str(plan.output_path))

            dest_original = self._unique_destination(self.originals_dir / plan.media.relative_path)
            dest_original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(plan.media.source_path), str(dest_original))

            self.status.completed_count += 1
        else:
            dest_failed = self._unique_destination(self.failed_dir / plan.media.relative_path)
            dest_failed.parent.mkdir(parents=True, exist_ok=True)
            if plan.media.source_path.exists():
                shutil.move(str(plan.media.source_path), str(dest_failed))

            # [NOTE] Write plain-English error report alongside the quarantined source file.
            report_path = dest_failed.with_suffix(dest_failed.suffix + ".error.txt")
            report_text = (
                f"AVE Encode Failure Report\n"
                f"Timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
                f"Source File: {plan.media.relative_path}\n"
                f"Preset: {plan.preset.name} ({plan.preset.id})\n"
                f"Reason: {error_reason}\n"
            )
            report_path.write_text(report_text, encoding="utf-8")
            self.status.failed_count += 1

        self.status.current_file = None
        self.status.progress_percent = 0.0
        self.status.file_eta_sec = 0
        self._save_status()

        def _run_direct_copy(self, plan: EncodePlan) -> bool:
            # [NOTE] Performs atomic file copy into .part path followed by standard QA validation.
            try:
                shutil.copy2(str(plan.media.source_path), str(plan.temp_part_path))
            except OSError as exc:
                self._cleanup_part_file(plan.temp_part_path)
                self.finalize_job(plan, passed=False, error_reason=f"Passthrough copy failed: {exc}")
                return False

            passed, qa_message = self.validate_output(plan)
            if not passed:
                self._cleanup_part_file(plan.temp_part_path)
                self.finalize_job(plan, passed=False, error_reason=qa_message)
                return False

            self.finalize_job(plan, passed=True)
            return True

    def _apply_progress_metric(self, key: str, val: str, total_duration_sec: float) -> None:
        # [NOTE] Updates progress %, fps, speed multiplier, and ETA from ffmpeg -progress key=value lines.
        if key == "fps":
            try:
                self.status.fps = round(float(val), 1)
            except ValueError:
                pass
        elif key == "speed":
            cleaned = val.rstrip("x").strip()
            try:
                self.status.speed_multiplier = round(float(cleaned), 2)
            except ValueError:
                pass
        elif key == "out_time_us":
            try:
                encoded_sec = max(0.0, int(val) / 1_000_000.0)
                if total_duration_sec > 0:
                    pct = min(99.9, (encoded_sec / total_duration_sec) * 100.0)
                    self.status.progress_percent = round(pct, 1)
                    if self.status.speed_multiplier > 0:
                        remaining_media_sec = max(0.0, total_duration_sec - encoded_sec)
                        self.status.file_eta_sec = int(remaining_media_sec / self.status.speed_multiplier)
            except ValueError:
                pass
        elif key == "progress" and val == "end":
            self.status.progress_percent = 100.0
            self.status.file_eta_sec = 0

    def _adjust_system_courtesy(self, pid: int) -> None:
        # [NOTE] Monitors host CPU/IO load and applies renice/ionice yielding when external workloads spike.
        try:
            load_1m, _, _ = os.getloadavg()
            cores = os.cpu_count() or 4
            # [NOTE] Yield if 1-minute load average exceeds 85% of available CPU threads.
            overloaded = load_1m > (cores * 0.85)
            self.status.system_throttled = overloaded

            target_nice = 15 if overloaded else 5
            os.setpriority(os.PRIO_PROCESS, pid, target_nice)

            if shutil.which("ionice"):
                io_class = "3" if overloaded else "2"
                subprocess.run(
                    ["ionice", "-c", io_class, "-p", str(pid)],
                    capture_output=True,
                    check=False,
                )
        except OSError:
            pass

    @staticmethod
    def _unique_destination(dest_path: Path) -> Path:
        # [NOTE] Appends a timestamp suffix if a file with the same name already exists in _originals or _failed.
        if not dest_path.exists():
            return dest_path
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return dest_path.with_name(f"{dest_path.stem}_{stamp}{dest_path.suffix}")

    @staticmethod
    def _cleanup_part_file(part_path: Path) -> None:
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _save_status(self) -> None:
        # [NOTE] Atomically writes status.json so the FastAPI/HTMX frontend never reads partial telemetry.
        tmp_status = self.config_dir / "status.json.tmp"
        payload = asdict(self.status)
        payload["state"] = self.status.state.value
        tmp_status.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(str(tmp_status), str(self.status_file))

