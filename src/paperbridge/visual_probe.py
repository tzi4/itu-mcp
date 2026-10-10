"""Synthetic, opt-in host experiment for ChatGPT's widget image attachment path."""
import base64
import io
from pathlib import Path
import uuid

PROBE_URI = "ui://paperbridge/visual-probe-v1.html"
PROBE_META = {"ui": {"resourceUri": PROBE_URI}, "openai/outputTemplate": PROBE_URI}


def visual_probe():
    """Create an original test image without accessing documents or credentials."""
    from PIL import Image, ImageDraw
    from mcp.types import CallToolResult, TextContent
    image = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(image)
    draw.polygon([(90, 260), (170, 80), (250, 260)], fill="#2563eb")
    draw.ellipse((390, 110, 550, 270), fill="#e84838")
    output = io.BytesIO()
    image.save(output, "PNG")
    info = {"status": "visual_probe_ready", "probe_id": uuid.uuid4().hex,
            "synthetic": True, "requires_user_action": True,
            "model_visual_access": "not_verified"}
    return CallToolResult(content=[TextContent(type="text", text=
        "Synthetic visual test prepared. If a preview appears, use its Add image to chat button. "
        "Do not infer image contents from tool success. No paper or account was accessed.")],
        structuredContent=info, _meta={"paperbridge/image": {
            "mimeType": "image/png", "data": base64.b64encode(output.getvalue()).decode()},
            "paperbridge/probe_id": info["probe_id"]})


def register_probe_resource(mcp):
    """Register static UI only; the host must separately allowlist the probe tool."""
    @mcp.resource(PROBE_URI, mime_type="text/html;profile=mcp-app",
                  meta={"ui": {"csp": {"connectDomains": [], "resourceDomains": []}},
                        "openai/widgetDescription": "A synthetic image preview with an Add image to chat button.",
                        "openai/widgetCSP": {"connect_domains": [], "resource_domains": []}})
    def visual_probe_ui():
        return Path(__file__).with_name("visual_probe.html").read_text(encoding="utf-8")
