"""Batch ZIP round trips and preservation on archival failures."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from runtime.workspace.archives import BatchArchives, archive_batch, restore_batch


class BatchArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.batch = self.root / "batches/batch_01"
        self.batch.mkdir(parents=True)
        (self.batch / "batch.json").write_bytes(b'{"batch_id":"batch_01"}\n')
        run = self.batch / "run_01"
        run.mkdir()
        (run / "state.jsonl").write_bytes(b'{"minerals":500}\n' * 100)
        (run / "match.SC2Replay").write_bytes(bytes(range(256)))
        self.original = {p.relative_to(self.batch): p.read_bytes() for p in self.batch.rglob("*") if p.is_file()}

    def test_round_trip_and_restored_cleanup_do_not_recompress(self):
        archives = BatchArchives()
        self.assertFalse(archives.finish(self.batch.parent))
        self.assertFalse(self.batch.exists())
        archive = self.batch.with_suffix(".zip")
        original_zip = archive.read_bytes()
        archives.restore(self.batch)
        self.assertEqual({p.relative_to(self.batch): p.read_bytes() for p in self.batch.rglob("*") if p.is_file()}, self.original)
        archives.restore(self.batch)
        with patch("runtime.workspace.archives.archive_batch", side_effect=AssertionError("Recompression")):
            self.assertFalse(archives.finish())
        self.assertFalse(self.batch.exists())
        self.assertEqual(archive.read_bytes(), original_zip)

    def test_failed_verification_keeps_source_and_does_not_publish_zip(self):
        with patch("runtime.workspace.archives.ZipFile.testzip", return_value="batch_01/run_01/state.jsonl"):
            errors = BatchArchives().finish(self.batch.parent)
        self.assertEqual(len(errors), 1)
        self.assertIn("verification", errors[0]["error"])
        self.assertEqual({p.relative_to(self.batch): p.read_bytes() for p in self.batch.rglob("*") if p.is_file()}, self.original)
        self.assertFalse(self.batch.with_suffix(".zip").exists())
        self.assertFalse(self.batch.with_suffix(".zip.tmp").exists())

    def test_invalid_archive_is_not_partially_restored(self):
        archive_batch(self.batch)
        for invalid in (False, True):
            with self.subTest(unsafe_path=invalid):
                with ZipFile(self.batch.with_suffix(".zip"), "w") as stored:
                    stored.writestr("../outside.txt" if invalid else "wrong_batch/state.jsonl", "fixture")
                with self.assertRaises(ValueError):
                    restore_batch(self.batch)
                self.assertFalse(self.batch.exists())
                self.assertFalse((self.root / "outside.txt").exists())
                self.assertFalse(list(self.batch.parent.glob(".batch_01-*")))

    def test_existing_source_directory_is_not_removed_as_a_restored_copy(self):
        archives = BatchArchives()
        archives.restore(self.batch)
        self.assertFalse(archives.finish())
        self.assertTrue(self.batch.is_dir())


if __name__ == "__main__":
    unittest.main()
