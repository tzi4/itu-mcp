"""Public paper links must not enter institutional PDF acquisition."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from paperbridge import DocumentStore, PaperBridge
from paperbridge.discovery import DiscoveryError, MetadataClient


class PublicAccessRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = DocumentStore(Path(temporary.name) / 'archive')
        self.discovery = Mock()
        self.discovery.open_access.return_value = {'status': 'not_configured'}
        self.provider = Mock()
        self.provider.name = 'springer'
        self.provider.supports.return_value = True
        self.institution = Mock()
        with patch.dict('os.environ', {}, clear=True):
            self.bridge = PaperBridge(self.store, self.discovery, provider=self.provider,
                                      institution=self.institution)
        self.paper = self.source('springer', open_access=True)
        self.discovery.lookup.return_value = self.paper
        self.discovery.search.return_value = [self.paper]

    @staticmethod
    def source(provider, *, open_access):
        doi, url = ('10.1234/public-source', 'https://link.springer.com/article/10.1234/public-source')
        if provider == 'ieee':
            doi, url = '10.1109/public-source', 'https://ieeexplore.ieee.org/document/123456/'
        return MetadataClient._paper({
            'DOI': doi, 'title': ['Synthetic research paper'], 'publisher': provider,
            'resource': {'primary': {'URL': url}},
            'license': [{'URL': 'https://creativecommons.org/licenses/by/4.0/',
                         'content-version': 'vor',
                         'start': {'date-time': '2024-01-01T00:00:00Z'}}] if open_access else [],
        })

    def archive_hash(self):
        return hashlib.sha256(self.store.database.read_bytes()).hexdigest()

    def assert_public(self, result):
        self.assertEqual(result['status'], 'public_access_available')
        self.assertEqual(result['reason'], 'open_access_use_direct_link')
        self.assertIsNone(result['text'])
        self.assertFalse(result['full_text_read'])
        self.assertFalse(result['figures_read'])
        self.assertFalse(result['downloaded'])
        self.assertTrue(result['open_access']['links'])
        self.assertIn('ordinary web tools', result['next_action'])
        self.assertIn('Do not retry', result['next_action'])

    def test_ieee_and_springer_public_text_fetch_and_page_do_not_acquire_or_render(self):
        before = self.archive_hash()
        with patch.object(self.store, 'get_page') as render:
            for provider in ('ieee', 'springer'):
                self.provider.name = provider
                self.discovery.lookup.return_value = paper = self.source(provider, open_access=True)
                for call, reference in ((self.bridge.get_text, paper['doi']),
                                        (self.bridge.fetch, paper['id']),
                                        (lambda ref: self.bridge.get_page(ref, 1), paper['doi'])):
                    with self.subTest(provider=provider, reference=reference):
                        self.assert_public(call(reference))
            render.assert_not_called()
        self.provider.retrieve.assert_not_called()
        self.institution.assert_not_called()
        self.discovery.open_access.assert_not_called()
        self.assertEqual(self.archive_hash(), before)

    def test_search_and_resolve_return_public_links_without_institution_login_advice(self):
        entry = self.bridge.search('synthetic')['results'][0]
        self.assertEqual(entry['access']['retrieval'], 'public_direct_link')
        self.assertTrue(entry['access']['open_access']['links'])
        self.assertFalse(entry['access']['full_text_read_by_discovery'])
        resolved = self.bridge.resolve(self.paper['doi'])
        self.assert_public(resolved)
        self.assertEqual(resolved['subscription_retrieval'], 'not_needed_for_public_link')
        self.assertIsNone(resolved['institution'])
        self.institution.entry_points.assert_not_called()
        self.provider.retrieve.assert_not_called()

    def test_public_doi_aliases_share_one_lookup_and_keep_links_in_batch_coverage(self):
        doi = self.paper['doi']
        selections = [{'reference': reference, 'page': page} for page, reference in enumerate(
            (doi, 'doi:' + doi.upper(), 'https://doi.org/' + doi), 1)]
        result = self.bridge.get_pages(selections)
        self.assertEqual(result['prepared_count'], 0)
        self.assertEqual(result['pages'], [])
        self.discovery.lookup.assert_called_once_with(doi)
        for entry in result['selection_coverage']:
            self.assertEqual(entry['status'], 'public_access_available')
            self.assertTrue(entry['open_access']['links'])
            self.assertIn('ordinary web tools', entry['next_action'])
        self.provider.retrieve.assert_not_called()

    def test_unpaywall_public_location_prevents_provider_entry(self):
        self.discovery.lookup.return_value = self.source('springer', open_access=False)
        self.discovery.open_access.return_value = {
            'status': 'found', 'links': {'url': 'https://repository.example.edu/paper'},
            'evidence': 'unpaywall', 'downloaded': False,
        }
        self.assert_public(self.bridge.get_text(self.paper['doi']))
        self.provider.retrieve.assert_not_called()
        self.discovery.open_access.assert_called_once_with(self.paper['doi'])

    def test_missing_or_unavailable_oa_metadata_does_not_claim_a_paywall(self):
        self.discovery.lookup.return_value = self.source('springer', open_access=False)
        expected = {'status': 'full_text_unavailable', 'reason': 'synthetic_provider_result'}
        self.provider.retrieve.return_value = expected
        for result in ({'status': 'not_found'}, {'status': 'not_configured'},
                       {'status': 'found', 'links': {}}, DiscoveryError('Synthetic API failure')):
            with self.subTest(result=result):
                self.discovery.open_access.side_effect = result if isinstance(result, Exception) else None
                self.discovery.open_access.return_value = result
                self.assertEqual(self.bridge.get_text(self.paper['doi']), expected)
        self.assertEqual(self.provider.retrieve.call_count, 4)

    def test_requests_without_browser_still_return_public_links(self):
        with patch.dict('os.environ', {}, clear=True):
            bridge = PaperBridge(self.store, self.discovery)
        self.assert_public(bridge.fetch(self.paper['id']))
        self.assert_public(bridge.get_text(self.paper['doi']))
        with patch.object(self.store, 'get_page') as render:
            self.assert_public(bridge.get_page(self.paper['doi'], 1))
            render.assert_not_called()
        self.provider.retrieve.assert_not_called()


if __name__ == '__main__':
    unittest.main()
