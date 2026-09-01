"""Minimal markdown → HTML för passbeskrivningar.

Passets innehåll — uppvärmning, varje övning, set, reps, vila, nedvarvning —
ligger i ``planned_sessions.details`` som fritext. Både motorn och en AI-coach
skriver markdown där: ``##``-rubriker, ``**fetstil**`` och listor. Fältet
renderades tidigare rakt in i ett ``<p>``, så taggarna visades som tecken och
varje radbrytning kollapsade till mellanslag. Ett pass med tolv övningar blev en
enda textmassa, och adepten drog slutsatsen att övningarna var borttagna.

Varför inte ``markdown``-paketet: Trixa håller ``requirements.txt`` kort med
flit, och ytan vi behöver är den delmängd som faktiskt förekommer i passtext.
Allt escapas FÖRE konverteringen — texten kan komma från en språkmodell, och
ingen väg genom den här funktionen får släppa igenom HTML från indata.
"""

from __future__ import annotations

import re
from html import escape

from markupsafe import Markup

# Fetstil före kursiv: annars äter kursiv-regeln den ena asterisken i "**".
_INLINE = (
    (re.compile(r"\*\*(.+?)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"), r"<em>\1</em>"),
    (re.compile(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])"), r"<em>\1</em>"),
    (re.compile(r"`(.+?)`"), r"<code>\1</code>"),
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def _inline(text: str) -> str:
    """Escapa först, formatera sedan. Ordningen är hela säkerheten här."""
    out = escape(text)
    for pattern, repl in _INLINE:
        out = pattern.sub(repl, out)
    return out


def render(text: str | None) -> Markup:
    """Passtext → HTML. Tom text ger tom sträng, aldrig None-artefakter."""
    if not text or not str(text).strip():
        return Markup("")

    html: list[str] = []
    list_tag: str | None = None      # öppen <ul>/<ol>, eller None
    paragraph: list[str] = []

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            html.append(f"</{list_tag}>")
            list_tag = None

    def close_paragraph() -> None:
        if paragraph:
            html.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()

    for raw in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()

        if not line.strip():
            close_paragraph()
            close_list()
            continue

        heading = _HEADING.match(line)
        if heading:
            close_paragraph()
            close_list()
            # Passtexten sitter inne i ett kort — h1/h2 skulle skrika över
            # passets egen titel. Nivåerna klämms in i h4-h6.
            level = min(6, 3 + len(heading.group(1)))
            html.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue

        bullet = _BULLET.match(line)
        numbered = None if bullet else _NUMBERED.match(line)
        if bullet or numbered:
            close_paragraph()
            wanted = "ul" if bullet else "ol"
            if list_tag != wanted:
                close_list()
                html.append(f"<{wanted}>")
                list_tag = wanted
            item = (bullet or numbered).group(1)
            html.append(f"<li>{_inline(item)}</li>")
            continue

        close_list()
        paragraph.append(_inline(line.strip()))

    close_paragraph()
    close_list()
    return Markup("".join(html))
