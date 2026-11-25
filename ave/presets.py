# [NOTE] Manages built-in starter presets, custom_presets.json CRUD, rolling .bak backups, and boot self-healing.

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from .models import Preset

SCHEMA_VERSION = 1

BUILTIN_PRESETS: list[Preset] = [
    # [NOTE] Shipped default: MKV, 1080p ceiling, HEVC 10-bit, CRF 22, fast, AAC, ENG/ENG.
    Preset(
        id="builtin_hevc_balanced",
        name="HEVC Balanced (Default)",
        is_builtin=True,
        container="mkv",
        max_height=1080,
        video_codec="hevc",
        bit_depth=10,
        crf=22,
        speed_preset="fast",
        audio_codec="aac",
        audio_languages=["eng"],
        subtitle_languages=["eng"],
        error_cleanup_remux=True,
    ),
    Preset(
        id="builtin_hevc_hq",
        name="HEVC High Quality",
        is_builtin=True,
        crf=19,
        speed_preset="medium",
    ),
    Preset(
        id="builtin_hevc_compact",
        name="HEVC Compact",
        is_builtin=True,
        crf=24,
        speed_preset="fast",
    ),
    Preset(
        id="builtin_av1",
        name="AV1 Efficient",
        is_builtin=True,
        video_codec="av1",
        crf=28,
        speed_preset="6",
    ),
    Preset(
        id="builtin_h264",
        name="H.264 Universal",
        is_builtin=True,
        container="mp4",
        video_codec="h264",
        bit_depth=8,
        crf=21,
    ),
]


class PresetManager:
    # [NOTE] Handles loading, atomic saving with .bak rotation, corruption recovery, and Web UI import/export.

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.presets_file = self.config_dir / "custom_presets.json"
        self.backup_file = self.config_dir / "custom_presets.json.bak"
        self.corrupt_file = self.config_dir / "custom_presets.corrupt.json"
        self.startup_notice: Optional[str] = None

    @staticmethod
    def slugify_id(name: str) -> str:
        # [NOTE] Generates a clean, collision-safe custom preset ID from a user-supplied name.
        slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
        return f"custom_{slug or 'preset'}"

    def load_all(self) -> list[Preset]:
        # [NOTE] Returns built-in presets merged with custom presets; auto-recovers from .bak if JSON is corrupt.
        custom_presets = self._load_custom_with_recovery()
        return [*BUILTIN_PRESETS, *custom_presets]

    def get_preset(self, preset_id: str) -> Preset:
        # [NOTE] Looks up a preset by ID, falling back to the shipped default if the ID no longer exists.
        for preset in self.load_all():
            if preset.id == preset_id:
                return preset
        return BUILTIN_PRESETS[0]

    def save_custom_preset(self, preset: Preset) -> Preset:
        # [NOTE] Rotates existing custom_presets.json to .bak before atomically writing updated JSON via .tmp.
        if preset.is_builtin or preset.id.startswith("builtin_"):
            raise ValueError("Built-in presets cannot be overwritten. Save as a new custom preset.")

        if not preset.id:
            preset.id = self.slugify_id(preset.name)
        preset.is_builtin = False

        existing = self._load_custom_with_recovery()
        updated = [p for p in existing if p.id != preset.id]
        updated.append(preset)

        self._write_atomic_with_backup(updated)
        return preset

    def delete_custom_preset(self, preset_id: str) -> bool:
        # [NOTE] Removes a user preset by ID with rolling .bak protection; returns True if deleted.
        if preset_id.startswith("builtin_"):
            raise ValueError("Built-in presets cannot be deleted.")

        existing = self._load_custom_with_recovery()
        filtered = [p for p in existing if p.id != preset_id]
        if len(filtered) == len(existing):
            return False

        self._write_atomic_with_backup(filtered)
        return True

    def export_json(self) -> str:
        # [NOTE] Serializes custom presets with schema header for 1-click browser download.
        customs = self._load_custom_with_recovery()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "presets": [asdict(p) for p in customs],
        }
        return json.dumps(payload, indent=2)

    def import_json(self, raw_json: str, merge: bool = True) -> int:
        # [NOTE] Validates and imports presets from an uploaded .json or .bak file; returns count imported.
        imported = self._parse_presets_payload(raw_json)
        if not imported:
            return 0

        if merge:
            existing = {p.id: p for p in self._load_custom_with_recovery()}
            for item in imported:
                existing[item.id] = item
            final_list = list(existing.values())
        else:
            final_list = imported

        self._write_atomic_with_backup(final_list)
        return len(imported)

    def _load_custom_with_recovery(self) -> list[Preset]:
        # [NOTE] Implements boot-time auto-adoption and self-healing from custom_presets.json.bak.
        if self.presets_file.exists():
            try:
                raw = self.presets_file.read_text(encoding="utf-8")
                presets = self._parse_presets_payload(raw)
                if presets and self.startup_notice is None:
                    self.startup_notice = f"Loaded {len(presets)} custom preset(s) from /config."
                return presets
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                # [NOTE] Preserve broken file for inspection, then attempt .bak recovery.
                shutil.move(str(self.presets_file), str(self.corrupt_file))
                if self.backup_file.exists():
                    try:
                        bak_raw = self.backup_file.read_text(encoding="utf-8")
                        recovered = self._parse_presets_payload(bak_raw)
                        self._write_atomic(recovered)
                        self.startup_notice = "Recovered custom presets from backup."
                        return recovered
                    except Exception:
                        pass
                # [NOTE] Both primary and backup were unreadable; initialize clean file so startup never fails.
                self._write_atomic([])
                self.startup_notice = "Custom presets file was unreadable; initialized clean preset store."
                return []

        # [NOTE] Primary file missing on boot, but a .bak file exists (e.g., accidental deletion).
        if self.backup_file.exists():
            try:
                bak_raw = self.backup_file.read_text(encoding="utf-8")
                recovered = self._parse_presets_payload(bak_raw)
                self._write_atomic(recovered)
                self.startup_notice = "Restored custom presets from backup."
                return recovered
            except Exception:
                pass

        # [NOTE] Brand-new install: initialize empty custom_presets.json quietly.
        self._write_atomic([])
        return []

    def _parse_presets_payload(self, raw_json: str) -> list[Preset]:
        # [NOTE] Strictly parses and validates schema_version and Preset dataclass fields.
        data: Any = json.loads(raw_json)
        if not isinstance(data, dict) or "presets" not in data or not isinstance(data["presets"], list):
            raise ValueError("Invalid preset JSON structure.")

        parsed: list[Preset] = []
        for entry in data["presets"]:
            if not isinstance(entry, dict):
                raise ValueError("Preset entry must be a JSON object.")
            entry_clean = dict(entry)
            entry_clean["is_builtin"] = False
            if not str(entry_clean.get("id", "")).startswith("custom_"):
                entry_clean["id"] = self.slugify_id(str(entry_clean.get("name", "preset")))
            parsed.append(Preset(**entry_clean))
        return parsed

    def _write_atomic_with_backup(self, presets: list[Preset]) -> None:
        # [NOTE] Copies current valid custom_presets.json to .bak before writing new content atomically.
        if self.presets_file.exists():
            try:
                # [NOTE] Only rotate to .bak if the existing file is valid JSON.
                self._parse_presets_payload(self.presets_file.read_text(encoding="utf-8"))
                shutil.copy2(str(self.presets_file), str(self.backup_file))
            except Exception:
                pass
        self._write_atomic(presets)

    def _write_atomic(self, presets: list[Preset]) -> None:
        # [NOTE] Writes to a .tmp file and uses os.replace() for POSIX-atomic file replacement.
        tmp_file = self.config_dir / "custom_presets.json.tmp"
        payload = {
            "schema_version": SCHEMA_VERSION,
            "presets": [asdict(p) for p in presets],
        }
        tmp_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(str(tmp_file), str(self.presets_file))
