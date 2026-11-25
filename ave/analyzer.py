# [NOTE] Runs ffprobe inspection and resolves resolution ceilings, ENG/ENG track rules, and passthrough eligibility.

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .models import EncodePlan, JobAction, MediaInfo, Preset, StreamInfo

COMPATIBLE_SUBS = {
    "subrip", "srt", "ass", "ssa",
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle",
}
CONVERTIBLE_TEXT_SUBS = {
    "mov_text", "tx3g", "webvtt", "eia_608", "eia_708", "jacosub", "microdvd", "realtext",
}

# [NOTE] Maps common 2-letter ISO 639-1 codes to 3-letter ISO 639-2 codes used by ffprobe.
LANG_ALIAS_MAP = {
    "en": "eng",
    "es": "spa",
    "fr": "fre",
    "de": "ger",
    "it": "ita",
    "ja": "jpn",
    "ko": "kor",
    "zh": "chi",
    "pt": "por",
    "ru": "rus",
}

CODEC_ALIAS_MAP = {
    "hevc": "hevc",
    "h265": "hevc",
    "x265": "hevc",
    "av1": "av1",
    "libaom-av1": "av1",
    "libsvtav1": "av1",
    "h264": "h264",
    "avc": "h264",
    "x264": "h264",
}


class ProbeError(Exception):
    # [NOTE] Raised when ffprobe fails or finds no valid primary video stream (triggers _failed quarantine).
    pass


class MediaAnalyzer:
    # [NOTE] Translates raw ffprobe JSON + active Preset into a validated EncodePlan.

    def __init__(self, output_dir: Path, shm_dir: Path = Path("/dev/shm")) -> None:
        self.output_dir = Path(output_dir)
        self.shm_dir = Path(shm_dir)

    @staticmethod
    def _normalize_lang(raw_lang: str | None) -> str | None:
        if not raw_lang:
            return None
        cleaned = raw_lang.strip().lower()
        if cleaned in {"und", "unk", ""}:
            return None
        return LANG_ALIAS_MAP.get(cleaned, cleaned)

    def probe(self, source_path: Path, watch_root: Path) -> MediaInfo:
        # [NOTE] Executes ffprobe -print_format json and parses streams, duration, and file size.
        cmd = [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(source_path),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
        except Exception as exc:
            raise ProbeError(f"ffprobe execution failed: {exc}") from exc

        if proc.returncode != 0 or not proc.stdout.strip():
            err_detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "unreadable media container"
            raise ProbeError(f"ffprobe error: {err_detail}")

        try:
            data: dict[str, Any] = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ProbeError("ffprobe returned malformed JSON metadata.") from exc

        fmt = data.get("format", {})
        try:
            duration_sec = float(fmt.get("duration", 0.0))
        except (TypeError, ValueError):
            duration_sec = 0.0

        streams: list[StreamInfo] = []
        has_primary_video = False

        for raw_stream in data.get("streams", []):
            codec_type = raw_stream.get("codec_type")
            if codec_type not in {"video", "audio", "subtitle", "data"}:
                continue

            # [NOTE] Skip embedded cover-art thumbnails so they are not treated as the main video track.
            disposition = raw_stream.get("disposition", {})
            if codec_type == "video" and disposition.get("attached_pic") == 1:
                continue

            tags = raw_stream.get("tags", {}) or {}
            lang = self._normalize_lang(tags.get("language") or tags.get("LANGUAGE"))
            title = tags.get("title") or tags.get("TITLE")

            stream_info = StreamInfo(
                index=int(raw_stream["index"]),
                codec_type=codec_type,
                codec_name=str(raw_stream.get("codec_name", "unknown")).lower(),
                language=lang,
                title=title,
                width=raw_stream.get("width"),
                height=raw_stream.get("height"),
            )
            streams.append(stream_info)

            if codec_type == "video" and (stream_info.height or 0) > 0:
                has_primary_video = True

        if not has_primary_video or duration_sec <= 0.0:
            raise ProbeError("No valid video stream or duration found in source file.")

        return MediaInfo(
            source_path=source_path,
            relative_path=source_path.relative_to(watch_root),
            size_bytes=source_path.stat().st_size,
            duration_sec=duration_sec,
            streams=streams,
        )

    def build_plan(self, media: MediaInfo, preset: Preset, enable_ram_scratch: bool = False) -> EncodePlan:
        # [NOTE] Evaluates never-upscale height, language track fallbacks, passthrough/remux, and /dev/shm headroom.
        video_stream = next(s for s in media.streams if s.codec_type == "video")
        source_height = video_stream.height or 0
        source_codec = CODEC_ALIAS_MAP.get(video_stream.codec_name, video_stream.codec_name)

        # [NOTE] Never-upscale rule: only downscale when source_height strictly exceeds preset ceiling.
        target_height: int | None = None
        if preset.max_height is not None and source_height > preset.max_height:
            target_height = preset.max_height

        # [NOTE] Resolve output path preserving nested Show/Season folder structure.
        rel_out = media.relative_path.with_suffix(f".{preset.container}")
        output_path = self.output_dir / rel_out

        # [NOTE] Check Smart Passthrough eligibility (matching codec, no downscale needed, under size threshold).
        action = JobAction.TRANSCODE
        if (
            preset.passthrough_max_mb is not None
            and source_codec == preset.video_codec
            and target_height is None
            and media.size_bytes <= (preset.passthrough_max_mb * 1024 * 1024)
        ):
            action = JobAction.REMUX_CLEAN if preset.error_cleanup_remux else JobAction.PASSTHROUGH

        # [NOTE] Select audio and subtitle streams (ENG/ENG by default; fallback to all if no tags match).
        selected_audio = self._select_tracks(media, preset.audio_languages, "audio")
        selected_subs = self._select_tracks(media, preset.subtitle_languages, "subtitle")

        copy_subs: list[int] = []
        convert_subs: list[int] = []
        stream_by_idx = {s.index: s for s in media.streams}

        for idx in selected_subs:
            sub_codec = stream_by_idx[idx].codec_name
            if sub_codec in COMPATIBLE_SUBS:
                copy_subs.append(idx)
            elif sub_codec in CONVERTIBLE_TEXT_SUBS:
                # [NOTE] MP4 containers require mov_text rather than SRT; MKV accepts SRT cleanly.
                if preset.container == "mkv":
                    convert_subs.append(idx)

        # [NOTE] Smart RAM Scratch check: requires free_shm > (source_size * 1.5) + 2 GB.
        use_ram = enable_ram_scratch and self._can_use_ram_scratch(media.size_bytes)
        part_filename = f".{output_path.name}.part.{preset.container}"
        if use_ram:
            temp_part_path = self.shm_dir / "ave_scratch" / part_filename
        else:
            temp_part_path = output_path.parent / part_filename

        return EncodePlan(
            media=media,
            preset=preset,
            action=action,
            output_path=output_path,
            temp_part_path=temp_part_path,
            use_ram_scratch=use_ram,
            target_height=target_height,
            has_audio=bool(selected_audio),
            selected_audio_indices=selected_audio,
            copy_subtitle_indices=copy_subs,
            convert_subtitle_indices=convert_subs,
        )


    def _select_tracks(self, media: MediaInfo, lang_codes: list[str], codec_type: str) -> list[int]:
        # [NOTE] Selects all tracks matching requested language codes; falls back to all tracks if none match.
        candidates = [s for s in media.streams if s.codec_type == codec_type]
        if not candidates:
            return []

        normalized_targets = {
            self._normalize_lang(code) for code in lang_codes if self._normalize_lang(code)
        }
        if not normalized_targets:
            return [s.index for s in candidates]

        matched = [s.index for s in candidates if self._normalize_lang(s.language) in normalized_targets]
        if matched:
            return matched

        # [NOTE] Fallback: keep all tracks of this type so untagged or foreign media is never stripped silent.
        return [s.index for s in candidates]

    def _can_use_ram_scratch(self, source_size: int) -> bool:
        # [NOTE] Returns True only if free_shm > (source_size * 1.5) + 2 GB.
        try:
            if not self.shm_dir.exists():
                return False
            usage = shutil.disk_usage(self.shm_dir)
            required_bytes = int(source_size * 1.5) + (2 * 1024 * 1024 * 1024)
            return usage.free > required_bytes
        except OSError:
            return False
