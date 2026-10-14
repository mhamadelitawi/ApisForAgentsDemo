"""Claude provider: anthropic SDK tool runner over the MCP tools (API key required)."""

import os

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool

from .base import McpTools, Transcript, console, result_text, show_call, show_turn


class ClaudeProvider:
    name = "claude"

    def __init__(self):
        self.model = os.getenv("CLAUDE_MODEL") or "claude-opus-5-5"
        self.effort = os.getenv("CLAUDE_EFFORT") or "low"
        self.client = AsyncAnthropic()  # ANTHROPIC_API_KEY, or an `ant auth login` profile

    async def run(self, task: str, tools: McpTools, system_prompt: str, recorder, max_turns: int) -> Transcript:
        t = Transcript()
        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": task}],
            tools=[async_mcp_tool(tool, tools.session_for[tool.name]) for tool in tools.tools],
            # Opus 5.5: thinking is always on and temperature is not accepted; effort is the dial.
            output_config={"effort": self.effort},
            # A safety-classifier refusal (possible on the injection scene) retries on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        turns = 0
        async for message in runner:
            turns += 1
            t.tokens_in += message.usage.input_tokens
            t.tokens_out += message.usage.output_tokens
            t.stop_reason = message.stop_reason or ""
            if message.stop_reason == "refusal":
                details = getattr(message, "stop_details", None)
                console.print(f"[red]Model refused[/red] ({getattr(details, 'category', None)})")
                recorder.turn(text="", tool_calls=[], stop_reason="refusal")
                break

            text = "".join(b.text for b in message.content if b.type == "text")
            calls = [b for b in message.content if b.type == "tool_use"]
            show_turn(text)
            recorder.turn(text=text, tool_calls=[{"id": c.id, "name": c.name, "arguments": c.input} for c in calls],
                          stop_reason=message.stop_reason, model=message.model)
            t.final_text = text or t.final_text

            if calls:
                # Cached by the runner, so the tools still run exactly once.
                response = await runner.generate_tool_call_response()
                results = {r["tool_use_id"]: r for r in (response or {}).get("content", [])
                           if isinstance(r, dict) and r.get("type") == "tool_result"}
                for c in calls:
                    r = results.get(c.id, {})
                    text_out, is_error = result_text(r.get("content")), bool(r.get("is_error"))
                    retry = t.count_call(c.name, c.input, is_error)
                    show_call(c.name, c.input, text_out, is_error, retry)
                    recorder.tool_result(name=c.name, arguments=c.input, text=text_out, is_error=is_error)

            if turns >= max_turns:
                console.print(f"[yellow]Stopping after {max_turns} turns.[/yellow]")
                await runner.close()
                break
        return t
