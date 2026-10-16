from __future__ import annotations
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from paperbridge import PaperBridge,DocumentStore,Rights
from paperbridge.discovery import DiscoveryError
from paperbridge.ieee import IeeeBrowserProvider
from paperbridge.institutions import ItuProfile
from paperbridge.publishers import identify_platform,platform_coverage


class PublisherCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=DocumentStore(Path(self.temp.name)/'archive')
        self.discovery=Mock()
        self.paper={'doi':'10.1234/demo','id':'doi:10.1234/demo','title':'Synthetic source',
            'url':'https://doi.org/10.1234/demo','publisher_url':'https://onlinelibrary.wiley.com/doi/10.1234/demo',
            'publisher':'Wiley','content_level':'metadata_only'}
        self.discovery.lookup.return_value=self.paper
        self.discovery.open_access.return_value={'status':'not_configured'}

    def test_host_identification_distinguishes_platforms_in_publisher_family(self):
        nature=dict(self.paper,publisher='Springer Science and Business Media LLC',publisher_url='https://www.nature.com/articles/synthetic')
        self.assertEqual(identify_platform(nature),'nature')
        self.assertEqual(identify_platform(dict(nature,publisher_url='https://link.springernature.com/article/10.1234/demo')),'springer')
        self.assertEqual(identify_platform(dict(self.paper,publisher='Unknown',publisher_url='https://www.cambridge.org/core/product/identifier/synthetic')),'cambridge')

    def test_metadata_host_spoofs_and_names_do_not_become_known_platforms(self):
        for url in ['https://onlinelibrary.wiley.com.evil.test/x','https://evil.test/?url=https://onlinelibrary.wiley.com',
                    'http://onlinelibrary.wiley.com/x','https://user@onlinelibrary.wiley.com/x',
                    'https://onlinelibrary.wiley.com:999/x','https://onlinelibrary.wiley.com\\@evil.test/x']:
            with self.subTest(url=url):
                self.assertEqual(identify_platform({'publisher_url':url,'publisher':'Unknown'}),'unknown')
        self.assertEqual(identify_platform({'publisher':'A Wiley impersonator'}),'unknown')
        self.assertEqual(identify_platform({'publisher_url':None,'publisher':None}),'unknown')

    def test_legacy_ieee_crossref_name_and_url_remain_routable(self):
        paper=dict(self.paper, publisher='Institute of Electrical and Electronics Engineers (IEEE)',
                   publisher_url='http://ieeexplore.ieee.org/document/123456/')
        self.assertEqual(identify_platform(paper),'ieee')
        self.assertTrue(IeeeBrowserProvider.supports(paper))
        # A name alias classifies metadata; it cannot authorize an unrelated URL.
        self.assertFalse(IeeeBrowserProvider.supports(dict(paper,publisher_url='https://evil.test/document/123456/')))

    def test_search_keeps_unsupported_sources_and_discloses_no_reading(self):
        self.discovery.search.return_value=[self.paper]
        result=PaperBridge(self.store,self.discovery).search('synthetic')
        entry=result['results'][0]
        self.assertEqual(entry['id'],self.paper['id'])
        self.assertEqual(entry['access']['provider'],'wiley')
        self.assertEqual(entry['access']['retrieval'],'adapter_not_implemented')
        self.assertFalse(entry['access']['full_text_read_by_discovery'])
        self.assertFalse(entry['access']['figures_read_by_discovery'])
        self.discovery.lookup.assert_not_called()

    def test_cached_reading_survives_missing_publisher_adapter(self):
        f=Path(self.temp.name)/'original.txt';f.write_text('Original synthetic local source')
        self.store.ingest(f,title='Synthetic',url=self.paper['url'],rights=Rights(True,True,'Own fixture'))
        entry=PaperBridge(self.store,self.discovery).search('synthetic',local_only=True)['results'][0]
        self.assertEqual(entry['access']['retrieval'],'authorized_archive_available')
        self.assertTrue(entry['first_chunk_id'].startswith('pb:'))
        self.assertFalse(entry['access']['full_text_read_by_discovery'])
        self.assertEqual(self.discovery.mock_calls,[])

    def test_resolve_does_not_confuse_known_platform_with_implemented_adapter(self):
        result=PaperBridge(self.store,self.discovery,institution=ItuProfile()).resolve(self.paper['doi'])
        self.assertEqual(result['provider'],'wiley')
        self.assertEqual(result['subscription_retrieval'],'not_implemented')
        self.assertEqual(result['institution']['credential_kind'],'itu_library_account')
        self.assertFalse(result['institution']['login_automated'])
        self.assertEqual(result['institution']['entitlement'],'not_checked')

    def test_mismatched_resolve_identity_stops_before_oa_lookup(self):
        self.discovery.lookup.return_value=dict(self.paper,doi='10.1234/other')
        with self.assertRaises(DiscoveryError):
            PaperBridge(self.store,self.discovery).resolve(self.paper['doi'])
        self.discovery.open_access.assert_not_called()

    def test_unavailable_source_has_specific_advice_and_never_calls_wrong_browser(self):
        (self.store.root/'publisher-policy.json').write_text(json.dumps({'ieee':{'local_processing':True,'external_model':True,'basis':'Own fixture'}}))
        runner=Mock();provider=IeeeBrowserProvider(self.store,None,runner=runner)
        result=PaperBridge(self.store,self.discovery,provider=provider).get_text(self.paper['doi'])
        self.assertEqual(result['reason'],'publisher_not_supported')
        self.assertEqual(result['access']['provider'],'wiley')
        self.assertIn('another relevant source',result['next_action'])
        self.assertIsNone(result['text']);runner.run.assert_not_called()
        self.discovery.lookup.side_effect=DiscoveryError('Metadata unavailable')
        result=PaperBridge(self.store,self.discovery,provider=provider).get_text(self.paper['doi'])
        self.assertEqual(result['reason'],'metadata_lookup_failed')
        self.assertFalse(result['full_text_read'])
        runner.run.assert_not_called()

    def test_status_is_configuration_coverage_not_an_entitlement_claim(self):
        with patch.dict('os.environ',{'PAPERBRIDGE_BROWSER_ENGINE':'firefox'}):
            coverage={e['provider']:e for e in PaperBridge.status()['publisher_coverage']}
        self.assertEqual(coverage['springer']['adapter'],'configured_experimental')
        self.assertEqual(coverage['nature']['adapter'],'not_implemented')
        self.assertTrue(all(e['entitlement']=='not_checked' for e in coverage.values()))
        self.assertEqual({e['provider'] for e in platform_coverage([]) if e['adapter']=='not_configured'},
                         {'ieee','springer','asme','acs','cambridge','oxford','taylor_francis','sage','acm'})

    def test_legacy_public_pdf_flag_does_not_register_or_advertise_mdpi(self):
        policy = self.store.root / "publisher-policy.json"
        policy_bytes = json.dumps({"mdpi": {
            "local_processing": True, "external_model": True, "basis": "Own fixture"
        }}).encode()
        policy.write_bytes(policy_bytes)
        with patch.dict("os.environ", {"PAPERBRIDGE_MDPI_PUBLIC_PDF": "1"}, clear=True):
            bridge = PaperBridge(self.store, self.discovery)
            self.assertEqual(bridge.providers, [])
            status = bridge.status()
            self.assertNotIn("mdpi", status["configured_providers"])
            self.assertNotIn("mdpi_journal_scope", status)
            self.assertFalse(any("mdpi" in item for item in status["experimental"]))
            coverage = {item["provider"]: item for item in status["publisher_coverage"]}
            self.assertEqual(coverage["mdpi"]["adapter"], "not_implemented")
            result = bridge.get_text("10.3390/drones9040261")
            self.assertEqual(result["reason"], "publisher_retrieval_not_implemented")
            self.assertIsNone(result["text"])
        self.assertEqual(policy.read_bytes(), policy_bytes)
        self.discovery.lookup.assert_called_once_with('10.3390/drones9040261')
        self.discovery.open_access.assert_not_called()

    def test_mdpi_discovery_stays_visible_without_retrieval_or_rendering(self):
        paper = dict(self.paper, doi="10.3390/drones9040261",
                     id="doi:10.3390/drones9040261", publisher="MDPI AG",
                     url="https://doi.org/10.3390/drones9040261",
                     publisher_url="https://www.mdpi.com/2504-446X/9/4/261")
        self.discovery.search.return_value = [paper]
        self.discovery.lookup.return_value = paper
        (self.store.root / "publisher-policy.json").write_text(json.dumps({"ieee": {
            "local_processing": True, "external_model": True, "basis": "Own fixture"
        }}))
        runner = Mock()
        provider = IeeeBrowserProvider(self.store, None, runner=runner)
        with patch.dict("os.environ", {}, clear=True), patch.object(self.store, "get_page") as render:
            bridge = PaperBridge(self.store, self.discovery, provider=provider)
            result = bridge.search("synthetic")["results"][0]
            self.assertEqual(result["id"], paper["id"])
            self.assertEqual(result["access"]["provider"], "mdpi")
            self.assertEqual(result["access"]["retrieval"], "public_direct_link")
            self.assertFalse(result["access"]["full_text_read_by_discovery"])
            resolved = bridge.resolve(paper["doi"])
            self.assertEqual(resolved["subscription_retrieval"], "not_needed_for_public_link")
            self.assertNotIn("public_pdf_retrieval", resolved)
            for result in (bridge.get_text(paper["doi"]), bridge.get_page(paper["doi"], 1)):
                self.assertEqual(result["reason"], "open_access_use_direct_link")
                self.assertEqual(result["access"]["provider"], "mdpi")
                self.assertEqual(result["access"]["retrieval"], "public_direct_link")
                self.assertIsNone(result["text"])
            render.assert_not_called()
            runner.run.assert_not_called()

    def test_existing_mdpi_archive_keeps_original_and_current_permission_checks(self):
        import hashlib
        from paperbridge_tests.test_paperbridge import sample_pdf
        paper_url = "https://doi.org/10.3390/drones9040261"
        source = Path(self.temp.name) / "original.pdf"
        original = sample_pdf("Original synthetic archived source")
        source.write_bytes(original)
        saved = self.store.ingest(source, title="Original fixture", url=paper_url,
                                 rights=Rights(True, True, "Own fixture"))
        before = hashlib.sha256(self.store.database.read_bytes()).hexdigest()
        with patch.dict("os.environ", {"PAPERBRIDGE_MDPI_PUBLIC_PDF": "1"}, clear=True), \
                patch("paperbridge.rendering.render_page", return_value={"fixture": True}) as render:
            bridge = PaperBridge(self.store, self.discovery)
            first = bridge.get_text(paper_url)
            cached = bridge.get_text(saved["first_chunk_id"])
            self.assertEqual(first["id"], saved["first_chunk_id"])
            self.assertEqual(first, cached)
            self.assertIn("Original synthetic archived source", first["text"])
            page = bridge.get_page(paper_url, 1, 1600)
            self.assertEqual(page["id"], saved["first_chunk_id"])
            self.assertEqual(page["content_type"], "image/png")
            render.assert_called_once_with(original, 1, 1600)
            self.assertEqual(hashlib.sha256(self.store.database.read_bytes()).hexdigest(), before)
            self.assertEqual(self.discovery.mock_calls, [])
            self.store.ingest(source, title="Original fixture", url=paper_url,
                              rights=Rights(True, False, "Revoked fixture export"))
            render.reset_mock()
            for reference in (paper_url, saved["first_chunk_id"]):
                with self.assertRaises(PermissionError):
                    bridge.get_text(reference)
                with self.assertRaises(PermissionError):
                    bridge.get_page(reference, 1)
            render.assert_not_called()
