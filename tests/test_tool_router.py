"""Router tool profile: catalog coverage, ranking and the itu_call dispatcher."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("NINOVA_USERNAME", "dummy")
os.environ.setdefault("NINOVA_PASSWORD", "dummy")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from ninova_mcp import server  # noqa: E402
from ninova_mcp.tool_catalog import DOMAINS, KEYWORDS, TOOL_DOMAIN  # noqa: E402
from ninova_mcp.tool_router import (  # noqa: E402
    CALL_TOOL_NAME, FAST_PATH_TOOL_NAMES, FIND_TOOL_NAME, PROFILE_ENV, fold,
    selected_profile, tokenize,
)


def _build(profile: str, tool_names: list[str] | None = None) -> FastMCP:
    mcp = FastMCP("test")
    server.register_tools(mcp, server.NinovaMcpApp(),
                          tool_names or server.LOCAL_TOOL_NAMES, profile=profile)
    return mcp


def _call(mcp: FastMCP, name: str, arguments: dict) -> dict:
    result = asyncio.run(mcp.call_tool(name, arguments))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def _names(mcp: FastMCP) -> set[str]:
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


class CatalogTests(unittest.TestCase):
    def test_every_tool_has_exactly_one_domain(self) -> None:
        listed = [name for _, names in DOMAINS.values() for name in names]
        self.assertEqual(len(listed), len(set(listed)), "a tool is listed in two domains")
        self.assertEqual(set(listed), set(server.LOCAL_TOOL_NAMES))

    def test_keywords_name_known_tools(self) -> None:
        self.assertLessEqual(set(KEYWORDS), set(TOOL_DOMAIN))


class TextTests(unittest.TestCase):
    def test_turkish_letters_fold_to_ascii(self) -> None:
        self.assertEqual(fold("İTÜ Ödev Şubesi ığç"), "itu odev subesi igc")

    def test_stacked_suffixes_meet_the_stem(self) -> None:
        self.assertIn("not", tokenize("notlarım"))
        self.assertIn("ders", tokenize("derslerim"))
        self.assertIn("sinif", tokenize("sınıfta"))


class ProfileTests(unittest.TestCase):
    def test_default_profile_is_full(self) -> None:
        self.assertEqual(selected_profile({}), "full")

    def test_unknown_profile_fails(self) -> None:
        with self.assertRaises(RuntimeError):
            selected_profile({PROFILE_ENV: "tiny"})

    def test_full_profile_publishes_every_tool(self) -> None:
        names = _names(_build("full"))
        self.assertEqual(names, set(server.LOCAL_TOOL_NAMES))

    def test_router_keeps_writes_state_mail_and_fast_paths_published(self) -> None:
        names = _names(_build("router"))
        self.assertIn(FIND_TOOL_NAME, names)
        self.assertIn(CALL_TOOL_NAME, names)
        must_publish = (
            set(server.STATEFUL_TOOL_NAMES) | set(server.DESTRUCTIVE_TOOL_NAMES)
            | set(server.MAIL_TOOL_NAMES) | set(FAST_PATH_TOOL_NAMES) | {"search", "fetch"}
        )
        self.assertLessEqual(must_publish, names)
        self.assertNotIn("get_cafeteria_menu", names)
        self.assertLess(len(names), len(server.LOCAL_TOOL_NAMES) // 2)

    def test_router_loses_no_operation(self) -> None:
        mcp = _build("router")
        listed = _call(mcp, FIND_TOOL_NAME, {"domain": "obs"})
        self.assertIn("obs_get_advisor", [op["operation"] for op in listed["operations"]])
        published = _names(mcp) - {FIND_TOOL_NAME, CALL_TOOL_NAME}
        reachable = set(published)
        for domain in DOMAINS:
            reachable.update(op["operation"] for op in _call(mcp, FIND_TOOL_NAME, {"domain": domain})["operations"])
        self.assertEqual(reachable, set(server.LOCAL_TOOL_NAMES))

    def test_router_tools_are_read_only(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(_build("router").list_tools())}
        for name in (FIND_TOOL_NAME, CALL_TOOL_NAME):
            self.assertTrue(tools[name].annotations.readOnlyHint)
            self.assertFalse(tools[name].annotations.destructiveHint)

    def test_router_respects_the_selected_tool_subset(self) -> None:
        mcp = _build("router", ["auth_status", "get_cafeteria_menu", "submit_assignment"])
        found = _call(mcp, FIND_TOOL_NAME, {"query": "obs danışman"})
        self.assertNotIn("obs_get_advisor", [m["operation"] for m in found["matches"]])
        refused = _call(mcp, CALL_TOOL_NAME, {"operation": "obs_get_advisor", "arguments": {}})
        self.assertEqual(refused["error"], "unknown_operation")


class RankingTests(unittest.TestCase):
    CASES = {
        "Ninova'da hocaların girdiği notlarım": "get_course_grades",
        "OBS harf notlarım açıklandı mı": "obs_get_course_grades",
        "Bugün yemekhanede ne var": "get_cafeteria_menu",
        "Şu an boş derslik var mı": "find_empty_classrooms",
        "Danışmanım kim": "obs_get_advisor",
        "devamsızlık sınırına yakın mıyım": "obs_get_attendance",
        "Mezun olmak için kaç dersim kaldı": "obs_get_graduation_remaining",
        "BLG 336E dersini geçmişte kim verdi": "archive_who_taught",
        "3.00 ortalama için kaç almam gerekir": "calculate_target_gpa",
        "kampüs kartı bakiyesi": "obs_get_campus_card",
        "When are my exams": "get_personal_exam_calendar",
        "obs_get_profile": "obs_get_profile",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build("router")

    def test_expected_operation_ranks_first(self) -> None:
        for query, expected in self.CASES.items():
            with self.subTest(query=query):
                found = _call(self.mcp, FIND_TOOL_NAME, {"query": query, "limit": 3})
                self.assertEqual(found["matches"][0]["operation"], expected)

    def test_matches_carry_compact_schemas(self) -> None:
        found = _call(self.mcp, FIND_TOOL_NAME, {"query": "hedef ortalama", "limit": 1})
        schema = found["matches"][0]["input_schema"]
        self.assertIn("target_gpa", schema["properties"])
        self.assertNotIn("title", json.dumps(schema))

    def test_published_tools_point_to_themselves(self) -> None:
        found = _call(self.mcp, FIND_TOOL_NAME, {"query": "ödev teslim et yükle"})
        submit = next(m for m in found["matches"] if m["operation"] == "submit_assignment")
        self.assertEqual(submit["call_with"], "submit_assignment")

    def test_empty_request_lists_domains(self) -> None:
        listed = _call(self.mcp, FIND_TOOL_NAME, {})
        self.assertIn("obs", [entry["domain"] for entry in listed["domains"]])


class CallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build("router")

    def test_runs_a_routed_operation(self) -> None:
        arguments = {"current_gpa": 2.8, "current_credits": 60, "target_gpa": 3.0, "future_credits": 20}
        direct = server.NinovaMcpApp().calculate_target_gpa(**arguments)
        routed = _call(self.mcp, CALL_TOOL_NAME,
                       {"operation": "calculate_target_gpa", "arguments": arguments})
        self.assertTrue(routed["untrusted_external_content"])
        routed.pop("untrusted_external_content")
        routed.pop("content_notice")
        self.assertEqual(routed, direct)

    def test_invalid_arguments_return_the_schema(self) -> None:
        result = _call(self.mcp, CALL_TOOL_NAME,
                       {"operation": "calculate_target_gpa", "arguments": {"current_gpa": 2.8}})
        self.assertEqual(result["error"], "invalid_arguments")
        self.assertIn("target_gpa", result["input_schema"]["properties"])

    def test_write_tools_are_never_dispatched(self) -> None:
        for operation in ("submit_assignment", "obs_save_registration_draft", "library_renew_loan"):
            with self.subTest(operation=operation):
                with mock.patch.object(server.NinovaMcpApp, operation) as write:
                    result = _call(self.mcp, CALL_TOOL_NAME, {"operation": operation, "arguments": {}})
                self.assertEqual(result["error"], "not_routed")
                write.assert_not_called()

    def test_unknown_operation_suggests_names(self) -> None:
        result = _call(self.mcp, CALL_TOOL_NAME, {"operation": "cafeteria menu", "arguments": {}})
        self.assertEqual(result["error"], "unknown_operation")
        self.assertIn("get_cafeteria_menu", result["suggestions"])


class CallLogTests(unittest.TestCase):
    def test_log_records_metadata_only(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "calls.jsonl")
            with mock.patch.dict(os.environ, {"ITU_MCP_CALL_LOG": path}):
                mcp = _build("router")
                _call(mcp, FIND_TOOL_NAME, {"query": "hedef ortalama"})
                _call(mcp, CALL_TOOL_NAME, {"operation": "calculate_target_gpa", "arguments": {
                    "current_gpa": 2.5, "current_credits": 40, "target_gpa": 2.9, "future_credits": 20}})
            with open(path, encoding="utf-8") as stream:
                entries = [json.loads(line) for line in stream]
        self.assertEqual([entry["tool"] for entry in entries],
                         [FIND_TOOL_NAME, "calculate_target_gpa", CALL_TOOL_NAME])
        self.assertEqual(entries[2]["operation"], "calculate_target_gpa")
        for entry in entries:
            self.assertEqual(set(entry) - {"operation"}, {"ts", "tool", "ms", "result_chars", "error"})


if __name__ == "__main__":
    unittest.main()
