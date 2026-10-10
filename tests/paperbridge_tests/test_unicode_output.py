"""Synthetic PDF Unicode regression at the actual MCP output boundary."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from typing import Any
from unittest.mock import patch

from mcp.server.fastmcp.tools.base import Tool
from paperbridge.storage import DocumentStore, Rights


class UnicodeOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = DocumentStore(self.root/'store')
        self.rights = Rights(True, True, 'Original synthetic regression fixture')

    def ingest_pdf(self, text):
        path = self.root/'synthetic.pdf'
        path.write_bytes(b'%PDF-1.4\nOriginal synthetic fixture\n%%EOF\n')
        reader = types.SimpleNamespace(is_encrypted=False,
            pages=[types.SimpleNamespace(extract_text=lambda:text)])
        with patch('pypdf.PdfReader',return_value=reader):
            return self.store.ingest(path,title='Original Unicode fixture',
                url='https://doi.org/10.1234/synthetic',rights=self.rights)

    def call_tool(self, reference):
        def papers_get_text(reference: str) -> dict[str, Any]:
            return self.store.fetch(reference)
        tool = Tool.from_function(papers_get_text, structured_output=True)
        return asyncio.run(tool.run({'reference':reference},convert_result=True))

    def test_new_pdf_with_unpaired_surrogates_serializes_through_mcp(self):
        text = 'A mathematical symbol '+('\ud835 '*12)+'is partly unavailable.'
        result = self.ingest_pdf(text)
        reference = result['first_chunk_id']
        self.call_tool(reference)
        fetched = self.store.fetch(reference)
        self.assertEqual(fetched['text'],text.replace('\ud835','\ufffd'))
        self.assertEqual(fetched['metadata']['text_encoding_repairs']['unpaired_surrogates_replaced'],12)
        self.assertTrue(fetched['metadata']['text_encoding_repairs']['stored_source_text_unchanged'])
        self.assertEqual(fetched['metadata']['page_char_end'],len(text))

    def test_existing_cached_text_preserves_chunk_ids_offsets_and_database(self):
        result = self.ingest_pdf('Before')
        page = 'x'*5000+'before \ud835 after'
        with self.store._connect() as db:
            db.execute('UPDATE documents SET pages=? WHERE id=?',(json.dumps([page]),result['document_id']))
        before = hashlib.sha256(self.store.database.read_bytes()).hexdigest()
        reference = 'pb:'+result['document_id']+':1'
        self.call_tool(reference)
        fetched = self.store.fetch(reference)
        self.assertEqual(fetched['id'],reference)
        self.assertEqual(fetched['text'],'before \ufffd after')
        self.assertEqual(fetched['metadata']['chunk_index'],1)
        self.assertEqual(fetched['metadata']['chunk_count'],2)
        self.assertEqual(fetched['metadata']['page_char_start'],5000)
        self.assertEqual(fetched['metadata']['page_char_end'],len(page))
        self.assertIsNone(fetched['metadata']['next_id'])
        self.assertEqual(hashlib.sha256(self.store.database.read_bytes()).hexdigest(),before)
        with self.store._connect() as db:
            stored = db.execute('SELECT pages,original FROM documents WHERE id=?',(result['document_id'],)).fetchone()
        self.assertEqual(json.loads(stored['pages']),[page])
        self.assertEqual(hashlib.sha256(stored['original']).hexdigest(),result['document_id'])

    def test_valid_unicode_is_unchanged_and_has_no_repair_notice(self):
        text = 'İstanbul, café, α ∑ 𝑥 𝛼 🚀 漢字 e\u0301'
        result = self.ingest_pdf(text)
        self.call_tool(result['first_chunk_id'])
        fetched = self.store.fetch(result['first_chunk_id'])
        self.assertEqual(fetched['text'],text)
        self.assertNotIn('text_encoding_repairs',fetched['metadata'])

    def test_valid_surrogate_pair_in_existing_json_remains_non_bmp(self):
        result = self.ingest_pdf('Before')
        with self.store._connect() as db:
            db.execute('UPDATE documents SET pages=? WHERE id=?',
                ('["\\ud835\\udc65 mathematical x"]',result['document_id']))
        self.call_tool(result['first_chunk_id'])
        fetched = self.store.fetch(result['first_chunk_id'])
        self.assertEqual(fetched['text'],'𝑥 mathematical x')
        self.assertNotIn('text_encoding_repairs',fetched['metadata'])

    def test_isolated_low_surrogate_is_explicitly_replaced(self):
        result = self.ingest_pdf('Left \udc65 right')
        self.call_tool(result['first_chunk_id'])
        fetched = self.store.fetch(result['first_chunk_id'])
        self.assertEqual(fetched['text'],'Left \ufffd right')
        self.assertEqual(fetched['metadata']['text_encoding_repairs']['unpaired_surrogates_replaced'],1)


if __name__ == '__main__':
    unittest.main()
