import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from paperbridge import DocumentStore, PaperBridge, Rights
from paperbridge.rendering import PageRenderUnavailable, render_page
from paperbridge_tests.test_paperbridge import sample_pdf


def visual_pdf(text=True):
    writer = PdfWriter()
    if text:
        page = writer.add_page(PdfReader(io.BytesIO(sample_pdf())).pages[0])
        drawing = page.get_contents().get_data()
    else:
        page = writer.add_blank_page(width=300, height=150)
        drawing = b''
    content = DecodedStreamObject()
    content.set_data(drawing + b'\nq 1 0 0 rg 200 20 40 40 re f Q')
    page[NameObject('/Contents')] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class PageImagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DocumentStore(self.root / 'papers')
        self.pdf = self.root / 'visual.pdf'
        self.pdf.write_bytes(visual_pdf())
        self.doc = self.ingest(True)

    def ingest(self, external):
        return self.store.ingest(self.pdf, title='Synthetic visual paper',
            url='https://doi.org/10.1234/visual', rights=Rights(True, external, 'Own synthetic drawing'))

    def test_original_vector_shape_is_preserved_in_png(self):
        result = self.store.get_page(self.doc['first_chunk_id'], 1, 900)
        image = Image.open(io.BytesIO(result['image_bytes'])).convert('RGB')
        self.assertEqual(image.size, (900, 450))
        self.assertEqual(image.getpixel((660, 330)), (255, 0, 0))
        self.assertEqual(result['content_level'], 'pdf_page_image')
        self.assertEqual(result['url'], 'https://doi.org/10.1234/visual')

    def test_image_only_pdf_can_be_viewed_without_claiming_text(self):
        self.pdf.write_bytes(visual_pdf(text=False))
        document = self.ingest(True)
        text = self.store.fetch(document['first_chunk_id'])
        self.assertEqual(text['reason'], 'no_extractable_text')
        self.assertIsNone(text['text'])
        self.assertTrue(self.store.get_page(document['first_chunk_id'], 1)['image_bytes'])

    def test_revocation_blocks_render_before_original_bytes_leave_store(self):
        self.ingest(False)
        with patch('paperbridge.rendering.render_page') as render:
            with self.assertRaises(PermissionError):
                self.store.get_page(self.doc['first_chunk_id'], 1)
        render.assert_not_called()

    def test_bad_page_cannot_trigger_discovery_or_download(self):
        provider, discovery = Mock(), Mock()
        bridge = PaperBridge(self.store, discovery, provider=provider)
        for page in [True, 0, 201, 1.5]:
            with self.subTest(page=page), self.assertRaises(ValueError):
                bridge.get_page('10.1109/unknown', page)
        provider.retrieve.assert_not_called()
        discovery.lookup.assert_not_called()
        with self.assertRaises(ValueError):
            self.store.get_page(self.doc['first_chunk_id'], 2)

    def test_doi_uses_cached_pdf_without_network(self):
        discovery = Mock()
        result = PaperBridge(self.store, discovery).get_page('10.1234/visual', 1)
        self.assertTrue(result['image_bytes'])
        discovery.lookup.assert_not_called()

    def test_worker_does_not_inherit_secrets_and_sanitizes_timeout(self):
        with patch.dict(os.environ, {'NINOVA_PASSWORD': 'synthetic-secret'}), \
                patch('paperbridge.rendering.subprocess.run',
                      side_effect=subprocess.TimeoutExpired('synthetic-secret', 25)) as run:
            with self.assertRaises(PageRenderUnavailable) as error:
                render_page(visual_pdf(), 1)
        self.assertNotIn('synthetic-secret', str(error.exception))
        self.assertNotIn('NINOVA_PASSWORD', run.call_args.kwargs['env'])
        self.assertIn('-I', run.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
