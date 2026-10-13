"""Run the routing questions through ``claude -p`` and record tool use and token usage.

Each (variant, question, repetition) is one fresh, non-persistent Claude Code
session connected only to a local ``itu-mcp`` stdio server. The raw stream
contains the student's real data, so write it outside the repository.

Example::

    .venv/bin/python benchmarks/tool_routing/run_claude.py \
        --env-file ~/.config/itu-mcp-bench/.env \
        --out .workspace/router-bench/run-1 \
        --variants full-nosearch,full-search --model sonnet
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SERVER_KEY = "itu"
TOOL_PREFIX = f"mcp__{SERVER_KEY}__"
ROUTER_CALL_TOOL = "itu_call"
ROUTER_FIND_TOOL = "itu_find_tool"

SYSTEM_APPEND = (
    "You are helping an ITU student. Answer only with data from the itu tools, "
    "in the language of the question, briefly. Never perform write actions."
)

# name -> (tool profile served by itu-mcp, extra environment for claude)
VARIANTS: dict[str, tuple[str, dict[str, str]]] = {
    # Every tool schema is sent up front, as in clients without tool search.
    "full-nosearch": ("full", {"ENABLE_TOOL_SEARCH": "false"}),
    # Claude Code may defer MCP schemas behind its own tool search.
    "full-search": ("full", {}),
    "router": ("router", {"ENABLE_TOOL_SEARCH": "false"}),
}


def write_mcp_config(path: Path, profile: str, env_file: Path, workdir: Path) -> None:
    config = {
        "mcpServers": {
            SERVER_KEY: {
                "command": str(REPO / ".venv" / "bin" / "python"),
                "args": ["-m", "ninova_mcp"],
                "cwd": str(workdir),
                "env": {
                    "NINOVA_ENV_FILE": str(env_file),
                    "ITU_MCP_TOOL_PROFILE": profile,
                    "PYTHONPATH": str(REPO / "src"),
                },
            }
        }
    }
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def effective_operation(name: str, tool_input: dict[str, Any]) -> str | None:
    """Map a Claude tool call to the itu-mcp operation it actually ran."""
    if not name.startswith(TOOL_PREFIX):
        return None
    tool = name[len(TOOL_PREFIX):]
    if tool == ROUTER_CALL_TOOL:
        return str(tool_input.get("operation") or "")
    if tool == ROUTER_FIND_TOOL:
        return None
    return tool


def parse_stream(lines: list[str]) -> dict[str, Any]:
    tool_calls: list[dict[str, Any]] = []
    tool_result_chars = 0
    final: dict[str, Any] = {}
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "assistant":
            for block in event.get("message", {}).get("content", []) or []:
                if block.get("type") == "tool_use":
                    tool_calls.append({"name": block.get("name", ""), "input": block.get("input") or {}})
        elif kind == "user":
            content = event.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    tool_result_chars += len(json.dumps(block.get("content"), ensure_ascii=False))
        elif kind == "result":
            final = event
    return {"tool_calls": tool_calls, "tool_result_chars": tool_result_chars, "final": final}


def score(question: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    operations = [
        op for call in parsed["tool_calls"]
        if (op := effective_operation(call["name"], call["input"]))
    ]
    expected = set(question["expected"])
    hit_index = next((i for i, op in enumerate(operations) if op in expected), None)
    tools = [call["name"] for call in parsed["tool_calls"]]
    usage = parsed["final"].get("usage") or {}
    context_tokens = sum(int(usage.get(key) or 0) for key in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
    ))
    return {
        "operations": operations,
        "tool_calls": len(tools),
        "itu_tool_calls": sum(name.startswith(TOOL_PREFIX) for name in tools),
        "find_calls": sum(name == TOOL_PREFIX + ROUTER_FIND_TOOL for name in tools),
        "tool_search_calls": sum(name == "ToolSearch" for name in tools),
        "hit": hit_index is not None,
        "first_hit_index": hit_index,
        "wrong_operations_before_hit": hit_index if hit_index is not None else len(operations),
        "input_tokens_total": context_tokens,
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cost_usd": parsed["final"].get("total_cost_usd"),
        "duration_ms": parsed["final"].get("duration_ms"),
        "num_turns": parsed["final"].get("num_turns"),
        "is_error": bool(parsed["final"].get("is_error")) or not parsed["final"],
        "tool_result_chars": parsed["tool_result_chars"],
    }


def run_one(prompt: str, model: str, config: Path, extra_env: dict[str, str],
            workdir: Path, timeout: int) -> tuple[list[str], float, int]:
    command = [
        "claude", "-p", prompt,
        "--model", model,
        "--strict-mcp-config", "--mcp-config", str(config),
        "--setting-sources", "",
        "--no-session-persistence",
        "--output-format", "stream-json", "--verbose",
        "--allowedTools", f"mcp__{SERVER_KEY}",
        "--append-system-prompt", SYSTEM_APPEND,
    ]
    env = {**os.environ, **extra_env}
    started = time.monotonic()
    completed = subprocess.run(
        command, cwd=workdir, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout,
    )
    return completed.stdout.splitlines(), time.monotonic() - started, completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, required=True,
                        help="Private .env with the student's ITU credentials.")
    parser.add_argument("--out", type=Path, required=True,
                        help="Results directory; keep it outside version control.")
    parser.add_argument("--variants", default="full-nosearch,full-search,router")
    parser.add_argument("--questions", type=Path, default=HERE / "questions.json")
    parser.add_argument("--only", default="", help="Comma-separated question ids.")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=420)
    args = parser.parse_args(argv)

    env_file = args.env_file.expanduser().resolve()
    if not env_file.is_file():
        parser.error(f"env file not found: {env_file}")
    questions = json.loads(args.questions.read_text(encoding="utf-8"))["questions"]
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        questions = [q for q in questions if q["id"] in wanted]
    variants = [name.strip() for name in args.variants.split(",") if name.strip()]
    unknown = sorted(set(variants) - set(VARIANTS))
    if unknown:
        parser.error(f"unknown variants: {', '.join(unknown)}")

    out = args.out.expanduser().resolve()
    (out / "raw").mkdir(parents=True, exist_ok=True)
    workdir = out / "workdir"
    workdir.mkdir(exist_ok=True)
    results_path = out / "results.jsonl"

    for repetition in range(args.repeat):
        # Interleave variants per question so live data and service load
        # change as little as possible between the compared runs.
        for question in questions:
            for variant in variants:
                profile, extra_env = VARIANTS[variant]
                config = out / f"mcp-{profile}.json"
                write_mcp_config(config, profile, env_file, workdir)
                label = f"{variant}/{question['id']}/{repetition}"
                try:
                    lines, wall, code = run_one(question["prompt"], args.model, config,
                                                extra_env, workdir, args.timeout)
                except subprocess.TimeoutExpired:
                    lines, wall, code = [], float(args.timeout), -1
                raw_path = out / "raw" / f"{variant}__{question['id']}__{repetition}.jsonl"
                raw_path.write_text("\n".join(lines), encoding="utf-8")
                record = {
                    "variant": variant, "question": question["id"], "repetition": repetition,
                    "model": args.model, "exit_code": code, "wall_seconds": round(wall, 2),
                    **score(question, parse_stream(lines)),
                }
                with results_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(f"{label}: hit={record['hit']} calls={record['itu_tool_calls']} "
                      f"tokens={record['input_tokens_total']} wall={record['wall_seconds']}s",
                      flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
