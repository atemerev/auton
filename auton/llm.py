"""Lightweight LLM client with tool-calling loop.

Stripped-down port of Lethe's AsyncLLMClient, focused on:
- litellm.acompletion for multi-provider support
- Tool calling loop with circuit breakers
- Token usage tracking
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from litellm import acompletion

logger = logging.getLogger(__name__)

# Circuit breaker constants (from Lethe)
MAX_TOOL_ERRORS = 8
MAX_REPEATED_TOOL_CALLS = 4
MAX_NO_PROGRESS_TURNS = 4
MAX_CONTINUATION_DEPTH = 2


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMClient:
    """Async LLM client with tool-calling loop."""

    def __init__(self, model: str, system_prompt: str, temperature: float = 0.7, max_output_tokens: int = 4096,
                 budget_check: Callable[[], bool] | None = None):
        self.model = model
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.messages: list[dict[str, Any]] = []
        self._tools: dict[str, tuple[Callable, dict]] = {}
        self._stop_requested = False
        self.usage = TokenUsage()
        self._budget_check = budget_check  # Returns True if budget exceeded

    def add_tool(self, func: Callable, schema: dict) -> None:
        """Register a tool function with its OpenAI function schema."""
        name = schema["name"]
        self._tools[name] = (func, schema)

    def _build_tool_specs(self) -> list[dict]:
        """Build OpenAI-format tool specs for the API call."""
        return [
            {"type": "function", "function": schema}
            for _, schema in self._tools.values()
        ]

    def get_tool(self, name: str) -> Callable | None:
        """Get a tool function by name."""
        entry = self._tools.get(name)
        return entry[0] if entry else None

    async def chat(self, message: str, max_tool_iterations: int = 15) -> str:
        """Send a message and get response, handling tool calls.

        Returns the final assistant response text.
        """
        total_tool_calls = 0
        continuation_depth = 0
        total_tool_errors = 0
        repeated_tool_call_streak = 0
        no_progress_turns = 0
        last_tool_signature = ""
        circuit_breaker_reason = ""

        self.messages.append({"role": "user", "content": message})

        while True:
            empty_count = 0

            for _ in range(max_tool_iterations):
                if self._stop_requested:
                    return self.messages[-1].get("content", "Done.") if self.messages else "Done."

                response = await self._call_api()

                choice = response["choices"][0]
                assistant_msg = choice["message"]
                content = assistant_msg.get("content") or ""
                tool_calls = assistant_msg.get("tool_calls")

                # Update token usage
                usage = response.get("usage", {})
                if usage:
                    self.usage.prompt_tokens += usage.get("prompt_tokens", 0)
                    self.usage.completion_tokens += usage.get("completion_tokens", 0)
                    self.usage.total_tokens += usage.get("total_tokens", 0)

                # Inline budget check after every API call
                if self._budget_check and self._budget_check():
                    logger.warning(f"Budget exceeded at {self.usage.total_tokens} tokens")
                    self._stop_requested = True
                    if content:
                        self.messages.append({"role": "assistant", "content": content})
                    return content or "Budget exceeded."

                if tool_calls:
                    empty_count = 0
                    total_tool_calls += len(tool_calls)

                    # Ensure all tool calls have IDs
                    for tc in tool_calls:
                        if not tc.get("id"):
                            tc["id"] = f"call-{uuid.uuid4().hex[:12]}"

                    # Add assistant message with tool calls
                    self.messages.append({
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls,
                    })

                    # Execute each tool
                    for tool_call in tool_calls:
                        tool_name = tool_call["function"]["name"].strip()
                        tool_id = tool_call["id"]

                        try:
                            tool_args = json.loads(tool_call["function"]["arguments"])
                        except json.JSONDecodeError as e:
                            logger.error(f"Tool {tool_name} malformed args: {e}")
                            total_tool_errors += 1
                            self.messages.append({
                                "role": "tool",
                                "content": f"Error: malformed arguments - {e}",
                                "tool_call_id": tool_id,
                                "name": tool_name,
                            })
                            continue

                        # Repeated call detection
                        signature = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
                        if signature == last_tool_signature:
                            repeated_tool_call_streak += 1
                        else:
                            repeated_tool_call_streak = 1
                            last_tool_signature = signature

                        logger.info(f"Tool: {tool_name}({list(tool_args.keys())})")

                        # Execute (handle sync and async)
                        handler = self.get_tool(tool_name)
                        if handler:
                            try:
                                if asyncio.iscoroutinefunction(handler):
                                    result = await handler(**tool_args)
                                else:
                                    loop = asyncio.get_event_loop()
                                    result = await loop.run_in_executor(None, lambda: handler(**tool_args))
                            except Exception as e:
                                result = f"Error: {e}"
                                logger.error(f"Tool {tool_name} failed: {e}")
                        else:
                            result = f"Unknown tool: {tool_name}"

                        is_error = isinstance(result, str) and (
                            result.startswith("Error:") or result.startswith("Unknown tool:")
                        )
                        if is_error:
                            total_tool_errors += 1
                        else:
                            no_progress_turns = 0

                        self.messages.append({
                            "role": "tool",
                            "content": str(result),
                            "tool_call_id": tool_id,
                            "name": tool_name,
                        })

                        if total_tool_errors >= MAX_TOOL_ERRORS:
                            circuit_breaker_reason = f"too many tool errors ({total_tool_errors})"
                            break
                        if repeated_tool_call_streak >= MAX_REPEATED_TOOL_CALLS:
                            circuit_breaker_reason = f"repeated tool call ({repeated_tool_call_streak}x)"
                            break

                    if self._stop_requested:
                        logger.info("Early stop requested by tool")
                        self._stop_requested = False
                        return content or "Done."

                    if not is_error:
                        no_progress_turns = 0
                    else:
                        no_progress_turns += 1
                        if no_progress_turns >= MAX_NO_PROGRESS_TURNS and not circuit_breaker_reason:
                            circuit_breaker_reason = f"no progress ({no_progress_turns} turns)"

                    if circuit_breaker_reason:
                        logger.warning(f"Circuit breaker: {circuit_breaker_reason}")
                        self.messages.append({
                            "role": "user",
                            "content": (
                                "[Circuit breaker triggered. Stop tool exploration. "
                                "Respond with a concise summary of findings so far.]"
                            ),
                        })
                        break

                    continue  # Loop for next LLM response

                # No tool calls — final response
                if content:
                    self.messages.append({"role": "assistant", "content": content})
                    return content

                # Empty response
                empty_count += 1
                if empty_count >= 2:
                    logger.warning("Multiple empty responses, forcing final")
                    self.messages.append({
                        "role": "user",
                        "content": "[Respond now with what you know.]",
                    })
                    response = await self._call_api(no_tools=True)
                    content = response["choices"][0]["message"].get("content", "")
                    if content:
                        self.messages.append({"role": "assistant", "content": content})
                        return content
                    return "Done."

                self.messages.append({
                    "role": "user",
                    "content": "[Empty response. Please respond with your findings.]",
                })
                continue

            # Exhausted tool iterations for this continuation
            if continuation_depth >= MAX_CONTINUATION_DEPTH or circuit_breaker_reason:
                break

            continuation_depth += 1
            logger.info(f"Auto-continuing (depth {continuation_depth}/{MAX_CONTINUATION_DEPTH})")
            nudge = (
                f"[You've made {total_tool_calls} tool calls. "
                f"Wrap up and respond with your findings.]"
            )
            self.messages.append({"role": "user", "content": nudge})

        # Final response without tools
        response = await self._call_api(no_tools=True)
        content = response["choices"][0]["message"].get("content", "")
        if content:
            self.messages.append({"role": "assistant", "content": content})
            return content

        return "Task processing limit reached."

    async def _call_api(self, no_tools: bool = False) -> dict:
        """Make the litellm API call."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.system_prompt}] + self.messages,
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
        }

        tools = self._build_tool_specs()
        if tools and not no_tools:
            kwargs["tools"] = tools

        try:
            response = await acompletion(**kwargs)
            return response.model_dump()
        except Exception as e:
            logger.error(f"LLM API error: {e}")
            # Retry once after a short delay
            await asyncio.sleep(2)
            try:
                response = await acompletion(**kwargs)
                return response.model_dump()
            except Exception as retry_err:
                raise RuntimeError(f"LLM API failed after retry: {retry_err}") from retry_err
