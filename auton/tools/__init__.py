"""Tool system for Auton agents.

Auto-generates OpenAI function schemas from Python functions with type hints
and Google-style docstrings. Ported from Lethe.
"""

import inspect
import re
from typing import Any, Callable, get_type_hints

# Global tool registry: name -> (function, schema)
TOOL_REGISTRY: dict[str, tuple[Callable, dict]] = {}


def _python_type_to_json(py_type) -> str:
    """Convert Python type to JSON schema type."""
    if py_type is None or py_type == type(None):
        return "string"
    type_name = getattr(py_type, "__name__", str(py_type))
    mapping = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "list": "array",
        "dict": "object",
    }
    return mapping.get(type_name, "string")


def _parse_docstring(docstring: str) -> tuple[str, dict[str, str]]:
    """Parse Google-style docstring into description and param descriptions."""
    if not docstring:
        return "", {}

    lines = docstring.strip().split("\n")
    description_lines = []
    param_descriptions = {}
    in_args = False
    current_param = None

    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("args:"):
            in_args = True
            continue
        elif stripped.lower().startswith("returns:"):
            in_args = False
            continue

        if in_args:
            match = re.match(r"(\w+)(?:\s*\([^)]*\))?\s*:\s*(.+)", stripped)
            if match:
                current_param = match.group(1)
                param_descriptions[current_param] = match.group(2)
            elif current_param and stripped:
                param_descriptions[current_param] += " " + stripped
        elif not in_args and stripped:
            description_lines.append(stripped)

    return " ".join(description_lines), param_descriptions


def function_to_schema(func: Callable) -> dict:
    """Generate OpenAI function schema from a Python function."""
    sig = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}

    description, param_docs = _parse_docstring(func.__doc__ or "")

    properties = {}
    required = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        param_type = hints.get(name, str)
        json_type = _python_type_to_json(param_type)
        prop: dict[str, Any] = {"type": json_type}
        if name in param_docs:
            prop["description"] = param_docs[name]
        properties[name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "name": func.__name__,
        "description": description or f"Execute {func.__name__}",
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def register_tool(func: Callable, schema: dict | None = None, name: str | None = None) -> None:
    """Register a tool in the global registry."""
    schema = schema or function_to_schema(func)
    if name:
        schema["name"] = name
    TOOL_REGISTRY[schema["name"]] = (func, schema)


def get_tools(names: list[str]) -> list[tuple[Callable, dict]]:
    """Get registered tools by name."""
    result = []
    for name in names:
        if name in TOOL_REGISTRY:
            result.append(TOOL_REGISTRY[name])
    return result
