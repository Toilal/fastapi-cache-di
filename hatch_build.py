"""Custom Hatchling metadata hook.

Builds the PyPI long description (``readme``) by concatenating ``README.md``
and ``CHANGELOG.md``.
"""

from pathlib import Path
from typing import Any

from hatchling.metadata.plugin.interface import MetadataHookInterface


def _balance_code_fences(text: str) -> str:
    """Make Markdown code fences safe to embed in the PyPI long description.

    Auto-generated changelog entries can carry a closing fence with trailing
    text on the same line, e.g.::

        ``` ([`abc`](https://.../abc))

    CommonMark does not treat such a line as a closing fence (a closing fence
    may only be followed by whitespace), so the code block is never terminated
    and swallows the rest of the document into a single ``<pre>`` block on PyPI.

    Normalise every closing fence onto its own line (moving any trailing text to
    the next line) and close a fence left open at end of input, so the assembled
    description always has balanced fences.
    """
    out: list[str] = []
    open_fence: str | None = None
    for line in text.split("\n"):
        stripped = line.lstrip()
        if not stripped.startswith("```"):
            out.append(line)
            continue
        backticks = len(stripped) - len(stripped.lstrip("`"))
        marker = "`" * backticks
        rest = stripped[backticks:].strip()
        if open_fence is None:
            # Opening fence: an info string (e.g. ```python) is valid, keep it.
            open_fence = marker
            out.append(line)
        else:
            # Inside a block: this closes it. A closing fence cannot carry an
            # info string, so emit the fence alone and push any trailing text
            # (typically the commit link) onto its own line.
            out.append(marker)
            if rest:
                out.append(rest)
            open_fence = None
    if open_fence is not None:
        out.append(open_fence)
    return "\n".join(out)


class CustomMetadataHook(MetadataHookInterface):
    """Assemble the long description from README.md + CHANGELOG.md."""

    def update(self, metadata: dict[str, Any]) -> None:
        root = Path(self.root)
        readme = (root / "README.md").read_text(encoding="utf-8")
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        metadata["readme"] = {
            "content-type": "text/markdown",
            "text": readme + "\n\n" + _balance_code_fences(changelog),
        }
