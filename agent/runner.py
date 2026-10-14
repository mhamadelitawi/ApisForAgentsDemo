"""Scripted runner: one task, one provider, against the ShopCo MCP server.

  docker compose run --rm runner --provider gemini --scene refund-basic
  docker compose run --rm runner --provider claude --scene retry --record
  docker compose run --rm runner --replay recordings/retry-claude.jsonl

Replay needs no model and no internet: it re-plays the recorded model turns and, by
default, re-executes the recorded tool calls against the live MCP server so the
dashboard and ledger behave exactly as they did on the recording.
"""

import argparse
import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path

import httpx
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from rich.table import Table

from . import recorder as rec
from .providers.base import McpTools, Transcript, console, show_call, show_turn
from .scenes import BY_ID, SCENES

MCP_URL = os.getenv("MCP_URL", "http://localhost:8801/mcp")
MCP_EVIL_URL = os.getenv("MCP_EVIL_URL", "http://localhost:8802/mcp")
API_URL = os.getenv("SHOPCO_API_URL", "http://localhost:8800").rstrip("/")
TOKEN_ADMIN = os.getenv("TOKEN_ADMIN", "demo-admin-token")
PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "support_agent.md"


def make_provider(name: str):
    if name == "claude":
        from .providers.claude import ClaudeProvider
        return ClaudeProvider()
    if name == "gemini":
        from .providers.gemini import GeminiProvider
        return GeminiProvider()
    raise SystemExit(f"unknown provider {name}")


async def admin(path: str, body: dict | None = None) -> dict:
    async with httpx.AsyncClient(base_url=API_URL, timeout=10) as c:
        r = await c.post(path, json=body or {}, headers={"Authorization": f"Bearer {TOKEN_ADMIN}"})
        r.raise_for_status()
        return r.json()


@contextlib.asynccontextmanager
async def mcp_tools(client_name: str, extra: bool):
    async with contextlib.AsyncExitStack() as stack:
        tools = McpTools()
        for url in [MCP_URL] + ([MCP_EVIL_URL] if extra else []):
            read, write, _ = await stack.enter_async_context(streamable_http_client(url))
            session = await stack.enter_async_context(
                ClientSession(read, write, client_info=types.Implementation(name=client_name, version="1.0")))
            await session.initialize()
            await tools.add(session)
        yield tools


def summary(t: Transcript, provider: str, model: str, wall: float) -> None:
    table = Table(title=f"{provider} · {model}")
    for col in ("calls", "retries", "errors", "tokens in", "tokens out", "wall time"):
        table.add_column(col, justify="right")
    table.add_row(str(t.calls), str(t.retries), str(t.errors), f"{t.tokens_in:,}", f"{t.tokens_out:,}", f"{wall:.1f}s")
    console.print(table)


async def run_live(args) -> None:
    scene = BY_ID[args.scene] if args.scene else None
    task = args.task or (scene["task"] if scene else None)
    if not task:
        raise SystemExit("give --scene or --task")
    provider = make_provider(args.provider)
    if scene and not args.no_preset:
        await admin(f"/admin/scenes/{scene['id']}")
    extra = args.extra_mcp or (scene is not None and scene["preset"].get("injection") == "tool-poisoning")

    path = None
    if args.record:
        path = Path(args.record) if args.record is not True else \
            Path("recordings") / f"{args.scene or 'task'}-{args.provider}.jsonl"
    recorder = rec.Recorder(path) if path else rec.NullRecorder()
    recorder.meta(provider=args.provider, model=provider.model, scene=args.scene, task=task, extra_mcp=extra)

    console.rule(f"[bold]{scene['number'] + ' · ' + scene['title'] if scene else 'task'}[/bold] · {args.provider}")
    console.print(f"[dim]task:[/dim] {task}")
    t0 = time.monotonic()
    async with mcp_tools(f"runner-{args.provider}", extra) as tools:
        console.print(f"[dim]{len(tools.tools)} tools: {', '.join(t.name for t in tools.tools)}[/dim]")
        transcript = await provider.run(task, tools, PROMPT.read_text(encoding="utf-8"), recorder, args.max_turns)
    wall = time.monotonic() - t0
    recorder.summary(calls=transcript.calls, retries=transcript.retries, errors=transcript.errors,
                     tokens_in=transcript.tokens_in, tokens_out=transcript.tokens_out, wall=round(wall, 1))
    recorder.close()
    with contextlib.suppress(Exception):
        await admin("/admin/events", {"type": "runner_stats", "provider": args.provider, "model": provider.model,
                                      "tokens_in": transcript.tokens_in, "tokens_out": transcript.tokens_out})
    summary(transcript, args.provider, provider.model, wall)
    if path:
        console.print(f"[green]recorded to {path}[/green]")


async def run_replay(args) -> None:
    lines = rec.load(Path(args.replay))
    meta = next((l for l in lines if l["type"] == "meta"), {})
    scene = BY_ID.get(meta.get("scene") or "")
    live = not args.print_only
    if scene and live and not args.no_preset:
        await admin(f"/admin/scenes/{scene['id']}")
    console.rule(f"[bold]REPLAY[/bold] {args.replay} · {meta.get('provider')} · {meta.get('model')}")
    console.print(f"[dim]task:[/dim] {meta.get('task')}")
    t = Transcript()
    t0 = time.monotonic()
    ctx = mcp_tools(f"runner-replay-{meta.get('provider')}", bool(meta.get("extra_mcp"))) if live \
        else contextlib.nullcontext(None)
    async with ctx as tools:
        last_t = 0.0
        for line in lines:
            if args.speed > 0 and "t" in line:
                await asyncio.sleep(max(0.0, min(3.0, (line["t"] - last_t) / args.speed)))
                last_t = line["t"]
            if line["type"] == "turn":
                show_turn(line.get("text", ""))
            elif line["type"] == "tool_result":
                name, arguments = line["name"], line.get("arguments") or {}
                if tools is not None:
                    text, is_error = await tools.call(name, arguments)
                else:
                    text, is_error = line.get("text", ""), bool(line.get("is_error"))
                retry = t.count_call(name, arguments, is_error)
                show_call(name, arguments, text, is_error, retry)
            elif line["type"] == "summary":
                t.tokens_in, t.tokens_out = line.get("tokens_in", 0), line.get("tokens_out", 0)
    if live:
        with contextlib.suppress(Exception):
            await admin("/admin/events", {"type": "runner_stats", "provider": meta.get("provider"), "replay": True,
                                          "tokens_in": t.tokens_in, "tokens_out": t.tokens_out})
    summary(t, f"replay of {meta.get('provider')}", meta.get("model", "?"), time.monotonic() - t0)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="runner", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", choices=["claude", "gemini"], default="gemini")
    p.add_argument("--scene", choices=[s["id"] for s in SCENES])
    p.add_argument("--task", help="override the scene task text")
    p.add_argument("--record", nargs="?", const=True, default=None,
                   help="write a JSONL recording (default recordings/<scene>-<provider>.jsonl)")
    p.add_argument("--replay", help="replay a JSONL recording")
    p.add_argument("--print-only", action="store_true", help="replay: print recorded results, call nothing")
    p.add_argument("--speed", type=float, default=1.0, help="replay pacing; 0 = as fast as possible")
    p.add_argument("--no-preset", action="store_true", help="do not load the scene preset (keep current admin state)")
    p.add_argument("--extra-mcp", action="store_true", help="also connect the tool-poisoning MCP server")
    p.add_argument("--max-turns", type=int, default=20)
    p.add_argument("--list-scenes", action="store_true")
    args = p.parse_args(argv)

    if args.list_scenes:
        for s in SCENES:
            console.print(f"[bold]{s['id']:<18}[/bold] {s['number']:<3} {s['title']:<24} {s['task']}")
        return
    try:
        asyncio.run(run_replay(args) if args.replay else run_live(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
