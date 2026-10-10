from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from paperbridge import DocumentStore, PaperBridge, Rights
from paperbridge_tests.test_paperbridge import sample_pdf


class StorageReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = DocumentStore(self.root / "papers")
        self.source = self.root / "original-fixture.pdf"
        self.source.write_bytes(sample_pdf("Original storage reuse fixture"))
        self.doi = "10.1234/storage-reuse"
        self.rights = Rights(True, True, "Original synthetic test document")
        self.document = self.ingest()

    def ingest(self):
        return self.store.ingest(
            self.source, title="Original storage reuse fixture",
            url="https://doi.org/" + self.doi, rights=self.rights)

    def persistent_files(self):
        return {path.relative_to(self.store.root).as_posix(): path.stat().st_size
                for path in self.store.root.rglob("*") if path.is_file()}

    def test_repeated_identical_pdf_ingestion_preserves_one_record_and_disk_size(self):
        baseline = self.persistent_files()
        for repetition in range(100):
            with self.subTest(repetition=repetition):
                self.assertEqual(self.ingest(), self.document)
                self.assertEqual(self.persistent_files(), baseline)
        with closing(sqlite3.connect(self.store.database)) as database:
            count = database.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        self.assertEqual(count, 1)

    def test_repeated_cached_text_reads_leave_archive_unchanged(self):
        baseline = self.persistent_files()
        database_bytes = self.store.database.read_bytes()
        discovery, provider = Mock(), Mock()
        with patch.dict("os.environ", {}, clear=True):
            bridge = PaperBridge(DocumentStore(self.store.root), discovery,
                                 provider=provider)
        for repetition in range(100):
            with self.subTest(repetition=repetition):
                results = (bridge.get_text(self.doi),
                           bridge.fetch("doi:" + self.doi),
                           bridge.fetch(self.document["first_chunk_id"]))
                for result in results:
                    self.assertEqual(result["id"], self.document["first_chunk_id"])
                    self.assertIn("Original storage reuse fixture", result["text"])
                self.assertEqual(self.persistent_files(), baseline)
        self.assertEqual(self.store.database.read_bytes(), database_bytes)
        self.assertEqual(discovery.mock_calls, [])
        self.assertEqual(provider.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
