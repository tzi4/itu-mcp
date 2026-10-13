"""Offline, credential-free routing metrics anyone can reproduce.

Reports the size of the tool surface each profile sends to a client and how
often ``itu_find_tool`` ranks an expected operation first or in the top 3
when given the raw question. Agents usually rephrase before searching, so
the raw question is a conservative input.

    NINOVA_USERNAME=dummy NINOVA_PASSWORD=dummy \
        .venv/bin/python benchmarks/tool_routing/offline.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
os.environ.setdefault("NINOVA_USERNAME", "dummy")
os.environ.setdefault("NINOVA_PASSWORD", "dummy")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from ninova_mcp import server  # noqa: E402
from ninova_mcp.tool_router import FIND_TOOL_NAME  # noqa: E402


def _build(profile: str) -> FastMCP:
    mcp = FastMCP("benchmark", instructions=server.server_instructions(server.SERVER_INSTRUCTIONS, profile))
    server.register_tools(mcp, server.NinovaMcpApp(), server.LOCAL_TOOL_NAMES, profile=profile)
    return mcp


async def _surface(mcp: FastMCP) -> dict[str, int]:
    tools = await mcp.list_tools()
    payload = json.dumps([tool.model_dump(exclude_none=True) for tool in tools], ensure_ascii=False)
    return {"tools": len(tools), "tools_list_chars": len(payload),
            "instructions_chars": len(mcp.instructions or "")}


async def _find(mcp: FastMCP, query: str) -> tuple[list[str], int]:
    result = await mcp.call_tool(FIND_TOOL_NAME, {"query": query, "limit": 3})
    blocks = result[0] if isinstance(result, tuple) else result
    text = blocks[0].text
    return [match["operation"] for match in json.loads(text).get("matches", [])], len(text)


async def main() -> int:
    questions = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))["questions"]
    full, router = _build("full"), _build("router")
    surfaces = {"full": await _surface(full), "router": await _surface(router)}
    top1 = top3 = 0
    find_chars = []
    rows = []
    for question in questions:
        ranked, size = await _find(router, question["prompt"])
        expected = set(question["expected"])
        hit1 = bool(ranked) and ranked[0] in expected
        hit3 = bool(expected & set(ranked))
        top1 += hit1
        top3 += hit3
        find_chars.append(size)
        rows.append((question["id"], hit1, hit3, ranked))
    count = len(questions)
    reduction = 1 - surfaces["router"]["tools_list_chars"] / surfaces["full"]["tools_list_chars"]
    print(json.dumps({
        "surface": surfaces,
        "tools_list_reduction": round(reduction, 3),
        "raw_question_top1": f"{top1}/{count}",
        "raw_question_top3": f"{top3}/{count}",
        "median_find_result_chars": sorted(find_chars)[count // 2],
    }, indent=2))
    for question_id, hit1, hit3, ranked in rows:
        mark = "top1" if hit1 else ("top3" if hit3 else "MISS")
        print(f"{mark:5s} {question_id:24s} {ranked}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
