# [NOTE] Automated unit and integration tests covering all 5 core AVE engine modules using built-in unittest.

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ave.analyzer import MediaAnalyzer
from ave.builder import CommandBuilder
from ave.models import EncodePlan, JobAction, MediaInfo, Preset, StreamInfo
from ave.presets import BUILTIN_PRESETS, PresetManager
from ave.runner import EncodeRunner
from ave.scanner import QueueScanner


class TestPresetManager(unittest.TestCase):
    # [NOTE] Verifies built-in preset protection, .bak rotation, and automatic corruption self-healing.

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ave_test_presets_"))
        self.pm = PresetManager(self.tmp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_builtin_default_matches_spec(self) -> None:
        default = self.pm.get_preset("builtin_hevc_balanced")
        self.assertEqual(default.container, "mkv")
        self.assertEqual(default.max_height, 1080)
        self.assertEqual(default.video_codec, "hevc")
        self.assertEqual(default.bit_depth, 10)
        self.assertEqual(default.crf, 22)
        self.assertEqual(default.speed_preset, "fast")
        self.assertEqual(default.audio_codec, "aac")
        self.assertEqual(default.audio_languages, ["eng"])
        self.assertEqual(default.subtitle_languages, ["eng"])
        self.assertTrue(default.error_cleanup_remux)

    def test_cannot_overwrite_builtin(self) -> None:
        with self.assertRaises(ValueError):
            self.pm.save_custom_preset(BUILTIN_PRESETS[0])

    def test_rolling_backup_and_corruption_recovery(self) -> None:
        # [NOTE] Save first preset, then second preset so .bak holds the first valid state.
        self.pm.save_custom_preset(Preset(id="", name="Anime Crisp", crf=19))
        self.pm.save_custom_preset(Preset(id="", name="Mobile Small", max_height=720, crf=25))
        self.assertTrue(self.pm.backup_file.exists())

        # [NOTE] Corrupt the primary custom_presets.json file intentionally.
        self.pm.presets_file.write_text("{corrupted_json: [1, 2", encoding="utf-8")

        # [NOTE] Re-initialize PresetManager to simulate container restart.
        rebooted_pm = PresetManager(self.tmp_dir)
        presets = rebooted_pm.load_all()
        custom_names = [p.name for p in presets if not p.is_builtin]

        self.assertIn("Anime Crisp", custom_names)
        self.assertTrue(rebooted_pm.corrupt_file.exists())
        self.assertEqual(rebooted_pm.startup_notice, "Recovered custom presets from backup.")


class TestQueueScanner(unittest.TestCase):
    # [NOTE] Verifies natural human-numeric sorting, incomplete file exclusion, and empty folder cleanup.

    def setUp(self) -> None:
        self.watch_dir = Path(tempfile.mkdtemp(prefix="ave_test_watch_"))
        self.scanner = QueueScanner(self.watch_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.watch_dir, ignore_errors=True)

    def test_natural_sort_and_temp_exclusion(self) -> None:
        (self.watch_dir / "Show/Season 10").mkdir(parents=True)
        (self.watch_dir / "Show/Season 2").mkdir(parents=True)

        (self.watch_dir / "Show/Season 10/Ep 1.mkv").write_bytes(b"data")
        (self.watch_dir / "Show/Season 2/Ep 10.mkv").write_bytes(b"data")
        (self.watch_dir / "Show/Season 2/Ep 2.mkv").write_bytes(b"data")
        (self.watch_dir / "Show/Season 2/Ep 1.mkv.part").write_bytes(b"partial")
        (self.watch_dir / "Show/Season 2/Ep 3.mp4.!qB").write_bytes(b"partial")
        (self.watch_dir / "_originals/already_done.mkv").write_bytes(b"done")

        candidates = self.scanner.list_candidates()
        rel_paths = [p.relative_to(self.watch_dir).as_posix() for p in candidates]

        self.assertEqual(
            rel_paths,
            [
                "Show/Season 2/Ep 2.mkv",
                "Show/Season 2/Ep 10.mkv",
                "Show/Season 10/Ep 1.mkv",
            ],
        )

    def test_cleanup_empty_folders_protects_special_dirs(self) -> None:
        empty_season = self.watch_dir / "DeadShow/Season 1"
        empty_season.mkdir(parents=True)
        (empty_season / ".DS_Store").write_bytes(b"mac_junk")

        removed = self.scanner.cleanup_empty_folders()
        self.assertGreaterEqual(removed, 2)
        self.assertFalse((self.watch_dir / "DeadShow").exists())
        self.assertTrue((self.watch_dir / "_originals").exists())
        self.assertTrue((self.watch_dir / "_failed").exists())


class TestAnalyzerAndBuilder(unittest.TestCase):
    # [NOTE] Verifies never-upscale ceilings, ENG/ENG filtering, silent fallback, and ffmpeg CLI generation.

    def setUp(self) -> None:
        self.out_dir = Path(tempfile.mkdtemp(prefix="ave_test_out_"))
        self.analyzer = MediaAnalyzer(output_dir=self.out_dir)
        self.builder = CommandBuilder()
        self.default_preset = BUILTIN_PRESETS[0]

    def tearDown(self) -> None:
        shutil.rmtree(self.out_dir, ignore_errors=True)

    def _sample_media(self, height: int = 2160, include_audio: bool = True) -> MediaInfo:
        streams = [
            StreamInfo(index=0, codec_type="video", codec_name="h264", width=3840, height=height),
        ]
        if include_audio:
            streams.extend([
                StreamInfo(index=1, codec_type="audio", codec_name="dts", language="eng"),
                StreamInfo(index=2, codec_type="audio", codec_name="ac3", language="jpn"),
                StreamInfo(index=3, codec_type="audio", codec_name="aac", language="en"),
            ])
        streams.extend([
            StreamInfo(index=4, codec_type="subtitle", codec_name="subrip", language="eng"),
            StreamInfo(index=5, codec_type="subtitle", codec_name="webvtt", language="eng"),
            StreamInfo(index=6, codec_type="subtitle", codec_name="ass", language="jpn"),
        ])
        return MediaInfo(
            source_path=Path("/watch/Movie.mkv"),
            relative_path=Path("Movie.mkv"),
            size_bytes=500 * 1024 * 1024,
            duration_sec=3600.0,
            streams=streams,
        )

    def test_never_upscale_and_eng_track_filtering(self) -> None:
        # [NOTE] 720p source under 1080p preset must NOT upscale (target_height=None).
        media_720 = self._sample_media(height=720)
        plan_720 = self.analyzer.build_plan(media_720, self.default_preset)
        self.assertIsNone(plan_720.target_height)
        self.assertEqual(plan_720.selected_audio_indices, [1, 3])
        self.assertEqual(plan_720.copy_subtitle_indices, [4])
        self.assertEqual(plan_720.convert_subtitle_indices, [5])

        # [NOTE] 2160p source under 1080p preset must downscale to 1080.
        media_4k = self._sample_media(height=2160)
        plan_4k = self.analyzer.build_plan(media_4k, self.default_preset)
        self.assertEqual(plan_4k.target_height, 1080)

        cmd = self.builder.build_command(plan_4k, cpu_count=24)
        self.assertIn("libx265", cmd)
        self.assertIn("yuv420p10le", cmd)
        self.assertIn("aq-mode=3:pools=16", " ".join(cmd))
        self.assertIn("scale=-2:1080", cmd)
        self.assertIn("srt", cmd)

    def test_silent_video_passes_an_flag(self) -> None:
        silent_media = self._sample_media(height=1080, include_audio=False)
        plan = self.analyzer.build_plan(silent_media, self.default_preset)
        self.assertFalse(plan.has_audio)

        cmd = self.builder.build_command(plan, cpu_count=8)
        self.assertIn("-an", cmd)


class TestRunnerQAAndRouting(unittest.TestCase):
    # [NOTE] Verifies post-encode QA thresholds (>100 KB, duration match) and _originals/_failed file routing.

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ave_test_runner_"))
        self.watch = self.root / "watch"
        self.output = self.root / "output"
        self.config = self.root / "config"
        self.watch.mkdir()
        self.output.mkdir()
        self.runner = EncodeRunner(self.watch, self.output, self.config)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _make_plan(self, source_file: Path) -> EncodePlan:
        media = MediaInfo(
            source_path=source_file,
            relative_path=source_file.relative_to(self.watch),
            size_bytes=source_file.stat().st_size,
            duration_sec=120.0,
            streams=[StreamInfo(index=0, codec_type="video", codec_name="hevc", width=1920, height=1080)],
        )
        out_path = self.output / media.relative_path.with_suffix(".mkv")
        part_path = self.output / f".{out_path.name}.part.mkv"
        return EncodePlan(
            media=media,
            preset=BUILTIN_PRESETS[0],
            action=JobAction.TRANSCODE,
            output_path=out_path,
            temp_part_path=part_path,
            use_ram_scratch=False,
            target_height=None,
            has_audio=False,
        )

    def test_qa_rejects_tiny_file_and_quarantines(self) -> None:
        src = self.watch / "broken.mkv"
        src.write_bytes(b"source_video_bytes")
        plan = self._make_plan(src)

        # [NOTE] Simulate a 5 KB truncated output file (below 100 KB threshold).
        plan.temp_part_path.write_bytes(b"x" * 5000)
        passed, reason = self.runner.validate_output(plan)
        self.assertFalse(passed)
        self.assertIn("100 KB", reason)

        self.runner.finalize_job(plan, passed=False, error_reason=reason)
        self.assertFalse(src.exists())
        self.assertTrue((self.watch / "_failed/broken.mkv").exists())
        self.assertTrue((self.watch / "_failed/broken.mkv.error.txt").exists())

    def test_qa_pass_moves_to_output_and_originals(self) -> None:
        src = self.watch / "good.mkv"
        src.write_bytes(b"source_video_bytes")
        plan = self._make_plan(src)
        plan.temp_part_path.write_bytes(b"x" * (150 * 1024))

        # [NOTE] Mock validator ffprobe call so test runs without needing a real video binary.
        with patch.object(self.runner.validator_probe, "probe", return_value=plan.media):
            passed, reason = self.runner.validate_output(plan)
            self.assertTrue(passed, reason)
            self.runner.finalize_job(plan, passed=True)

        self.assertTrue(plan.output_path.exists())
        self.assertTrue((self.watch / "_originals/good.mkv").exists())
        self.assertEqual(self.runner.status.completed_count, 1)


if __name__ == "__main__":
    unittest.main()
