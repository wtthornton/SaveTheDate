"""The committed stylesheet has to actually contain what the templates ask for.

`app/static/app.css` is a build artifact that is committed on purpose, so a deploy
never has to run Tailwind. The cost of that choice is that it can fall behind the
templates silently: a class added to a template that nobody rebuilt for simply does
nothing, and the page still renders, just wrong.

That is not hypothetical. `h-[160px]` and `lg:h-[260px]` on the ferry photograph in
`wedding.html` were missing from the committed CSS, so the image had no height cap at
all and rendered at its natural 900x599 aspect — most of a phone screen, where 160px
was intended. Every test was green, including the browser-driven visual suite, because
the page was perfectly valid; it just was not the page anyone had designed.

This checks the property that was actually broken: every *Tailwind-shaped* class a
template names exists as a selector in the built stylesheet. It needs no Tailwind
binary, so it runs in CI.

"Tailwind-shaped" means the token carries a variant (`lg:h-[260px]`) or an arbitrary
value (`h-[160px]`). That is a deliberate scope, not a loophole. Bare semantic names
like `beat` and `beat-media` are hooks that `tests/test_visual.py` selects on and are
*supposed* to have no rule of their own, so a test that demanded one would be wrong
about them. Every class that vanishes silently when the stylesheet goes stale is in
the checked set, because a generated utility is exactly what a rebuild produces.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS_PATH = Path(__file__).resolve().parent.parent / "app" / "static" / "app.css"
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "app" / "templates"

CLASS_ATTR = re.compile(r'class="([^"]*)"', re.DOTALL)
JINJA_BLOCK = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.DOTALL)
JINJA_EXPR = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
JINJA_STRING = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def _is_generated_utility(token: str) -> bool:
    """A token Tailwind must have generated a rule for, so its absence is a defect.

    A variant (`lg:`, `hover:`) or an arbitrary value (`[160px]`) can only come from
    Tailwind. A bare name cannot be told apart from a bespoke hook, so it is left out
    — see the module docstring.
    """
    return ("[" in token or ":" in token) and not token.startswith(("group", "peer"))


def _escape(token: str) -> str:
    """Escape a class name the way Tailwind writes it into a CSS selector."""
    return "".join(char if (char.isalnum() or char in "-_") else "\\" + char for char in token)


def _classes_used() -> dict[str, set[str]]:
    """Every literal class name each template names, Jinja conditionals included."""
    used: dict[str, set[str]] = {}
    for template in sorted(TEMPLATE_DIR.glob("*.html")):
        source = JINJA_BLOCK.sub(" ", template.read_text())
        tokens: set[str] = set()
        for value in CLASS_ATTR.findall(source):
            # A conditional class lives inside `{{ ' foo' if x else '' }}`; take the
            # string literals out of the expression and drop the expression itself.
            for expression in JINJA_EXPR.findall(value):
                for single, double in JINJA_STRING.findall(expression):
                    tokens.update((single or double).split())
            tokens.update(JINJA_EXPR.sub(" ", value).split())
        used[template.name] = {
            token for token in tokens if _is_generated_utility(token) and "{" not in token
        }
    return used


def test_every_class_the_templates_name_exists_in_the_built_stylesheet() -> None:
    css = CSS_PATH.read_text()
    missing: list[str] = []

    for template, tokens in _classes_used().items():
        for token in sorted(tokens):
            selector = re.compile(r"\." + re.escape(_escape(token)) + r"(?![a-zA-Z0-9_-])")
            if not selector.search(css):
                missing.append(f"{template}: {token}")

    assert not missing, (
        "classes used in a template but absent from app/static/app.css — rebuild it "
        "with `~/.local/bin/tailwindcss -i app/static/src/app.css -o app/static/app.css` "
        "and commit the result:\n  " + "\n  ".join(missing)
    )


def test_the_ferry_photograph_is_capped_on_a_phone() -> None:
    """The specific regression above, named so it cannot be quietly generalized away.

    900x599 at phone width is ~233px tall with no cap. The design says 160px.
    """
    css = CSS_PATH.read_text()
    wedding = (TEMPLATE_DIR / "wedding.html").read_text()

    assert 'src="/static/img/ferry-sunset.jpg"' in wedding
    assert "h-[160px]" in wedding, "the ferry photo lost its phone height cap"
    assert re.search(r"\.h-\\\[160px\\\]\s*\{\s*height:\s*160px", css), (
        "h-[160px] is used by a template but has no rule in the built stylesheet"
    )


# -- The stylesheet has to reach the browser, not just exist ----------------


def test_the_stylesheet_url_carries_a_content_fingerprint() -> None:
    """Cloudflare hands `/static/*` back with `max-age=14400` and caches it at the edge.

    Under a fixed URL that means a rebuilt stylesheet keeps being served for four
    hours. It happened: a reviewer spent a while looking at a page with none of its
    new rules — no card, no animation — while the server served the correct file the
    whole time and nothing about the page said so.

    A changed file has to live at a changed address. This asserts the address moves.
    """
    from app.templating import static_url

    first = static_url("app.css")
    assert first.startswith("/static/app.css?v="), first
    assert len(first.split("?v=")[1]) >= 8, "the fingerprint is too short to be a hash"

    # Same bytes, same address: a redeploy of identical content must not bust caches.
    assert static_url("app.css") == first


def test_a_rebuilt_stylesheet_gets_a_new_url(tmp_path: Path) -> None:
    """The property that actually matters, exercised by changing a file's bytes."""
    from app import templating

    scratch = tmp_path / "probe.css"
    scratch.write_text("a{}")
    original_dir = templating.STATIC_DIR
    templating.STATIC_DIR = tmp_path
    try:
        before = templating.static_url("probe.css")
        scratch.write_text("a{color:red}")
        after = templating.static_url("probe.css")
    finally:
        templating.STATIC_DIR = original_dir

    assert before != after, "the URL did not change when the file did"


def test_every_page_asks_for_the_fingerprinted_stylesheet() -> None:
    """A template that hard-codes `/static/app.css` opts itself back into the bug."""
    offenders = [
        template.name
        for template in sorted(TEMPLATE_DIR.glob("*.html"))
        if 'href="/static/app.css"' in template.read_text()
    ]

    assert offenders == [], f"templates bypassing static_url(): {offenders}"
