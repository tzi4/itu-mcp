"""Accurate provider attempts and bounded retries for original page fixtures."""
from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from paperbridge import DocumentStore, PaperBridge, Rights
from paperbridge.browser import RetrievalUnavailable
from paperbridge.discovery import DiscoveryError
from paperbridge.identifiers import doi_url
from paperbridge.rendering import PageRenderUnavailable
from paperbridge_tests.test_paperbridge import sample_pdf


class ServiceReliabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = DocumentStore(self.root / 'archive')
        self.doi = '10.1234/service-fixture'
        self.discovery = Mock()
        self.discovery.lookup.side_effect = self.paper
        self.discovery.open_access.return_value = {'status': 'not_configured'}
        self.provider = Mock()
        self.provider.name = 'ieee'
        self.provider.supports.return_value = True
        self.provider.retrieve.side_effect = RetrievalUnavailable('publisher_interaction_required')
        with patch.dict('os.environ', {}, clear=True):
            self.bridge = PaperBridge(self.store, self.discovery, provider=self.provider)

    def paper(self, doi):
        return {'doi': doi, 'id': 'doi:' + doi, 'title': 'Original service fixture',
                'url': doi_url(doi), 'publisher': 'IEEE', 'content_level': 'metadata_only',
                'publisher_url': 'https://ieeexplore.ieee.org/document/123456/'}

    def selections(self):
        return [{'reference': reference, 'page': number} for number, reference in enumerate(
            (self.doi, 'doi:' + self.doi.upper(), 'https://doi.org/' + self.doi), 1)]

    def acquire(self, paper):
        """Persist two original pages using the normal archive, with no network."""
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter()
        for number in (1, 2):
            document = PdfReader(io.BytesIO(sample_pdf(f'Original fixture page {number}')))
            writer.add_page(document.pages[0])
        source = self.root / 'original-fixture.pdf'
        with source.open('wb') as output:
            writer.write(output)
        stored = self.store.ingest(source, title=paper['title'], url=paper['url'],
                                   rights=Rights(True, True, 'Original synthetic test document'))
        result = self.store.fetch(stored['first_chunk_id'])
        result['metadata'].update(retrieval='synthetic_provider', institutional_entitlement='not_verified')
        return result

    def test_failed_provider_attempt_preserves_reason_without_claiming_authentication(self):
        for reason in ('publisher_interaction_required', 'browser_busy', 'browser_operation_timeout'):
            with self.subTest(reason=reason):
                self.provider.retrieve.reset_mock()
                self.provider.retrieve.side_effect = RetrievalUnavailable(reason)
                result = self.bridge.get_text(self.doi)
                self.provider.retrieve.assert_called_once_with(self.paper(self.doi))
                self.assertEqual(result['status'], 'full_text_unavailable')
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['access']['provider'], 'ieee')
                self.assertEqual(result['access']['retrieval'], 'provider_attempt_failed')
                self.assertEqual(result['access']['entitlement'], 'not_checked')
                self.assertFalse(result['access']['full_text_read_by_discovery'])
                self.assertFalse(result['full_text_read'])
                self.assertIsNone(result['text'])

    def test_permission_denial_prevents_metadata_and_provider_attempt(self):
        self.provider.rights.side_effect = RetrievalUnavailable('document_permission_required')
        result = self.bridge.get_text(self.doi)
        self.assertEqual(result['reason'], 'document_permission_required')
        self.assertEqual(result['access']['retrieval'], 'not_attempted')
        self.discovery.lookup.assert_not_called()
        self.provider.retrieve.assert_not_called()

    def test_selected_provider_denial_does_not_become_a_retrieval_attempt(self):
        other = Mock()
        other.name = 'springer'
        other.supports.return_value = False
        self.provider.rights.side_effect = RetrievalUnavailable('publisher_permission_required')
        self.bridge.providers.append(other)
        result = self.bridge.get_text(self.doi)
        self.assertEqual(result['reason'], 'publisher_permission_required')
        self.assertEqual(result['access']['retrieval'], 'configured_not_attempted')
        self.provider.retrieve.assert_not_called()
        other.retrieve.assert_not_called()

    def test_metadata_failure_does_not_become_a_provider_attempt(self):
        self.discovery.lookup.side_effect = DiscoveryError('Synthetic metadata failure')
        result = self.bridge.get_text(self.doi)
        self.assertEqual(result['reason'], 'metadata_lookup_failed')
        self.assertEqual(result['access']['retrieval'], 'not_attempted')
        self.provider.retrieve.assert_not_called()

    def test_metadata_identity_failure_does_not_enter_a_provider(self):
        self.discovery.lookup.side_effect = None
        self.discovery.lookup.return_value = self.paper('10.1234/different-source')
        result = self.bridge.get_text(self.doi)
        self.assertEqual(result['reason'], 'metadata_identity_not_verified')
        self.assertEqual(result['access']['retrieval'], 'configured_not_attempted')
        self.provider.retrieve.assert_not_called()

    def test_discovery_keeps_non_retrieval_reporting(self):
        self.discovery.search.return_value = [self.paper(self.doi)]
        search = self.bridge.search('original fixture')
        resolved = self.bridge.resolve(self.doi)
        self.assertEqual(search['results'][0]['access']['retrieval'], 'configured_not_attempted')
        self.assertEqual(resolved['access']['retrieval'], 'configured_not_attempted')
        self.provider.retrieve.assert_not_called()

    def test_fresh_and_cached_text_keep_their_provenance(self):
        self.provider.retrieve.side_effect = self.acquire
        fresh = self.bridge.get_text(self.doi)
        baseline = self.store.database.read_bytes()
        cached = self.bridge.get_text('https://doi.org/' + self.doi)
        self.assertEqual(fresh['id'], cached['id'])
        self.assertEqual(fresh['metadata']['retrieval'], 'synthetic_provider')
        self.assertEqual(fresh['metadata']['institutional_entitlement'], 'not_verified')
        self.assertEqual(cached['metadata']['storage'], 'local_archive')
        self.assertNotIn('retrieval', cached['metadata'])
        self.assertEqual(fresh['text'], cached['text'])
        self.assertEqual(self.store.database.read_bytes(), baseline)
        self.provider.retrieve.assert_called_once()
        self.discovery.lookup.assert_called_once_with(self.doi)

    def test_doi_aliases_share_one_failure_with_individual_coverage(self):
        selections = self.selections()
        result = self.bridge.get_pages(selections)
        self.provider.retrieve.assert_called_once()
        self.discovery.lookup.assert_called_once_with(self.doi)
        self.assertEqual(result['prepared_count'], 0)
        self.assertEqual(result['requested_count'], 3)
        for index, entry in enumerate(result['selection_coverage']):
            self.assertEqual(entry['selection_index'], index)
            self.assertEqual(entry['reference'], selections[index]['reference'])
            self.assertEqual(entry['page'], selections[index]['page'])
            self.assertEqual(entry['status'], 'full_text_unavailable')
            self.assertEqual(entry['reason'], 'publisher_interaction_required')
        self.assertFalse(result['entire_papers_read'])
        self.assertEqual(self.store.search('fixture'), [])

    def test_other_doi_is_attempted_independently_with_its_own_failure(self):
        other = '10.1234/other-source'
        self.provider.retrieve.side_effect = [RetrievalUnavailable('publisher_interaction_required'),
                                              RetrievalUnavailable('browser_busy')]
        result = self.bridge.get_pages([
            {'reference': self.doi, 'page': 1}, {'reference': 'doi:' + self.doi, 'page': 2},
            {'reference': other, 'page': 1}, {'reference': 'https://doi.org/' + other, 'page': 2}])
        self.assertEqual(self.provider.retrieve.call_count, 2)
        self.assertEqual([call.args[0]['doi'] for call in self.provider.retrieve.call_args_list],
                         [self.doi, other])
        self.assertEqual([entry['reason'] for entry in result['selection_coverage']],
                         ['publisher_interaction_required'] * 2 + ['browser_busy'] * 2)

    def test_new_explicit_batch_can_retry_after_access_changes(self):
        failed = self.bridge.get_pages(self.selections())
        self.assertEqual(failed['prepared_count'], 0)
        self.provider.retrieve.assert_called_once()
        self.provider.retrieve.side_effect = self.acquire
        succeeded = self.bridge.get_pages([{'reference': 'doi:' + self.doi, 'page': 1}], max_edge=800)
        self.assertEqual(succeeded['prepared_count'], 1)
        self.assertEqual(self.provider.retrieve.call_count, 2)
        self.assertTrue(succeeded['pages'][0]['image_bytes'].startswith(b'\x89PNG\r\n\x1a\n'))

    def test_pre_provider_failures_are_also_reused_within_one_batch(self):
        self.discovery.lookup.side_effect = DiscoveryError('Synthetic metadata failure')
        result = self.bridge.get_pages(self.selections())
        self.discovery.lookup.assert_called_once_with(self.doi)
        self.provider.retrieve.assert_not_called()
        self.assertEqual([entry['reason'] for entry in result['selection_coverage']],
                         ['metadata_lookup_failed'] * 3)

    def test_bad_page_does_not_suppress_a_valid_page_after_acquisition(self):
        self.provider.retrieve.side_effect = self.acquire
        result = self.bridge.get_pages([{'reference': self.doi, 'page': 3},
                                        {'reference': 'doi:' + self.doi, 'page': 1}], max_edge=800)
        self.assertEqual([entry['status'] for entry in result['selection_coverage']],
                         ['invalid_or_unknown_reference', 'image_prepared'])
        self.provider.retrieve.assert_called_once()
        self.assertEqual(result['pages'][0]['page'], 1)

    def test_render_failure_does_not_suppress_another_page(self):
        self.provider.retrieve.side_effect = self.acquire
        image = {'image_bytes': b'\x89PNG\r\n\x1a\n', 'width': 800, 'height': 400}
        with patch('paperbridge.rendering.render_page',
                   side_effect=[PageRenderUnavailable('Original fixture render failure'), image]) as render:
            result = self.bridge.get_pages([{'reference': self.doi, 'page': 1},
                                            {'reference': 'doi:' + self.doi, 'page': 2}])
        self.assertEqual(render.call_count, 2)
        self.assertEqual([entry['status'] for entry in result['selection_coverage']],
                         ['page_image_unavailable', 'image_prepared'])
        self.provider.retrieve.assert_called_once()
        self.assertEqual(result['pages'][0]['page'], 2)

    def test_successful_batch_acquires_once_and_preserves_duplicate_handling(self):
        self.provider.retrieve.side_effect = self.acquire
        with patch.object(self.store, 'ingest', wraps=self.store.ingest) as ingest:
            result = self.bridge.get_pages([
                {'reference': self.doi, 'page': 1}, {'reference': 'doi:' + self.doi, 'page': 2},
                {'reference': 'https://doi.org/' + self.doi, 'page': 1}], max_edge=800)
        ingest.assert_called_once()
        self.provider.retrieve.assert_called_once()
        self.assertEqual(result['prepared_count'], 2)
        self.assertEqual([entry['status'] for entry in result['selection_coverage']],
                         ['image_prepared', 'image_prepared', 'duplicate_page'])
        self.assertEqual([page['page'] for page in result['pages']], [1, 2])
        with closing(sqlite3.connect(self.store.database)) as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 1)

    def test_archive_permission_is_checked_for_each_selected_page(self):
        acquired = self.acquire(self.paper(self.doi))
        original = self.store.get_page
        def revoke_after_page(*args):
            result = original(*args)
            with self.store._connect() as database:
                denied = json.dumps({'local_processing': True, 'external_model': False,
                                     'basis': 'Synthetic revocation between page selections'})
                database.execute('UPDATE documents SET rights=?', (denied,))
            return result
        with patch.object(self.store, 'get_page', side_effect=revoke_after_page):
            result = self.bridge.get_pages([{'reference': self.doi, 'page': 1},
                                            {'reference': acquired['id'], 'page': 2}], max_edge=800)
        self.assertEqual([entry['status'] for entry in result['selection_coverage']],
                         ['image_prepared', 'external_model_permission_required'])
        self.provider.retrieve.assert_not_called()
        self.discovery.lookup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
