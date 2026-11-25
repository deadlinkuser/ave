# [NOTE] Assembles safe, injection-free ffmpeg CLI argument lists from an EncodePlan.

from __future__ import annotations

import os

from .models import EncodePlan, JobAction


class CommandBuilder:
    # [NOTE] Builds deterministic ffmpeg command arrays with dark-scene AQ, thread caps, and metadata preservation.

    def build_command(self, plan: EncodePlan, cpu_count: int | None = None) -> list[str]:
        # [NOTE] Dispatches to transcode, error-cleanup remux, or direct copy command generation.
        cores = cpu_count or os.cpu_count() or 4

        if plan.action == JobAction.REMUX_CLEAN:
            return self._build_remux_args(plan)

        # [NOTE] Full transcode path (JobAction.PASSTHROUGH is handled directly via file copy in runner.py).
        return self._build_transcode_args(plan, cores)

    def _build_transcode_args(self, plan: EncodePlan, cpu_count: int) -> list[str]:
        # [NOTE] Configures libx265 (yuv420p10le, --aq-mode 3, pools=16 cap), scale=-2:H, AAC, and SRT sub conversion.
        preset = plan.preset
        cmd: list[str] = [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-progress", "pipe:1",
            "-nostats",
            "-thread_queue_size", "1024",
            "-i", str(plan.media.source_path),
            "-max_muxing_queue_size", "2048",
            # [NOTE] Preserve container metadata, stream titles, and chapter markers.
            "-map_metadata", "0",
            "-map_chapters", "0",
        ]

        # [NOTE] Map primary video stream (first non-attached video stream).
        primary_video = next(s for s in plan.media.streams if s.codec_type == "video")
        cmd.extend(["-map", f"0:{primary_video.index}"])

        # [NOTE] Map selected audio tracks, or explicitly disable audio (-an) if source is silent.
        if plan.has_audio and plan.selected_audio_indices:
            for stream_idx in plan.selected_audio_indices:
                cmd.extend(["-map", f"0:{stream_idx}"])
        else:
            cmd.append("-an")

        # [NOTE] Map compatible subtitles (stream copy) and convertible text subtitles (convert to SRT).
        all_sub_indices = [*plan.copy_subtitle_indices, *plan.convert_subtitle_indices]
        for stream_idx in all_sub_indices:
            cmd.extend(["-map", f"0:{stream_idx}"])

        # [NOTE] Video codec & dark-scene gradient retention settings.
        cmd.extend(self._video_codec_args(plan, cpu_count))

        # [NOTE] Never-upscale resolution ceiling filter (only applied when downscaling).
        if plan.target_height is not None:
            cmd.extend(["-vf", f"scale=-2:{plan.target_height}"])

        # [NOTE] Audio encoding configuration.
        if plan.has_audio and plan.selected_audio_indices:
            if preset.audio_codec == "copy":
                cmd.extend(["-c:a", "copy"])
            elif preset.audio_codec == "opus":
                cmd.extend(["-c:a", "libopus", "-b:a", "160k"])
            else:
                cmd.extend(["-c:a", "aac", "-b:a", "192k"])

        # [NOTE] Per-output-stream subtitle codec rules (copy compatible, convert text tracks to SRT).
        for out_sub_pos, orig_idx in enumerate(all_sub_indices):
            if orig_idx in plan.copy_subtitle_indices:
                cmd.extend([f"-c:s:{out_sub_pos}", "copy"])
            else:
                cmd.extend([f"-c:s:{out_sub_pos}", "srt"])

        # [NOTE] Explicitly force container muxer format so .part.<ext> temp filenames write valid headers.
        muxer = "matroska" if preset.container == "mkv" else "mp4"
        if muxer == "mp4":
            cmd.extend(["-movflags", "+faststart"])
        cmd.extend(["-f", muxer, str(plan.temp_part_path)])

        return cmd

    def _video_codec_args(self, plan: EncodePlan, cpu_count: int) -> list[str]:
        # [NOTE] Builds encoder-specific flags including 10-bit pixel format, AQ mode 3, and pools=16 cap.
        preset = plan.preset
        pix_fmt = "yuv420p10le" if preset.bit_depth == 10 else "yuv420p"

        if preset.video_codec == "hevc":
            x265_params: list[str] = ["log-level=error"]
            # [NOTE] Dark-scene gradient retention on fast/balanced presets.
            if preset.speed_preset in {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium"}:
                x265_params.append("aq-mode=3")
            # [NOTE] High-core stability cap: prevent thread pool contention on >16 core CPUs.
            if cpu_count > 16:
                x265_params.append("pools=16")

            return [
                "-c:v", "libx265",
                "-pix_fmt", pix_fmt,
                "-crf", str(preset.crf),
                "-preset", preset.speed_preset,
                "-x265-params", ":".join(x265_params),
            ]

        if preset.video_codec == "av1":
            return [
                "-c:v", "libsvtav1",
                "-pix_fmt", pix_fmt,
                "-crf", str(preset.crf),
                "-preset", preset.speed_preset,
            ]

        # [NOTE] H.264 Universal fallback.
        return [
            "-c:v", "libx264",
            "-pix_fmt", pix_fmt,
            "-crf", str(preset.crf),
            "-preset", preset.speed_preset,
        ]

    def _build_remux_args(self, plan: EncodePlan) -> list[str]:
        # [NOTE] Builds fast stream-copy remux (-err_detect ignore_err -map 0 -c copy -fflags +genpts) for cleanup.
        muxer = "matroska" if plan.preset.container == "mkv" else "mp4"
        return [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-progress", "pipe:1",
            "-nostats",
            "-err_detect", "ignore_err",
            "-i", str(plan.media.source_path),
            "-map", "0",
            "-map_metadata", "0",
            "-map_chapters", "0",
            "-c", "copy",
            "-fflags", "+genpts",
            "-f", muxer,
            str(plan.temp_part_path),
        ]
