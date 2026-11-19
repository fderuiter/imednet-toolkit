"""Native Sphinx extension for interactive client-side Pyodide execution cells."""

from __future__ import annotations

import html
import json
import posixpath
from typing import Any

from docutils import nodes
from docutils.parsers.rst import Directive

DEFAULT_BUTTON_TEXT = "Activate Interactive Runtime"


class PyodideButtonNode(nodes.General, nodes.Element):
    """Docutils node representing an interactive Pyodide runtime launch button."""

    def __init__(
        self, rawsource: str = "", text: str = DEFAULT_BUTTON_TEXT, **attributes: Any
    ) -> None:
        super().__init__(rawsource, **attributes)
        self["text"] = text

    def to_html(self) -> str:
        """Render an accessible button element for client-side Pyodide initialization."""
        escaped_text = html.escape(str(self["text"]), quote=True)
        return (
            f'<button type="button" title="{escaped_text}" '
            f'class="thebe-launch-button pyodide-launch-button" '
            f'data-pyodide-launch="true">{escaped_text}</button>'
        )


class PyodideButtonDirective(Directive):
    """Directive registering ``.. pyodide-button::`` (and ``.. thebe-button::`` alias)."""

    optional_arguments = 1
    final_argument_whitespace = True
    has_content = False

    def run(self) -> list[nodes.Node]:
        text = self.arguments[0].strip() if self.arguments else DEFAULT_BUTTON_TEXT
        return [PyodideButtonNode(text=text or DEFAULT_BUTTON_TEXT)]


def _visit_pyodide_button_html(translator: Any, node: PyodideButtonNode) -> None:
    translator.body.append(node.to_html())
    raise nodes.SkipNode


def _skip_node(_translator: Any, _node: nodes.Node) -> None:
    raise nodes.SkipNode


def _compute_content_root(pagename: str) -> str:
    """Compute a relative trailing-slash path from ``pagename`` back to the HTML root."""
    parent = posixpath.dirname(pagename.strip("/"))
    if not parent:
        return "./"
    depth = len([part for part in parent.split("/") if part])
    return "../" * depth


def _inject_pyodide_page_context(
    app: Any,
    pagename: str,
    _templatename: str,
    context: dict[str, Any],
    _doctree: Any,
) -> None:
    """Ensure ``data-content_root`` and ``#imednet-pyodide-config`` JSON are present."""
    if not context.get("content_root"):
        context["content_root"] = _compute_content_root(pagename)

    raw_config = getattr(app.config, "pyodide_config", None)
    if not isinstance(raw_config, dict):
        return

    serialized = json.dumps(raw_config, sort_keys=True).replace("</", "<\\/")
    script_tag = (
        f'<script id="imednet-pyodide-config" type="application/json">{serialized}</script>\n'
    )
    existing_metatags = context.get("metatags", "")
    if 'id="imednet-pyodide-config"' not in existing_metatags:
        context["metatags"] = existing_metatags + script_tag


def setup(app: Any) -> dict[str, Any]:
    """Register Pyodide directives, nodes, configuration values, and HTML hooks."""
    app.add_config_value("pyodide_config", {}, "html")
    app.add_node(
        PyodideButtonNode,
        html=(_visit_pyodide_button_html, None),
        latex=(_skip_node, None),
        text=(_skip_node, None),
        man=(_skip_node, None),
        texinfo=(_skip_node, None),
        override=True,
    )
    app.add_directive("pyodide-button", PyodideButtonDirective, override=True)
    app.add_directive("thebe-button", PyodideButtonDirective, override=True)
    app.connect("html-page-context", _inject_pyodide_page_context)
    return {
        "version": "1.0.0",
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
