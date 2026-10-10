"""Positive open-access evidence must match the work, version and effective date."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from paperbridge.discovery import DiscoveryError, MetadataClient, public_access


DOI = "10.1234/fixture"
WORK = {
    "DOI": DOI, "title": ["Original synthetic metadata"],
    "resource": {"primary": {"URL": "https://example.org/article/fixture"}},
    "license": [{"URL": "http://creativecommons.org/licenses/by/4.0/",
                 "content-version": "vor", "start": {"date-time": "2020-01-01T00:00:00Z"}}],
}


class OpenAccessDiscoveryTests(unittest.TestCase):
    def test_current_vor_license_returns_only_validated_metadata_links(self):
        work = deepcopy(WORK)
        paper = MetadataClient._paper(work)
        result = public_access(paper)
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["links"]["url"], WORK["resource"]["primary"]["URL"])
        self.assertEqual(result["evidence"], "crossref_current_vor_open_license")
        self.assertEqual(result["reported_license"], "https://creativecommons.org/licenses/by/4.0/")
        self.assertEqual(result["version"], "publishedVersion")
        self.assertFalse(result["downloaded"])
        self.assertEqual(result["external_model_permission"], "not_evaluated")
        self.assertNotIn("text", result)
        self.assertFalse(paper["full_text_read"])
        self.assertEqual(work, WORK)

    def test_future_wrong_version_tdm_and_malformed_licenses_are_not_oa_evidence(self):
        variants = [
            {"content-version": "am"}, {"content-version": "tdm"}, {"content-version": "unspecified"},
            {"content-version": None}, {"URL": "https://www.springer.com/tdm"},
            {"URL": "https://creativecommons.org.evil.test/licenses/by/4.0/"},
            {"URL": "https://user:password@creativecommons.org/licenses/by/4.0/"},
            {"URL": "https://creativecommons.org/licenses/by/4.0/?redirect=other"},
            {"start": {"date-time": "2999-01-01T00:00:00Z"}}, {"start": None},
            {"start": {}}, {"start": {"date-time": "not a date"}},
            {"start": {"date-time": "2020-01-01"}}, {"start": {"date-parts": [[2020]]}},
            {"start": {"date-time": "2020-01-01T00:00:00Z", "date-parts": [[2999, 1, 1]]}},
            {"start": {"timestamp": True}}, {"start": {"timestamp": float("inf")}},
        ]
        for values in variants:
            with self.subTest(values=values):
                work = deepcopy(WORK)
                work["license"][0].update(values)
                self.assertIsNone(public_access(MetadataClient._paper(work)))

    def test_lifetime_cc_license_and_exact_past_date_variants_are_supported(self):
        for start in (None, {"date-parts": [[2020, 1, 1]]}, {"timestamp": 1577836800000}):
            with self.subTest(start=start):
                work = deepcopy(WORK)
                if start is None:
                    del work["license"][0]["start"]
                else:
                    work["license"][0]["start"] = start
                self.assertEqual(public_access(MetadataClient._paper(work))["status"], "found")
        for license_url in ("https://creativecommons.org/licenses/by-nc-nd/4.0/",
                            "https://creativecommons.org/publicdomain/zero/1.0/",
                            "https://creativecommons.org/licenses/by/4.0/legalcode"):
            work = deepcopy(WORK)
            work["license"][0]["URL"] = license_url
            expected = license_url.removesuffix("legalcode")
            self.assertEqual(public_access(MetadataClient._paper(work))["reported_license"], expected)

    def test_malformed_license_collections_and_mismatched_paper_identity_do_not_match(self):
        for licenses in (None, "open", {}, [None, False, "open"]):
            work = deepcopy(WORK)
            work["license"] = licenses
            self.assertIsNone(public_access(MetadataClient._paper(work)))
        paper = MetadataClient._paper(WORK)
        for field, value in (("doi", "invalid"), ("id", "doi:10.1234/other"),
                             ("url", "https://doi.org/10.1234/other")):
            self.assertIsNone(public_access(dict(paper, **{field: value})))
        client = MetadataClient(session=Mock())
        with patch.object(client, "_get", return_value={"message": dict(WORK, DOI="10.1234/other")}):
            with self.assertRaisesRegex(DiscoveryError, "identity"):
                client.lookup(DOI)

    def test_mdpi_standard_journal_metadata_returns_policy_link_without_adapter(self):
        paper = MetadataClient._paper(dict(WORK, DOI="10.3390/drones9040261", license=[],
            resource={"primary": {"URL": "https://www.mdpi.com/2504-446X/9/4/261"}}))
        result = public_access(paper)
        self.assertEqual(result["evidence"], "mdpi_journal_open_access_policy")
        self.assertEqual(result["links"]["url"], paper["publisher_url"])
        self.assertIsNone(result["reported_license"])
        for url in ("https://www.mdpi.com/about", "https://www.mdpi.com.evil.test/2504-446X/9/4/261",
                    "http://www.mdpi.com/2504-446X/9/4/261"):
            self.assertIsNone(public_access(dict(paper, publisher_url=url)))
        self.assertIsNone(public_access(dict(paper, doi=DOI, id="doi:" + DOI, url="https://doi.org/" + DOI)))

    def test_unpaywall_positive_and_negative_flags_are_identity_checked_literal_booleans(self):
        client = MetadataClient(email="fixture@example.org", session=Mock())
        data = {"doi": DOI.upper(), "is_oa": True, "best_oa_location": {
            "url": "https://example.org/manuscript", "license": None, "version": "acceptedVersion"}}
        with patch.object(client, "_get", return_value=data):
            result = client.open_access(DOI)
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["evidence"], "unpaywall")
        self.assertEqual(result["version"], "acceptedVersion")
        self.assertFalse(result["downloaded"])
        for flag in ("true", "false", 0, 1, None, []):
            with self.subTest(flag=flag), patch.object(client, "_get", return_value=dict(data, is_oa=flag)):
                with self.assertRaises(DiscoveryError):
                    client.open_access(DOI)
        for doi in (None, "10.1234/other", "not a DOI"):
            with self.subTest(doi=doi), patch.object(client, "_get", return_value=dict(data, doi=doi)):
                with self.assertRaisesRegex(DiscoveryError, "identity"):
                    client.open_access(DOI)
        with patch.object(client, "_get", return_value=dict(data, is_oa=False)):
            result = client.open_access(DOI)
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["links"], {})

    def test_unpaywall_rejects_invalid_links_and_does_not_invent_a_readable_location(self):
        client = MetadataClient(email="fixture@example.org", session=Mock())
        for url in (None, [], "javascript:alert(1)", "http://example.org/a", "https://user:secret@example.org/a",
                    "https://127.0.0.1/a", "https://localhost/a", "https://example.org:invalid/a",
                    "https://example.org:8443/a", "https://example.org\\\\@other.test/a"):
            data = {"doi": DOI, "is_oa": True, "best_oa_location": {"url": url}}
            with self.subTest(url=url), patch.object(client, "_get", return_value=data):
                result = client.open_access(DOI)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["links"], {})
        data = {"doi": DOI, "is_oa": True, "best_oa_location": {
            "url": "http://example.org/a", "url_for_pdf": "https://example.org/original.pdf"}}
        with patch.object(client, "_get", return_value=data):
            result = client.open_access(DOI)
        self.assertEqual(result["links"], {"url_for_pdf": "https://example.org/original.pdf"})
        client.session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
