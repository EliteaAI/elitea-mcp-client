"""A flattened result string gives the platform no way to tell an MCP failure from
ordinary output, so isError enforcement upstream could never fire on the proxied path.
See EliteaAI/elitea_issues#6401."""
from types import SimpleNamespace

from src.elitea_mcp.utils.tool_result import render_tool_result


def _result(text, is_error=False, kind="text"):
    content = [SimpleNamespace(type=kind, text=text)]
    return SimpleNamespace(content=content, isError=is_error)


def test_success_stays_a_bare_string():
    """The success shape must not change: the platform forwards it verbatim and
    everything downstream of it expects the text."""
    assert render_tool_result(_result("all good")) == "all good"


def test_failure_becomes_an_is_error_envelope():
    rendered = render_tool_result(_result("repo not found", is_error=True))

    assert rendered == {
        "isError": True,
        "content": [{"type": "text", "text": "repo not found"}],
    }


def test_empty_content_does_not_raise():
    """content[0] used to be indexed unguarded, so an empty result was an IndexError."""
    assert render_tool_result(SimpleNamespace(content=[], isError=False)) == ""
    assert render_tool_result(SimpleNamespace(content=[], isError=True)) == {
        "isError": True,
        "content": [{"type": "text", "text": ""}],
    }


def test_non_text_content_falls_back_to_data():
    item = SimpleNamespace(type="image", data="base64blob")
    result = SimpleNamespace(content=[item], isError=False)

    assert render_tool_result(result) == "base64blob"


def test_missing_is_error_attribute_is_treated_as_success():
    assert render_tool_result(SimpleNamespace(content=[SimpleNamespace(text="hi")])) == "hi"
