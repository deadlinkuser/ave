# [NOTE] Manages schema-versioned /config/config.json with non-destructive upgrade key merging.

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CONFIG_VERSION = 1


@dataclass
class AppConfig:
    # [NOTE] Persistent user settings stored in /config/config.json.
    config_version: int = CONFIG_VERSION
    default_preset_id: str = "builtin_hevc_balanced"
    enable_ram_scratch: bool = False
    thermal_cooldown_sec: int = 0          # 0–30 second pause between files
    write_batch_summaries: bool = True     # Opt-out default per v1.0 spec
    theme: str = "dark"                    # "dark" or "light"


class ConfigManager:
    # [NOTE] Loads, upgrades, and atomically saves config.json without overwriting user preferences.

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "config.json"

    def load(self) -> AppConfig:
        # [NOTE] Merges saved JSON keys over AppConfig defaults so container upgrades add new keys seamlessly.
        defaults = asdict(AppConfig())
        if not self.config_file.exists():
            cfg = AppConfig()
            self.save(cfg)
            return cfg

        try:
            raw: Any = json.loads(self.config_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config.json must be a JSON object")

            merged = {**defaults, **{k: v for k, v in raw.items() if k in defaults}}
            merged["config_version"] = CONFIG_VERSION
            cfg = AppConfig(**merged)

            # [NOTE] Persist merged schema if new keys were added during an upgrade.
            if raw.get("config_version") != CONFIG_VERSION or set(defaults.keys()) - set(raw.keys()):
                self.save(cfg)
            return cfg
        except Exception:
            cfg = AppConfig()
            self.save(cfg)
            return cfg

    def save(self, cfg: AppConfig) -> None:
        # [NOTE] Atomically writes config.json via .tmp rename.
        tmp_file = self.config_dir / "config.json.tmp"
        payload = asdict(cfg)
        payload["config_version"] = CONFIG_VERSION
        tmp_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(str(tmp_file), str(self.config_file))

    def set_default_preset(self, preset_id: str) -> AppConfig:
        # [NOTE] Updates the active default preset selected via the Web GUI "Make Default Preset" toggle.
        cfg = self.load()
        cfg.default_preset_id = preset_id
        self.save(cfg)
        return cfg
