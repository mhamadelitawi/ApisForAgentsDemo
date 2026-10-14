"""Gemini provider: google-genai with a manual function-calling loop over the MCP tools."""

import os

from google import genai
from google.genai import types as gt

from .base import McpTools, Transcript, console, show_call, show_turn


class GeminiProvider:
    name = "gemini"

    def __init__(self):
        self.model = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
        self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    async def run(self, task: str, tools: McpTools, system_prompt: str, recorder, max_turns: int) -> Transcript:
        t = Transcript()
        declarations = [gt.FunctionDeclaration(name=tool.name, description=tool.description,
                                               parameters_json_schema=tool.inputSchema)
                        for tool in tools.tools]
        config = gt.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0,
            tools=[gt.Tool(function_declarations=declarations)],
            automatic_function_calling=gt.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = [gt.Content(role="user", parts=[gt.Part.from_text(text=task)])]

        for _ in range(max_turns):
            resp = await self.client.aio.models.generate_content(model=self.model, contents=contents, config=config)
            usage = resp.usage_metadata
            if usage:
                t.tokens_in += usage.prompt_token_count or 0
                t.tokens_out += (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
            if not resp.candidates or resp.candidates[0].content is None:
                t.stop_reason = str(getattr(resp.prompt_feedback, "block_reason", "no candidates"))
                console.print(f"[red]No response from Gemini[/red] ({t.stop_reason})")
                recorder.turn(text="", tool_calls=[], stop_reason=t.stop_reason)
                break
            candidate = resp.candidates[0]
            contents.append(candidate.content)  # keeps thought signatures for the next turn
            parts = candidate.content.parts or []
            text = "".join(p.text for p in parts if p.text and not p.thought)
            calls = [p.function_call for p in parts if p.function_call]
            t.stop_reason = str(candidate.finish_reason)
            show_turn(text)
            recorder.turn(text=text, tool_calls=[{"name": c.name, "arguments": dict(c.args or {})} for c in calls],
                          stop_reason=t.stop_reason, model=self.model)
            t.final_text = text or t.final_text
            if not calls:
                break

            responses = []
            for c in calls:
                args = dict(c.args or {})
                text_out, is_error = await tools.call(c.name, args)
                retry = t.count_call(c.name, args, is_error)
                show_call(c.name, args, text_out, is_error, retry)
                recorder.tool_result(name=c.name, arguments=args, text=text_out, is_error=is_error)
                responses.append(gt.Part.from_function_response(
                    name=c.name, response={"error": text_out} if is_error else {"result": text_out}))
            contents.append(gt.Content(role="user", parts=responses))
        else:
            console.print(f"[yellow]Stopping after {max_turns} turns.[/yellow]")
        return t
