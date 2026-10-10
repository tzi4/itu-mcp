"""Check the registered singular tool's default and explicit legacy transport."""
import base64
import importlib.util
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(importlib.util.find_spec("mcp"), "Install the optional MCP dependency")
class SinglePageTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_tool_defaults_to_mcp_apps_and_keeps_explicit_compatibility(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError
        from paperbridge.mcp_server import register_tools
        from paperbridge.page_viewer import PAGE_VIEW_URI

        original = b"\x89PNG\r\n\x1a\n"
        reference = "pb:" + "a" * 64 + ":0"
        page = {"id": reference, "url": "https://doi.org/10.1234/fixture", "page": 2,
                "page_count": 3, "image_bytes": original}
        bridge = Mock()
        bridge.get_page.return_value = page
        with patch.dict("os.environ", {}, clear=True):
            server = FastMCP("Original synthetic transport fixture")
            register_tools(server, bridge)
            tools = {tool.name: tool for tool in await server.list_tools()}
            selected = tools["papers_view_page"]
            self.assertEqual(selected.inputSchema["properties"]["continuation_transport"]["default"], "mcp_apps")
            self.assertEqual(selected.meta["ui"]["resourceUri"], PAGE_VIEW_URI)
            for options, expected in (({}, "mcp_apps"), ({"continuation_transport": "compatibility"}, "compatibility")):
                with self.subTest(expected=expected):
                    bridge.get_page.reset_mock()
                    result = await server.call_tool("papers_view_page", {
                        "reference": reference, "page": 2, "max_edge": 800, "automatic": True, **options})
                    bridge.get_page.assert_called_once_with(reference, 2, 800)
                    self.assertEqual(result.structuredContent["continuation_transport"], expected)
                    self.assertEqual(result.meta["paperbridge/page"]["continuation_transport"], expected)
                    self.assertEqual(result.structuredContent["model_visual_access"], "not_verified")
                    self.assertEqual(result.structuredContent["automatic_delivery"], "requested_not_verified")
                    self.assertEqual(base64.b64decode(result.meta["paperbridge/image"]["data"]), original)
            bridge.get_page.reset_mock()
            with self.assertRaises(ToolError):
                await server.call_tool("papers_view_page", {
                    "reference": reference, "page": 2, "continuation_transport": "invalid"})
            bridge.get_page.assert_not_called()
