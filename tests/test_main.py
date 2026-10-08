"""Regression tests for the local, CPU-only media workflow."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class LocalMediaWorkflowTests(unittest.TestCase):
    def test_discovers_only_supported_files_in_stable_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            input_dir = Path(temporary)
            (input_dir / "Lecture 02.MKV").touch()
            (input_dir / "notes.txt").touch()
            nested = input_dir / "week 1"
            nested.mkdir()
            (nested / "Lecture #01.mp4").touch()

            with patch.object(main, "INPUT_DIR", input_dir):
                files = main.list_local_media_files()

            self.assertEqual(
                [file.relative_to(input_dir) for file in files],
                [Path("Lecture 02.MKV"), Path("week 1/Lecture #01.mp4")],
            )

    def test_cpu_and_fp16_invariants_are_explicit(self) -> None:
        self.assertEqual(main.get_device(), "cpu")

        class RecordingModel:
            def __init__(self) -> None:
                self.options: dict[str, object] | None = None

            def transcribe(self, _segment: str, **options: object) -> dict[str, str]:
                self.options = options
                return {"text": "test"}

        with tempfile.TemporaryDirectory() as temporary:
            partial = Path(temporary) / "result.partial.txt"
            model = RecordingModel()
            main.transcribe_segments(model, [Path("segment.wav")], "fr", partial)

        self.assertEqual(model.options, {"verbose": False, "fp16": False, "language": "fr"})

    def test_ffmpeg_creates_pcm_segments_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "Lecture #1 with spaces.wav"
            temp_dir = workspace / "temp"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=1000:duration=61", "-ac", "2", "-ar", "44100",
                    str(source),
                ],
                check=True,
            )
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

            with patch.object(main, "TEMP_DIR", temp_dir):
                segments = main.extract_audio_segments("local", source)
                self.assertEqual(len(segments), 2)
                probe = subprocess.run(
                    [
                        "ffprobe", "-v", "error", "-select_streams", "a:0",
                        "-show_entries", "stream=channels,sample_rate,codec_name",
                        "-of", "default=noprint_wrappers=1", str(segments[0]),
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertIn("codec_name=pcm_s16le", probe.stdout)
                self.assertIn("sample_rate=16000", probe.stdout)
                self.assertIn("channels=1", probe.stdout)
                main.clean_temp()

            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
            self.assertEqual(list(temp_dir.glob("segment_*.wav")), [])

    def test_output_paths_never_overwrite_existing_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            export_dir = Path(temporary)
            (export_dir / "course.txt").touch()
            with patch.object(main, "EXPORT_DIR", export_dir):
                final, partial = main.output_paths("course")

            self.assertNotEqual(final, export_dir / "course.txt")
            self.assertEqual(partial.suffix, ".txt")
            self.assertTrue(partial.name.endswith(".partial.txt"))


if __name__ == "__main__":
    unittest.main()
