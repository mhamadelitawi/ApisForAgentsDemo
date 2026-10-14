"""Provider protocol and the shared MCP tool box both providers drive."""

import json
from dataclasses import dataclass, field
from typing import Protocol

import mcp.types as types
from mcp import ClientSession
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

console = Console()


@dataclass
class Transcript:
    calls: int = 0
    retries: int = 0
    errors: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    final_text: str = ""
    stop_reason: str = ""
    _seen: dict = field(default_factory=dict)

    def count_call(self, name: str, args: dict, is_error: bool) -> bool:
        sig = name + json.dumps({k: v for k, v in sorted(args.items()) if k != "idempotency_key"}, sort_keys=True)
        self._seen[sig] = self._seen.get(sig, 0) + 1
        self.calls += 1
        retry = self._seen[sig] > 1
        self.retries += retry
        self.errors += is_error
        return retry


class McpTools:
    """One or more MCP sessions merged into one tool list."""

    def __init__(self):
        self.tools: list[types.Tool] = []
        self.session_for: dict[str, ClientSession] = {}

    async def add(self, session: ClientSession) -> None:
        for tool in (await session.list_tools()).tools:
            self.tools.append(tool)
            self.session_for[tool.name] = session

    async def call(self, name: str, args: dict) -> tuple[str, bool]:
        session = self.session_for.get(name)
        if session is None:
            return f"Unknown tool {name!r}.", True
        result = await session.call_tool(name, args)
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        return text, bool(result.isError)


def result_text(content) -> str:
    """Flatten a tool_result content (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict):
            parts.append(block.get("text", ""))
        else:
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts)


def show_turn(text: str) -> None:
    if text.strip():
        console.print(Panel(text.strip(), title="model", border_style="cyan"))


def show_call(name: str, args: dict, result: str, is_error: bool, retry: bool) -> None:
    title = f"[bold]{name}[/bold]" + ("  [yellow]RETRY[/yellow]" if retry else "")
    console.print(Panel(Syntax(json.dumps(args, indent=2), "json", theme="ansi_dark", word_wrap=True),
                        title=f"tool call {title}", border_style="magenta"))
    console.print(Panel(result if len(result) < 3000 else result[:3000] + "\n…",
                        title="result", border_style="red" if is_error else "green"))


class Provider(Protocol):
    name: str
    model: str

    async def run(self, task: str, tools: McpTools, system_prompt: str, recorder, max_turns: int) -> Transcript: ...
