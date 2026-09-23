import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import recognize_wav
from src.job_store import PipelineStore


class RecognizeWavTests(unittest.TestCase):
    def test_manual_wav_is_registered_as_audio_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PipelineStore(Path(directory) / "pipeline.sqlite3")
            store.initialize()
            info = {"sha256": "a" * 64, "size_bytes": 7, "duration_seconds": 1.0}
            source_id, created = store.enqueue_manual_wav(
                "source-hash", "prep-hash", "input.wav", Path("/tmp/input.wav"), info,
            )
            self.assertTrue(created)
            source = store.source(source_id)
        self.assertEqual(source["stage"], "audio_ready")
        self.assertEqual(source["wav_sha256"], info["sha256"])

    def test_short_input_name_uses_pipeline_audio_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            audio_dir = Path(directory) / "audio"
            audio_dir.mkdir()
            expected = audio_dir / "input.wav"
            expected.write_bytes(b"fixture")
            with patch.object(recognize_wav, "AUDIO_DIR", audio_dir):
                self.assertEqual(recognize_wav.resolve_input(Path("input.wav")), expected)

    def test_input_registers_audio_ready_source(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "input.wav"
            audio.write_bytes(b"fixture")
            info = {"sha256": "a" * 64, "size_bytes": 7, "duration_seconds": 1.0}
            with patch.object(recognize_wav, "inspect_audio", return_value=info), \
                    patch.object(recognize_wav, "copy_into_pipeline", return_value=Path("/tmp/manual.wav")), \
                    patch.object(recognize_wav, "PipelineStore") as store_type:
                store_type.return_value.enqueue_manual_wav.return_value = (9, True)
                self.assertEqual(recognize_wav.main(["-i", str(audio)]), 0)
        store = store_type.return_value
        store.initialize.assert_called_once()
        store.enqueue_manual_wav.assert_called_once()

    def test_force_uses_a_new_scanner_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "input.wav"
            audio.write_bytes(b"fixture")
            info = {"sha256": "a" * 64, "size_bytes": 7, "duration_seconds": 1.0}
            with patch.object(recognize_wav, "inspect_audio", return_value=info), \
                    patch.object(recognize_wav, "copy_into_pipeline", return_value=Path("/tmp/manual.wav")), \
                    patch.object(recognize_wav, "PipelineStore") as store_type, \
                    patch.object(recognize_wav.uuid, "uuid4") as new_uuid:
                new_uuid.return_value.hex = "forced-run"
                store_type.return_value.enqueue_manual_wav.return_value = (10, True)
                self.assertEqual(recognize_wav.main(["-i", str(audio), "--force"]), 0)
        args = store_type.return_value.enqueue_manual_wav.call_args.args
        self.assertIn(":scope-run:forced-run", args[1])


if __name__ == "__main__":
    unittest.main()
