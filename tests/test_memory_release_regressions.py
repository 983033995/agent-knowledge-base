from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import akb


class ReleaseRegressions(unittest.TestCase):
    def test_ingest_mtime_is_rechecked_inside_write_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"AKB_VAULT": directory, "AKB_DISTILLER_AUTOSTART": "0"}):
                akb._CONFIG_CACHE = None
                started = threading.Event()
                release = threading.Event()
                result = []
                original = akb.write_note
                transcript = Path(directory) / "transcript.jsonl"
                transcript.write_text('{"role":"user","content":"旧正文"}\n', encoding="utf-8")

                def pause_old(*args, **kwargs):
                    if threading.current_thread().name == "old-ingest":
                        started.set()
                        self.assertTrue(release.wait(3))
                    return original(*args, **kwargs)

                def old_ingest():
                    result.append(akb.ingest_transcript(source="codex", transcript=str(transcript), session_id="stable"))

                with patch.object(akb, "write_note", side_effect=pause_old):
                    worker = threading.Thread(target=old_ingest, name="old-ingest")
                    worker.start()
                    self.assertTrue(started.wait(3))
                    transcript.write_text('{"role":"user","content":"更新正文"}\n', encoding="utf-8")
                    info = transcript.stat()
                    os.utime(transcript, ns=(info.st_atime_ns, info.st_mtime_ns + 10_000_000))
                    target = akb.ingest_transcript(source="codex", transcript=str(transcript), session_id="stable")
                    newest_mtime = akb.read_note(target)[0]["transcript_mtime_ns"]
                    release.set()
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                metadata, body = akb.read_note(target)
                self.assertIn("更新正文", body)
                self.assertNotIn("旧正文", body)
                self.assertEqual(metadata["transcript_mtime_ns"], newest_mtime)
                akb._CONFIG_CACHE = None
                akb._CONFIG_CACHE_KEY = None


if __name__ == "__main__":
    unittest.main()
