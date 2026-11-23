"""HTML -> clean text, whitespace and location normalization."""
from __future__ import annotations

import html as _html
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

_BLOCK_CLOSE = re.compile(r"</(p|div|li|ul|ol|h[1-6]|tr|section|article)\s*>|<br\s*/?>", re.I)
_LI_OPEN = re.compile(r"<li[^>]*>", re.I)


def normalize_ws(text: str) -> str:
    text = text.replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_text(markup: str | None) -> str:
    if not markup:
        return ""
    markup = _LI_OPEN.sub("<li>- ", markup)
    markup = _BLOCK_CLOSE.sub(lambda m: m.group(0) + "\n", markup)
    tree = HTMLParser(markup)
    for node in tree.css("script, style, noscript"):
        node.decompose()
    root = tree.body or tree.root
    return normalize_ws(root.text(separator="") if root else "")


def unescape_then_text(escaped_html: str | None) -> str:
    """Greenhouse returns HTML entity-escaped HTML in `content`."""
    return html_to_text(_html.unescape(escaped_html or ""))


_LOCATION_ALIASES = {
    "bangalore": "Bengaluru",
    "bengaluru": "Bengaluru",
    "bombay": "Mumbai",
    "gurgaon": "Gurugram",
    "madras": "Chennai",
    "calcutta": "Kolkata",
}
_ALIAS_RE = re.compile(r"\b(" + "|".join(_LOCATION_ALIASES) + r")\b", re.I)


def normalize_location(loc: str | None) -> str:
    if not loc:
        return ""
    loc = _ALIAS_RE.sub(lambda m: _LOCATION_ALIASES[m.group(1).lower()], loc)
    loc = normalize_ws(loc.replace("\n", " "))
    return re.sub(r"\s*([|;])\s*", r" \1 ", loc).replace(" ; ", " | ")
