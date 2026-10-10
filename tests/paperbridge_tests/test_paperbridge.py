from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock

from paperbridge import DocumentStore, PaperBridge, Rights
from paperbridge.discovery import DiscoveryError, MetadataClient
from paperbridge.identifiers import doi_url, normalize_doi
from paperbridge.institutions import ItuProfile


def sample_pdf(text="Rocket landing demo"):
    """Small valid PDF with one actual text page; no third-party fixture content."""
    stream = b"BT /F1 12 Tf 40 80 Td (" + text.encode("ascii") + b") Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 150] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    data, offsets = b"%PDF-1.4\n", [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return data + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DocumentStore(self.root / "state")

    def ingest(self, text="rocket landing", external=True, suffix=".txt"):
        path = self.root / ("input" + suffix)
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        return self.store.ingest(path, title="Demo document", url="https://example.org/demo",
                                 rights=Rights(True, external, "Own test fixture"))

    def test_fetch_exact_text_and_provenance(self):
        document = self.ingest()
        result = self.store.fetch(document["first_chunk_id"])
        self.assertEqual(result["text"], "rocket landing")
        self.assertEqual(result["metadata"]["page"], 1)
        self.assertTrue(result["metadata"]["untrusted_external_content"])
        self.assertTrue(result["metadata"]["entire_extracted_text_in_this_response"])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_chunks_reconstruct_entire_document_without_loss(self):
        text = "guidance landing Ω\n" * 700
        document = self.ingest(text)
        parts, current = [], document["first_chunk_id"]
        while current:
            result = self.store.fetch(current)
            self.assertLessEqual(len(result["text"]), 5000)
            self.assertFalse(result["metadata"]["entire_extracted_text_in_this_response"])
            parts.append(result["text"])
            current = result["metadata"]["next_id"]
        self.assertEqual("".join(parts), text)

    def test_private_content_not_searchable_or_exportable(self):
        document = self.ingest(external=False)
        self.assertEqual(self.store.search("rocket"), [])
        with self.assertRaises(PermissionError):
            self.store.fetch(document["first_chunk_id"])

    def test_rights_revocation_applies_to_existing_id(self):
        document = self.ingest(external=True)
        self.ingest(external=False)
        with self.assertRaises(PermissionError):
            self.store.fetch(document["first_chunk_id"])

    def test_local_permission_required_before_read(self):
        with self.assertRaises(PermissionError):
            self.store.ingest(self.root / "nonexistent", title="x", url="https://example.org",
                               rights=Rights(False, False, "Not authorized"))

    def test_cannot_fetch_arbitrary_path(self):
        for identifier in ["../../etc/passwd", "file:///etc/passwd", "pb:" + "a" * 64 + ":-1"]:
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.store.fetch(identifier)

    def test_pdf_extraction(self):
        document = self.ingest(sample_pdf(), suffix=".pdf")
        self.assertIn("Rocket landing demo", self.store.fetch(document["first_chunk_id"])["text"])

    def test_html_login_page_is_not_pdf(self):
        with self.assertRaises(ValueError):
            self.ingest(b"<html>Sign in</html>", suffix=".pdf")

    def test_blank_text_rejected(self):
        with self.assertRaises(ValueError):
            self.ingest("   \n")

    def test_missing_chunk_rejected(self):
        document = self.ingest()
        with self.assertRaises(LookupError):
            self.store.fetch(f"pb:{document['document_id']}:999")

    def test_no_network_for_local_fetch(self):
        document = self.ingest()
        discovery = Mock()
        bridge = PaperBridge(self.store, discovery)
        bridge.fetch(document["first_chunk_id"])
        bridge.search("rocket", local_only=True)
        self.assertEqual(discovery.mock_calls, [])

    def test_search_failure_retains_local_results(self):
        self.ingest()
        discovery = Mock()
        discovery.search.side_effect = DiscoveryError("Unavailable")
        result = PaperBridge(self.store, discovery).search("rocket")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["warnings"], ["Unavailable"])
        self.assertEqual(result["metadata_discovery"], "unavailable")

    def test_long_local_paper_does_not_prevent_metadata_discovery(self):
        self.ingest("rocket landing guidance " * 5000)
        discovery = Mock()
        discovery.search.return_value = [{"id": "doi:10.1234/new", "title": "New paper",
            "url": "https://doi.org/10.1234/new", "content_level": "metadata_only",
            "full_text_read": False}]
        result = PaperBridge(self.store, discovery).search("rocket landing", limit=10)
        discovery.search.assert_called_once_with("rocket landing", 10)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["id"], "doi:10.1234/new")
        self.assertEqual(result["metadata_discovery"], "completed")
        self.assertTrue(all(not entry["full_text_read"] for entry in result["results"]))

    def test_search_distinct_documents_before_limit_and_preserves_matching_chunk(self):
        first = self.ingest("rocket " * 15000)
        path = self.root / "second.txt"
        path.write_text("prefix " * 800 + " rocket landing", encoding="utf-8")
        second = self.store.ingest(path, title="Second", url="https://example.org/second",
                                  rights=Rights(True, True, "Own fixture"))
        entries = self.store.search("rocket landing", limit=2)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["first_chunk_id"], second["first_chunk_id"])
        self.assertNotEqual(entries[0]["id"], second["first_chunk_id"])
        self.assertIn("rocket landing", self.store.fetch(entries[0]["id"])["text"])
        self.assertEqual(entries[1]["first_chunk_id"], first["first_chunk_id"])

    def test_local_only_search_has_one_result_per_source_without_network(self):
        self.ingest("rocket " * 4000)
        # A second archived version of the same source must not occupy another slot.
        self.ingest("rocket landing " * 4000)
        discovery = Mock()
        result = PaperBridge(self.store, discovery).search("rocket", local_only=True)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["metadata_discovery"], "not_requested")
        self.assertEqual(discovery.mock_calls, [])

    def test_search_merges_metadata_and_local_source_preferring_readable_id(self):
        document = self.ingest()
        discovery = Mock()
        discovery.search.return_value = [
            {"id": "metadata:existing", "url": "https://example.org/demo"},
            {"id": "metadata:new", "url": "https://example.org/new"},
            {"id": "metadata:new", "url": "https://example.org/new"},
        ]
        entries = PaperBridge(self.store, discovery).search("rocket", 3)["results"]
        self.assertEqual([entry["id"] for entry in entries],
                         [document["first_chunk_id"], "metadata:new"])
        self.assertEqual(entries[0]["content_level"], "local_chunk_available")

    def test_search_limit_one_still_discovers_and_discovery_failure_falls_back(self):
        self.ingest("rocket " * 4000)
        discovery = Mock()
        discovery.search.return_value = [{"id": "metadata:new", "url": "https://example.org/new"}]
        bridge = PaperBridge(self.store, discovery)
        self.assertEqual(bridge.search("rocket", 1)["results"][0]["id"], "metadata:new")
        discovery.search.side_effect = DiscoveryError("Unavailable")
        self.assertTrue(bridge.search("rocket", 1)["results"][0]["id"].startswith("pb:"))

    def test_search_invalid_arguments_do_not_contact_discovery(self):
        discovery = Mock()
        bridge = PaperBridge(self.store, discovery)
        for query, limit in [(None, 10), ("rocket", True), ("rocket", 1.5),
                             ("", 10), ("rocket", 0), ("rocket", 21)]:
            with self.subTest(query=query, limit=limit), self.assertRaises(ValueError):
                bridge.search(query, limit)
        self.assertEqual(discovery.mock_calls, [])

    def test_metadata_fetch_never_claims_full_text(self):
        discovery = Mock()
        discovery.lookup.return_value = {"id": "doi:10.1234/demo", "doi": "10.1234/demo",
            "title": "Demo", "url": "https://doi.org/10.1234/demo", "authors": [], "year": 2026}
        result = PaperBridge(self.store, discovery).fetch("doi:10.1234/demo")
        self.assertEqual(result["metadata"]["content_level"], "metadata_only")
        self.assertFalse(result["metadata"]["full_text_read"])

    def test_persistence(self):
        document = self.ingest()
        other = DocumentStore(self.root / "state")
        self.assertEqual(other.fetch(document["first_chunk_id"])["text"], "rocket landing")

    def test_get_text_by_doi_uses_local_document_without_network(self):
        path = self.root / "doi.txt"
        path.write_text("Original DOI transport fixture.", encoding="utf-8")
        self.store.ingest(path, title="Original fixture", url="https://doi.org/10.1234/DEMO",
                          rights=Rights(True, True, "Own fixture; DOI is synthetic"))
        discovery = Mock()
        bridge = PaperBridge(self.store, discovery)
        for reference in ["10.1234/demo", "doi:10.1234/demo", "https://doi.org/10.1234/DEMO"]:
            self.assertEqual(bridge.get_text(reference)["text"], "Original DOI transport fixture.")
        self.assertEqual(bridge.fetch("doi:10.1234/demo")["text"], "Original DOI transport fixture.")
        self.assertEqual(discovery.mock_calls, [])

    def test_get_text_without_browser_checks_public_metadata_without_claiming_text(self):
        discovery = Mock()
        discovery.lookup.side_effect = DiscoveryError('Synthetic metadata unavailable')
        result = PaperBridge(self.store, discovery).get_text("10.1234/missing")
        self.assertEqual(result["status"], "full_text_unavailable")
        self.assertIsNone(result["text"])
        self.assertFalse(result["full_text_read"])
        discovery.lookup.assert_called_once_with('10.1234/missing')
        discovery.open_access.assert_not_called()

    def test_doi_does_not_bypass_document_permission(self):
        path = self.root / "private.txt"
        path.write_text("Private fixture", encoding="utf-8")
        self.store.ingest(path, title="Private", url="https://doi.org/10.1234/demo",
                          rights=Rights(True, False, "Own private fixture"))
        bridge = PaperBridge(self.store, Mock())
        with self.assertRaises(PermissionError):
            bridge.get_text("10.1234/demo")
        with self.assertRaises(PermissionError):
            bridge.fetch("doi:10.1234/demo")


class ValidationTests(unittest.TestCase):
    def test_doi_normalization(self):
        self.assertEqual(normalize_doi("https://doi.org/10.1234/ABC"), "10.1234/abc")
        self.assertEqual(normalize_doi(" DOI:10.1234/ABC "), "10.1234/abc")
        self.assertEqual(doi_url("10.1234/a?b"), "https://doi.org/10.1234/a%3Fb")

    def test_bad_doi(self):
        for value in ["https://evil.example/10.1234/x", "10.1234/x\ny", "hello", "10.1234/"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_doi(value)

    def test_rights_validation(self):
        for args in [(True, "false", "x"), (True, True, ""), (False, True, "x")]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                Rights(*args)

    def test_api_redirect_not_followed(self):
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.status_code = 302
        client = MetadataClient(session=session)
        with self.assertRaises(DiscoveryError):
            client.search("landing")
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertEqual(session.get.call_args.args[0], "https://api.crossref.org/works")

    def test_unpaywall_email_not_invented(self):
        session = Mock()
        self.assertEqual(MetadataClient(session=session).open_access("10.1234/x")["status"], "not_configured")
        session.get.assert_not_called()

    def test_itu_links_not_entitlement_claim(self):
        result = ItuProfile().entry_points("elsevier")
        self.assertIn("go.openathens.net/redirector/itu.edu.tr", result["platform_login_url"])
        self.assertEqual(result["entitlement"], "not_checked")
        self.assertIn("yetkim.itu.edu.tr", ItuProfile().entry_points("ieee")["platform_login_url"])


if __name__ == "__main__":
    unittest.main()
