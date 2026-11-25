# [NOTE] Central data models defining the shared contract across all AVE engine modules and the Web GUI.

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal, Optional


class EngineState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSING = "pausing"        # Finish current file, then pause
    COOLDOWN = "cooldown"      # Thermal pause between files
    STOPPED = "stopped"


class JobAction(str, Enum):
    TRANSCODE = "transcode"    # Full re-encode via ffmpeg
    REMUX_CLEAN = "remux"      # Stream-copy with +genpts error cleanup
    PASSTHROUGH = "passthrough"  # Direct copy (already meets target specs)


@dataclass
class Preset:
    # [NOTE] Represents either a read-only built-in starter preset or a user-created preset.
    id: str
    name: str
    is_builtin: bool = False
    container: Literal["mkv", "mp4"] = "mkv"
    max_height: Optional[int] = 1080          # None = Original resolution
    video_codec: Literal["hevc", "av1", "h264"] = "hevc"
    bit_depth: Literal[8, 10] = 10
    crf: int = 22
    speed_preset: str = "fast"
    audio_codec: Literal["aac", "copy", "opus"] = "aac"
    audio_languages: list[str] = field(default_factory=lambda: ["eng"])
    subtitle_languages: list[str] = field(default_factory=lambda: ["eng"])
    passthrough_max_mb: Optional[int] = None  # Skip re-encode if codec matches & size <= threshold
    error_cleanup_remux: bool = True          # Default ON per v1.0 spec


@dataclass
class StreamInfo:
    # [NOTE] Normalized ffprobe metadata for a single video, audio, or subtitle stream.
    index: int
    codec_type: Literal["video", "audio", "subtitle", "data"]
    codec_name: str
    language: Optional[str] = None
    title: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class MediaInfo:
    # [NOTE] Complete probe inspection of a candidate source file in /watch.
    source_path: Path
    relative_path: Path
    size_bytes: int
    duration_sec: float
    streams: list[StreamInfo] = field(default_factory=list)


@dataclass
class EncodePlan:
    # [NOTE] Output of analyzer.py; tells builder.py exactly which streams to map and how to process them.
    media: MediaInfo
    preset: Preset
    action: JobAction
    output_path: Path
    temp_part_path: Path
    use_ram_scratch: bool
    target_height: Optional[int]              # None if native resolution is kept (never-upscale rule)
    has_audio: bool = True                    # False for silent video sources (screen captures, timelapses)
    selected_audio_indices: list[int] = field(default_factory=list)
    copy_subtitle_indices: list[int] = field(default_factory=list)
    convert_subtitle_indices: list[int] = field(default_factory=list)  # Converted to SRT


@dataclass
class BatchStatus:
    # [NOTE] Serialized to /config/status.json for live HTMX Web GUI polling.
    state: EngineState = EngineState.IDLE
    active_preset_id: str = "builtin_hevc_balanced"
    current_file: Optional[str] = None
    current_initial_bytes: int = 0
    progress_percent: float = 0.0
    fps: float = 0.0
    speed_multiplier: float = 0.0
    file_elapsed_sec: int = 0
    file_eta_sec: int = 0
    batch_encode_sec: int = 0                 # Formatted as HH:MM:SS (>24h supported) in UI
    pending_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    system_throttled: bool = False            # True when yielding CPU/IO to host workloads
    banner_message: Optional[str] = None      # e.g., "Recovered custom presets from backup"
