"""A very small DOM for asserting on rendered guest pages.

Built on the standard library's `html.parser` rather than a new dependency. It exists
so the accessibility rules in TAP-7728 are checked against parsed markup rather than
by grepping the template text, which would pass on a page that never rendered.
"""

from __future__ import annotations

from html.parser import HTMLParser

VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

FORM_CONTROLS = frozenset({"input", "select", "textarea"})

# Controls that carry no user-visible value of their own and so need no label.
UNLABELLED_INPUT_TYPES = frozenset({"hidden", "submit", "button", "reset", "image"})


class Element:
    """One parsed tag, with its attributes, its children and its own text.

    Text and child elements are held in a single ordered list rather than two
    separate ones. Keeping them apart reads `by <strong>December 15</strong>.` back
    as "by . December 15", which is the wrong document order and would let a
    substring assertion pass or fail for a reason that is not on the page.
    """

    def __init__(self, tag: str, attrs: dict[str, str], parent: Element | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.nodes: list[str | Element] = []

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<{self.tag} {self.attrs}>"

    @property
    def children(self) -> list[Element]:
        return [node for node in self.nodes if isinstance(node, Element)]

    @property
    def text(self) -> str:
        """This element's own text, not its descendants'."""
        return "".join(node for node in self.nodes if isinstance(node, str)).strip()

    @property
    def deep_text(self) -> str:
        """Every run of text under this element, in document order."""
        parts: list[str] = []
        for node in self.nodes:
            piece = node if isinstance(node, str) else node.deep_text
            if piece.strip():
                parts.append(piece.strip())
        return " ".join(parts)

    def ancestors(self) -> list[Element]:
        chain: list[Element] = []
        node = self.parent
        while node is not None:
            chain.append(node)
            node = node.parent
        return chain


class Document(HTMLParser):
    """Parses a page into an `Element` tree and answers questions about it."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#document", {}, None)
        self._stack: list[Element] = [self.root]
        self.all: list[Element] = []
        self.feed(html)
        self.close()

    # -- HTMLParser hooks -------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag, {k: (v if v is not None else "") for k, v in attrs}, self._stack[-1])
        self._stack[-1].nodes.append(element)
        self.all.append(element)
        if tag not in VOID_ELEMENTS:
            self._stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag, {k: (v if v is not None else "") for k, v in attrs}, self._stack[-1])
        self._stack[-1].nodes.append(element)
        self.all.append(element)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].nodes.append(data)

    # -- Queries ----------------------------------------------------------

    def find_all(self, tag: str, **attrs: str) -> list[Element]:
        return [
            element
            for element in self.all
            if element.tag == tag and all(element.attrs.get(k) == v for k, v in attrs.items())
        ]

    def find(self, tag: str, **attrs: str) -> Element | None:
        found = self.find_all(tag, **attrs)
        return found[0] if found else None

    @property
    def text(self) -> str:
        return self.root.deep_text

    def form_controls(self) -> list[Element]:
        """Every control a guest actually fills in."""
        return [
            element
            for element in self.all
            if element.tag in FORM_CONTROLS
            and element.attrs.get("type", "text").lower() not in UNLABELLED_INPUT_TYPES
        ]

    def unlabelled_controls(self) -> list[Element]:
        """Controls with no label: no wrapping `<label>`, no `for=`, no `aria-label`."""
        label_targets = {
            label.attrs["for"] for label in self.find_all("label") if label.attrs.get("for")
        }
        orphans = []
        for control in self.form_controls():
            if control.attrs.get("aria-label") or control.attrs.get("aria-labelledby"):
                continue
            if control.attrs.get("id") and control.attrs["id"] in label_targets:
                continue
            if any(ancestor.tag == "label" for ancestor in control.ancestors()):
                continue
            orphans.append(control)
        return orphans

    def faked_controls(self) -> list[Element]:
        """Divs and spans dressed up as controls, which the keyboard cannot reach."""
        return [
            element
            for element in self.all
            if element.tag in {"div", "span"}
            and (element.attrs.get("role") or element.attrs.get("onclick"))
        ]
