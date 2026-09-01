"""Rendering of an MCP CallToolResult for the platform socket channel."""


def _result_text(tool_result) -> str:
    content = getattr(tool_result, "content", None) or []
    if not content:
        return ""
    first = content[0]
    text = getattr(first, "text", None)
    if text is None:
        text = getattr(first, "data", None)
    return str(text) if text is not None else str(first)


def render_tool_result(tool_result):
    """Plain text on success, a minimal isError envelope on failure.

    The success shape stays a bare string so the platform's forwarding contract and
    everything downstream of it is unchanged; only a failure grows a shape, because a
    flattened string gives the platform no way to tell a failure from output.
    """
    text = _result_text(tool_result)
    if getattr(tool_result, "isError", False):
        return {"isError": True, "content": [{"type": "text", "text": text}]}
    return text
