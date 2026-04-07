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

from auton.budget import BudgetPlanner

logger = logging.getLogger(__name__)


def _repair_json_args(raw: str) -> dict | None:
    """Attempt to repair malformed JSON tool arguments from LLMs.

    Common issue: LLMs emit literal newlines inside JSON string values
    (e.g., in write_file content). This replaces unescaped control chars
    and retries parsing.
    """
    import re
    # Replace literal control characters inside strings with escaped versions
    # This handles the most common case: unescaped newlines in string values
    repaired = re.sub(r'[\x00-\x1f]', lambda m: {
        '\n': '\\n', '\r': '\\r', '\t': '\\t',
    }.get(m.group(), ''), raw)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # Try extracting JSON object if surrounded by extra text
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            # Try the control-char replacement on the extracted object
            repaired = re.sub(r'[\x00-\x1f]', lambda m: {
                '\n': '\\n', '\r': '\\r', '\t': '\\t',
            }.get(m.group(), ''), match.group())
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

    return None


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
                 budget_check: Callable[[], bool] | None = None,
                 on_event: Callable[[dict], None] | None = None,
                 budget_planner: BudgetPlanner | None = None,
                 output_tools: set[str] | None = None):
        self.model = model
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.messages: list[dict[str, Any]] = []
        self._tools: dict[str, tuple[Callable, dict]] = {}
        self._stop_requested = False
        self.usage = TokenUsage()
        self._budget_check = budget_check  # Returns True if budget exceeded
        self._on_event = on_event  # Granular event callback for streaming
        self._budget_planner = budget_planner  # Cost estimation & finalize trigger

        # Write gate: structural enforcement of incremental saves.
        # Auto-enabled for agents with a budget and write_file tool.
        # Cadence tightens as budget approaches limit.
        self._output_tools = output_tools or set()
        self._calls_since_save = 0
        self._write_gate_active = False

    def add_tool(self, func: Callable, schema: dict) -> None:
        """Register a tool function with its OpenAI function schema."""
        name = schema["name"]
        self._tools[name] = (func, schema)

    def _build_tool_specs(self) -> list[dict]:
        """Build OpenAI-format tool specs for the API call.

        When the write gate is active, only output tools (write_file,
        read_file, list_files, finish, check_budget) are included.
        """
        return [
            {"type": "function", "function": schema}
            for name, (_, schema) in self._tools.items()
            if not self._write_gate_active or name in self._output_tools
        ]

    @property
    def _save_cadence(self) -> int:
        """Dynamic save cadence based on budget consumption.

        Returns 0 (disabled) when there's no budget planner or no output tools.
        Tightens as budget approaches the limit:
          <50% used → every 5 data-gathering calls
          50-80%    → every 3
          >80%      → every 1 (save after each call)
        """
        if not self._output_tools or not self._budget_planner:
            return 0
        pct = self._budget_planner.pct_used
        if pct >= 80:
            return 1
        elif pct >= 50:
            return 3
        return 5

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

                # Budget planner: finalize before the next expensive API call
                if self._budget_planner and self._budget_planner.should_finalize():
                    logger.info(
                        f"BudgetPlanner: finalize triggered. "
                        f"Spent={self._budget_planner.total_spent:,}, "
                        f"remaining={self._budget_planner.remaining:,}"
                    )
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "[BUDGET LIMIT REACHED. You have used most of your token budget. "
                            "Write any output files NOW, then produce your final summary. "
                            "This is your last chance to save work.]"
                        ),
                    })
                    # Activate write gate so the agent can save but not search
                    self._write_gate_active = True
                    # Temporarily boost max_output_tokens for finalize — the agent
                    # needs to write potentially large files in tool call arguments
                    saved_max = self.max_output_tokens
                    self.max_output_tokens = max(saved_max, 16384)
                    finalize_resp = await self._call_api()
                    self.max_output_tokens = saved_max
                    fin_msg = finalize_resp["choices"][0]["message"]
                    fin_content = fin_msg.get("content") or ""
                    fin_tool_calls = fin_msg.get("tool_calls")
                    fin_usage = finalize_resp.get("usage", {})
                    if fin_usage:
                        self.usage.prompt_tokens += fin_usage.get("prompt_tokens", 0)
                        self.usage.completion_tokens += fin_usage.get("completion_tokens", 0)
                        self.usage.total_tokens += fin_usage.get("total_tokens", 0)
                        self._budget_planner.record_call(fin_usage.get("total_tokens", 0))

                    # Execute any tool calls (write_file to save final state)
                    if fin_tool_calls:
                        self.messages.append({
                            "role": "assistant",
                            "content": fin_content,
                            "tool_calls": fin_tool_calls,
                        })
                        for tc in fin_tool_calls:
                            t_name = tc["function"]["name"].strip()
                            t_id = tc.get("id") or f"call-{uuid.uuid4().hex[:12]}"
                            try:
                                t_args = json.loads(tc["function"]["arguments"])
                            except json.JSONDecodeError as e:
                                raw_args = tc["function"]["arguments"]
                                t_args = _repair_json_args(raw_args)
                                if t_args is None:
                                    logger.error(f"Finalize tool {t_name} malformed args: {e}\n  raw: {raw_args[:500]}")
                                    self.messages.append({
                                        "role": "tool",
                                        "content": f"Error: malformed arguments - {e}",
                                        "tool_call_id": t_id,
                                        "name": t_name,
                                    })
                                    continue
                                logger.info(f"Repaired malformed JSON for finalize tool {t_name}")
                            handler = self.get_tool(t_name)
                            if handler:
                                try:
                                    if asyncio.iscoroutinefunction(handler):
                                        t_result = await handler(**t_args)
                                    else:
                                        loop = asyncio.get_event_loop()
                                        t_result = await loop.run_in_executor(
                                            None, lambda: handler(**t_args)
                                        )
                                except Exception as e:
                                    t_result = f"Error: {e}"
                            else:
                                t_result = f"Unknown tool: {t_name}"
                            self.messages.append({
                                "role": "tool",
                                "content": str(t_result),
                                "tool_call_id": t_id,
                                "name": t_name,
                            })
                            if self._on_event:
                                self._on_event({
                                    "type": "tool_result",
                                    "tool_name": t_name,
                                    "tool_call_id": t_id,
                                    "result_preview": str(t_result)[:500],
                                    "is_error": False,
                                })
                        # Get final text response after saving
                        final_resp = await self._call_api(no_tools=True)
                        fin_content = final_resp["choices"][0]["message"].get("content", "")
                        final_usage = final_resp.get("usage", {})
                        if final_usage:
                            self.usage.prompt_tokens += final_usage.get("prompt_tokens", 0)
                            self.usage.completion_tokens += final_usage.get("completion_tokens", 0)
                            self.usage.total_tokens += final_usage.get("total_tokens", 0)
                            self._budget_planner.record_call(final_usage.get("total_tokens", 0))

                    if fin_content:
                        if self._on_event:
                            self._on_event({
                                "type": "llm_response",
                                "content": fin_content,
                                "tool_calls": [],
                            })
                        self.messages.append({"role": "assistant", "content": fin_content})
                    self._stop_requested = True
                    return fin_content or "Budget exhausted — finalizing."

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

                # Record call cost in budget planner
                if self._budget_planner:
                    self._budget_planner.record_call(usage.get("total_tokens", 0))

                # Inline budget check after every API call
                if self._budget_check and self._budget_check():
                    logger.warning(f"Budget exceeded at {self.usage.total_tokens} tokens")
                    self._stop_requested = True
                    if content:
                        self.messages.append({"role": "assistant", "content": content})
                    return content or "Budget exceeded."

                # Budget planner hard stop (115% absolute safety valve)
                if self._budget_planner and self._budget_planner.hard_stop():
                    logger.warning(
                        f"BudgetPlanner hard stop at {self._budget_planner.total_spent:,} tokens "
                        f"({self._budget_planner.pct_used:.0f}% of budget)"
                    )
                    self._stop_requested = True
                    if content:
                        self.messages.append({"role": "assistant", "content": content})
                    return content or "Budget hard limit exceeded."

                if tool_calls:
                    empty_count = 0
                    total_tool_calls += len(tool_calls)

                    # Ensure all tool calls have IDs
                    for tc in tool_calls:
                        if not tc.get("id"):
                            tc["id"] = f"call-{uuid.uuid4().hex[:12]}"

                    # Emit llm_response event
                    if self._on_event:
                        self._on_event({
                            "type": "llm_response",
                            "content": content,
                            "tool_calls": [
                                {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}
                                for tc in tool_calls
                            ],
                        })

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
                            raw_args = tool_call["function"]["arguments"]
                            tool_args = _repair_json_args(raw_args)
                            if tool_args is None:
                                logger.error(f"Tool {tool_name} malformed args: {e}\n  raw: {raw_args[:500]}")
                                total_tool_errors += 1
                                self.messages.append({
                                    "role": "tool",
                                    "content": f"Error: malformed arguments - {e}",
                                    "tool_call_id": tool_id,
                                    "name": tool_name,
                                })
                                continue
                            logger.info(f"Repaired malformed JSON for tool {tool_name}")

                        # Repeated call detection
                        signature = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
                        if signature == last_tool_signature:
                            repeated_tool_call_streak += 1
                        else:
                            repeated_tool_call_streak = 1
                            last_tool_signature = signature

                        if tool_name == "write_file":
                            logger.warning(f"write_file: path={tool_args.get('path', '?')}, content_len={len(tool_args.get('content', ''))}")
                        logger.info(f"Tool: {tool_name}({list(tool_args.keys())})")

                        # Emit tool_call event
                        if self._on_event:
                            self._on_event({
                                "type": "tool_call",
                                "tool_name": tool_name,
                                "tool_call_id": tool_id,
                                "arguments": tool_args,
                            })

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

                        # Emit tool_result event
                        if self._on_event:
                            self._on_event({
                                "type": "tool_result",
                                "tool_name": tool_name,
                                "tool_call_id": tool_id,
                                "result_preview": str(result)[:500],
                                "is_error": is_error,
                            })

                        self.messages.append({
                            "role": "tool",
                            "content": str(result),
                            "tool_call_id": tool_id,
                            "name": tool_name,
                        })

                        # Write gate tracking
                        if self._save_cadence > 0 and not is_error:
                            if tool_name in ("write_file", "finish"):
                                self._calls_since_save = 0
                                if self._write_gate_active:
                                    logger.info("Write gate: save detected, restoring all tools")
                                    self._write_gate_active = False
                            elif tool_name not in self._output_tools:
                                self._calls_since_save += 1

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

                    # Write gate activation: after N data-gathering calls without
                    # a save, physically remove non-output tools from the next call
                    if (self._save_cadence > 0
                            and not self._write_gate_active
                            and self._calls_since_save >= self._save_cadence):
                        self._write_gate_active = True
                        logger.info(
                            f"Write gate: activated after {self._calls_since_save} "
                            f"calls without save"
                        )
                        self.messages.append({
                            "role": "user",
                            "content": (
                                "[SAVE REQUIRED: You have gathered data from multiple "
                                "sources without saving. Write your findings to disk "
                                "now before continuing.]"
                            ),
                        })

                    continue  # Loop for next LLM response

                # No tool calls — final response
                if content:
                    if self._on_event:
                        self._on_event({
                            "type": "llm_response",
                            "content": content,
                            "tool_calls": [],
                        })
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
        # Prefill fix: inject [Continue] to prevent "assistant message prefill" error
        # with Anthropic models. Set PREFILL_FIX_ENABLED=false to disable (saves tokens).
        import os
        prefill_fix = os.getenv("PREFILL_FIX_ENABLED", "true").lower() != "false"
        if prefill_fix and self.messages and self.messages[-1].get("role") == "assistant":
            self.messages.append({
                "role": "user",
                "content": "[Continue]",
            })

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
