"""Visual and layout tests, driven by a real browser.

These exist because of a concrete failure: the welcome page shipped with an empty grey
box where the design has a full-bleed photographic hero, and all 45 existing tests
stayed green. They test structure, phases, labels and type sizes — nothing that knows
what the page looks like.

So these load the real pages in Chromium at phone and desktop widths, measure computed
geometry against the design canvas, and write full-page screenshots to
`tests/screenshots/` for a human to look at. The screenshots are the point as much as
the assertions: a person can see a regression an assertion was never written for.

The numbers below come from the artboards in the design canvas:
  Main.dc.html        390x1880   mobile welcome
  RsvpForm.dc.html    390x2160   mobile RSVP
  Welcome.dc.html     1440x2280  desktop welcome
  DesktopRsvp.dc.html 1440x1380  desktop RSVP
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, FloatRect, Page, ViewportSize, sync_playwright
from sqlalchemy.orm import Session

from tests.factories import add_guest, add_segments, create_event

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHOTS = PROJECT_ROOT / "tests" / "screenshots"

PHONE: ViewportSize = {"width": 390, "height": 844}
DESKTOP: ViewportSize = {"width": 1440, "height": 900}

# The design's own hero heights, which the build should be close to.
HERO_PHONE_PX = 520
HERO_DESKTOP_PX = 760

MINIMUM_BODY_PX = 18.0
MINIMUM_TAP_PX = 44.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="session")
def live_url(_migrated_schema: None, _test_database_url: str) -> Iterator[str]:
    """A real uvicorn, because a browser cannot talk to an in-process TestClient."""
    port = _free_port()
    env = {**os.environ, "DATABASE_URL": _test_database_url, "REVIEW_INSTANCE": "false"}
    process = subprocess.Popen(
        [
            str(PROJECT_ROOT / ".venv" / "bin" / "uvicorn"),
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read().decode() if process.stdout else ""
                raise RuntimeError(f"the app exited before serving:\n{output}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("the app never started listening")
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture
def token(client: Any, db_session: Session) -> str:
    """A guest whose RSVP window is open, seeded fresh for each test."""
    from datetime import UTC, datetime, timedelta

    event = create_event(
        client,
        rsvp_opens_at=datetime.now(UTC) - timedelta(days=1),
        rsvp_deadline=datetime.now(UTC) + timedelta(days=30),
    )
    add_segments(db_session, event["id"])
    return str(add_guest(client, event["id"], "Jordan Lee", party_size=2)["invite_token"])


def _page(browser: Browser, viewport: ViewportSize) -> Page:
    context = browser.new_context(viewport=viewport, device_scale_factor=2)
    return context.new_page()


def _shoot(page: Page, name: str) -> Path:
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    return path


def _boxes_of(page: Page, selector: str) -> list[FloatRect]:
    """Every rendered box for a selector, with the un-rendered ones dropped."""
    found: list[FloatRect] = []
    for element in page.query_selector_all(selector):
        box = element.bounding_box()
        if box is not None:
            found.append(box)
    return found


def _box(page: Page, selector: str) -> FloatRect:
    element = page.query_selector(selector)
    assert element is not None, f"no element matched {selector!r}"
    box = element.bounding_box()
    assert box is not None, f"{selector!r} is present but has no box (display:none?)"
    return box


# -- The screenshots, which are the point ---------------------------------


@pytest.mark.parametrize("width_name,viewport", [("phone", PHONE), ("desktop", DESKTOP)])
@pytest.mark.parametrize(
    "page_name,suffix",
    [
        ("welcome", ""),
        ("wedding", "/wedding"),
        ("rsvp", "/rsvp"),
        ("print", "/print"),
    ],
)
def test_capture_every_page(
    browser: Browser,
    live_url: str,
    token: str,
    width_name: str,
    viewport: ViewportSize,
    page_name: str,
    suffix: str,
) -> None:
    """Write a full-page screenshot of every page at both widths, and sanity-check it.

    The assertions here are deliberately shallow — the file on disk is the deliverable.
    """
    page = _page(browser, viewport)
    page.goto(f"{live_url}/invites/{token}{suffix}", wait_until="networkidle")
    shot = _shoot(page, f"{page_name}-{width_name}")

    assert shot.stat().st_size > 5000, f"{shot.name} looks blank"
    # Nothing should ever scroll sideways on a phone.
    overflow = page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
    )
    assert not overflow, f"{page_name} at {width_name} scrolls horizontally"
    page.context.close()


# -- Layout against the design -------------------------------------------


def test_the_hero_is_full_bleed(browser: Browser, live_url: str, token: str) -> None:
    """The design's hero runs edge to edge. A centered column is the wrong shape."""
    for name, viewport, expected in (
        ("phone", PHONE, HERO_PHONE_PX),
        ("desktop", DESKTOP, HERO_DESKTOP_PX),
    ):
        page = _page(browser, viewport)
        page.goto(f"{live_url}/invites/{token}", wait_until="networkidle")
        hero = _box(page, ".hero")

        assert hero["x"] <= 1, f"{name}: hero is inset {hero['x']}px, not full bleed"
        assert hero["width"] >= viewport["width"] - 1, (
            f"{name}: hero is {hero['width']}px wide in a {viewport['width']}px viewport"
        )
        assert abs(hero["height"] - expected) <= expected * 0.2, (
            f"{name}: hero is {hero['height']}px, design says about {expected}px"
        )
        page.context.close()


def test_guests_sit_side_by_side_on_a_desktop(browser: Browser, live_url: str, token: str) -> None:
    """DesktopRsvp.dc.html puts the guest fieldsets in a two-column grid.

    On a 1440px screen, stacking them wastes most of the width and pushes the rest of
    the form below the fold for no reason.
    """
    page = _page(browser, DESKTOP)
    page.goto(f"{live_url}/invites/{token}/rsvp", wait_until="networkidle")
    _shoot(page, "rsvp-desktop-guests")

    boxes = _boxes_of(page, "form fieldset")
    assert len(boxes) >= 2, "expected a fieldset per guest"
    first, second = boxes[0], boxes[1]

    assert abs(first["y"] - second["y"]) < 40, (
        f"guest fieldsets are stacked: y={first['y']} and y={second['y']}"
    )
    assert second["x"] > first["x"] + first["width"] / 2, (
        "the second guest does not sit to the right of the first"
    )
    page.context.close()


def test_a_guest_column_is_not_split_again(browser: Browser, live_url: str, token: str) -> None:
    """Two guests side by side already halves the width. Halving it again is too far.

    The artboard puts the weekend and the paid extras in a two-column grid at form
    level, but this form is per person, so that grid can only live inside a guest card
    — where it yields four columns of about 200px and wraps "Golf at Palmilla" onto two
    lines. Inside a guest, the blocks stack.
    """
    page = _page(browser, DESKTOP)
    page.goto(f"{live_url}/invites/{token}/rsvp", wait_until="networkidle")

    split = page.evaluate(
        """() => {
            const bad = [];
            for (const fs of document.querySelectorAll('form fieldset')) {
                for (const el of fs.querySelectorAll('*')) {
                    const cs = getComputedStyle(el);
                    if (cs.display !== 'grid') continue;
                    const tracks = cs.gridTemplateColumns.split(' ').filter(Boolean);
                    if (tracks.length > 1) bad.push(
                        (el.className || el.tagName) + ' -> ' + cs.gridTemplateColumns);
                }
            }
            return bad;
        }"""
    )
    assert not split, "a guest column is subdivided into columns again:\n  " + "\n  ".join(split)
    page.context.close()


def test_the_couple_is_not_named_twice_on_one_screen(
    browser: Browser, live_url: str, token: str
) -> None:
    """The desktop RSVP carries the names in its side panel, as the artboard does.

    The mobile header repeating them in the form column is the phone layout leaking
    through, and it reads as a mistake rather than a flourish.
    """
    page = _page(browser, DESKTOP)
    page.goto(f"{live_url}/invites/{token}/rsvp", wait_until="networkidle")

    # Scan every element's OWN text rather than a guessed set of heading selectors:
    # the first version of this test looked only at h1/h2 and passed while the page
    # plainly showed the names twice, because the side panel uses a <p>. Own text also
    # stops an ancestor being counted for wrapping its children.
    showing = page.evaluate(
        """() => {
            const found = [];
            for (const el of document.querySelectorAll('body *')) {
                const own = Array.from(el.childNodes)
                    .filter(n => n.nodeType === Node.TEXT_NODE)
                    .map(n => n.textContent.trim()).join(' ');
                if (!/Bill/.test(own) || !/Lisa/.test(own)) continue;
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                found.push(el.tagName.toLowerCase() + '.' + (el.className || '(none)'));
            }
            return found;
        }"""
    )
    assert len(showing) <= 1, (
        f"the couple is named {len(showing)} times on the desktop RSVP: {showing}"
    )
    page.context.close()


def test_guests_stack_on_a_phone(browser: Browser, live_url: str, token: str) -> None:
    """The mobile artboard stacks them, and 390px has no room for two columns."""
    page = _page(browser, PHONE)
    page.goto(f"{live_url}/invites/{token}/rsvp", wait_until="networkidle")

    boxes = _boxes_of(page, "form fieldset")
    assert len(boxes) >= 2
    first, second = boxes[0], boxes[1]
    assert second["y"] > first["y"] + 40, "guest fieldsets are side by side on a phone"
    assert abs(first["x"] - second["x"]) < 2, "guest fieldsets are not aligned"
    page.context.close()


@pytest.mark.parametrize(
    "page_name,suffix", [("welcome", ""), ("wedding", "/wedding"), ("rsvp", "/rsvp")]
)
def test_no_page_is_a_long_thin_column_on_a_desktop(
    browser: Browser, live_url: str, token: str, page_name: str, suffix: str
) -> None:
    """Every page, not just the one that happened to be checked first.

    The original version of this test loaded only the welcome page, because it looked
    for `.beat`. The Wedding page therefore had no desktop layout at all and stayed
    green until somebody opened it and said it was "long and skinny". A test that only
    visits one of three pages is a test that covers one of three pages.

    The measure: how tall is the page relative to how wide its content gets. A real
    desktop layout is broad and comparatively short; a stretched phone is a ribbon.
    """
    page = _page(browser, DESKTOP)
    page.goto(f"{live_url}/invites/{token}{suffix}", wait_until="networkidle")

    shape = page.evaluate(
        """() => {
            const main = document.querySelector('main') || document.body;
            let widest = 0;
            for (const el of main.querySelectorAll('*')) {
                const cs = getComputedStyle(el);
                if (cs.display === 'none') continue;
                const own = Array.from(el.childNodes)
                    .filter(n => n.nodeType === Node.TEXT_NODE)
                    .map(n => n.textContent.trim()).join('');
                if (!own) continue;
                widest = Math.max(widest, el.getBoundingClientRect().width);
            }
            return {widest, height: document.documentElement.scrollHeight,
                    viewport: window.innerWidth};
        }"""
    )
    assert shape["widest"] > shape["viewport"] * 0.45, (
        f"{page_name}: the widest text block is {round(shape['widest'])}px in a "
        f"{shape['viewport']}px viewport — this is a phone column, not a desktop layout"
    )
    assert shape["height"] < 6000, (
        f"{page_name}: the desktop page is {shape['height']}px tall — content is "
        "stacking vertically instead of using the width"
    )
    page.context.close()


def test_the_welcome_beats_use_the_full_width(browser: Browser, live_url: str, token: str) -> None:
    """Welcome.dc.html gives the beats 470px pictures beside their text."""
    page = _page(browser, DESKTOP)
    page.goto(f"{live_url}/invites/{token}", wait_until="networkidle")

    beats = _boxes_of(page, ".beat")
    assert beats, "no .beat sections found on the welcome page"
    widest = max(box["width"] for box in beats)
    assert widest > DESKTOP["width"] * 0.6, (
        f"widest content block is {widest}px in a {DESKTOP['width']}px viewport — "
        "the desktop layout is a phone column"
    )
    page.context.close()


def test_the_beats_alternate_sides_on_a_desktop(
    browser: Browser, live_url: str, token: str
) -> None:
    """Welcome.dc.html alternates: image left, then image right, then image left."""
    page = _page(browser, DESKTOP)
    page.goto(f"{live_url}/invites/{token}", wait_until="networkidle")

    sides: list[str] = []
    for beat in page.query_selector_all(".beat"):
        media = beat.query_selector(".beat-media, .beat-thumb, .photo-slot")
        if media is None:
            continue
        box = beat.bounding_box()
        media_box = media.bounding_box()
        if box is None or media_box is None:
            continue
        sides.append("left" if media_box["x"] < box["x"] + box["width"] / 2 else "right")

    assert len(sides) >= 3, f"expected three beats, measured {len(sides)}"
    assert sides[0] != sides[1], f"beats 1 and 2 are on the same side: {sides}"
    page.context.close()


# -- Accessibility, measured in a browser rather than in the stylesheet ----


@pytest.mark.parametrize("width_name,viewport", [("phone", PHONE), ("desktop", DESKTOP)])
def test_no_rendered_text_is_below_18px(
    browser: Browser, live_url: str, token: str, width_name: str, viewport: ViewportSize
) -> None:
    """The stylesheet scan cannot see cascade, inheritance or Tailwind utilities.

    This reads the computed size of every element that actually has text in it, which
    is the number a guest's eyes get.
    """
    page = _page(browser, viewport)
    page.goto(f"{live_url}/invites/{token}/rsvp", wait_until="networkidle")

    offenders = page.evaluate(
        """() => {
            const bad = [];
            for (const el of document.querySelectorAll('body *')) {
                if (el.closest('[class*="eyebrow"]') || el.matches('[class*="eyebrow"]')) continue;
                const own = Array.from(el.childNodes)
                    .filter(n => n.nodeType === Node.TEXT_NODE)
                    .map(n => n.textContent.trim()).join('');
                if (!own) continue;
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                const px = parseFloat(cs.fontSize);
                if (px < 18) bad.push(el.tagName.toLowerCase() + '.' +
                    (el.className || '(none)') + ' = ' + px + 'px :: ' + own.slice(0, 40));
            }
            return bad;
        }"""
    )
    assert not offenders, f"{width_name}: text below {MINIMUM_BODY_PX}px:\n  " + "\n  ".join(
        offenders
    )
    page.context.close()


@pytest.mark.parametrize("width_name,viewport", [("phone", PHONE), ("desktop", DESKTOP)])
def test_no_text_renders_smaller_than_the_body_baseline(
    browser: Browser, live_url: str, token: str, width_name: str, viewport: ViewportSize
) -> None:
    """A font size in px is not a legibility measurement. x-height is.

    The hero date is 20px, which clears an 18px floor and reads as the larger number —
    but it is Cormorant Garamond, light, italic. Its x-height measures 8px against 9px
    for the 18px Karla body text, so the most important line on a save-the-date renders
    SMALLER than the paragraphs around it. A nominal-px check cannot see that.

    Baseline: the x-height of 18px Karla, the project's stated body-text floor.
    """
    page = _page(browser, viewport)
    page.goto(f"{live_url}/invites/{token}", wait_until="networkidle")
    page.wait_for_timeout(800)  # let the webfonts load, or every measurement is Georgia

    offenders = page.evaluate(
        """() => {
            const c = document.createElement('canvas').getContext('2d');
            const cache = new Map();
            // Measure the metric the text actually uses: x-height where there are
            // lowercase letters to sit on it, cap-height for all-caps runs like roman
            // numerals, where x-height describes nothing on screen. Using x-height for
            // everything creates pressure to reclassify all-caps text as an "eyebrow"
            // purely to escape a measurement that never applied to it.
            const metric = (font, allCaps) => {
                const key = font + '|' + allCaps;
                if (!cache.has(key)) {
                    c.font = font;
                    cache.set(key, c.measureText(allCaps ? 'H' : 'x').actualBoundingBoxAscent);
                }
                return cache.get(key);
            };
            const fontOf = (cs) =>
                `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
            const hasLower = (s) => /[a-z]/.test(s);

            const bad = [];
            for (const el of document.querySelectorAll('body *')) {
                const own = Array.from(el.childNodes)
                    .filter(n => n.nodeType === Node.TEXT_NODE)
                    .map(n => n.textContent.trim()).join('');
                if (!own) continue;
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;

                const isEyebrow = /eyebrow/.test(el.className || '');
                const px = parseFloat(cs.fontSize);
                const allCaps = !hasLower(own) ||
                                cs.textTransform === 'uppercase';
                const mine = metric(fontOf(cs), allCaps);
                const baseline = metric('normal 400 18px Karla, sans-serif', allCaps);

                if (isEyebrow) {
                    // The exemption exists for short tracked uppercase labels that are
                    // scanned, not read. It is not a way to make ordinary text small,
                    // so it still carries a floor and must actually look like a label.
                    const tracked = parseFloat(cs.letterSpacing) >= 1;
                    const upper = cs.textTransform === 'uppercase' ||
                                  own === own.toUpperCase();
                    if (!tracked || !upper) bad.push(
                        `${el.className} is exempted as an eyebrow but is not a tracked ` +
                        `uppercase label :: "${own.slice(0, 30)}"`);
                    else if (px < 13) bad.push(
                        `${el.className} eyebrow is ${px}px (floor is 13px) :: ` +
                        `"${own.slice(0, 30)}"`);
                    continue;
                }
                if (mine < baseline) bad.push(
                    `${el.tagName.toLowerCase()}.${el.className || '(none)'} ` +
                    `${cs.fontSize} ${cs.fontStyle} ${cs.fontWeight} — x-height ` +
                    `${mine.toFixed(1)}px vs ${baseline.toFixed(1)}px baseline :: ` +
                    `"${own.slice(0, 30)}"`);
            }
            return bad;
        }"""
    )
    assert not offenders, (
        f"{width_name}: text rendering smaller than 18px Karla:\n  " + "\n  ".join(offenders)
    )
    page.context.close()


@pytest.mark.parametrize("width_name,viewport", [("phone", PHONE), ("desktop", DESKTOP)])
def test_every_interactive_target_is_44px(
    browser: Browser, live_url: str, token: str, width_name: str, viewport: ViewportSize
) -> None:
    """Measured, not declared. A 44px min-height loses to a shorter computed box."""
    page = _page(browser, viewport)
    page.goto(f"{live_url}/invites/{token}/rsvp", wait_until="networkidle")

    small = page.evaluate(
        """(floor) => {
            const bad = [];
            const sel = 'a[href], button, summary, label:has(input), input:not([type=hidden])';
            for (const el of document.querySelectorAll(sel)) {
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                const r = el.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) continue;
                // A checkbox inside a big label inherits that label's target.
                if (el.tagName === 'INPUT' && el.closest('label')) {
                    const lr = el.closest('label').getBoundingClientRect();
                    if (lr.height >= floor) continue;
                }
                if (r.height < floor) bad.push(
                    el.tagName.toLowerCase() + '.' + (el.className || '(none)') +
                    ' = ' + Math.round(r.height) + 'px');
            }
            return bad;
        }""",
        MINIMUM_TAP_PX,
    )
    assert not small, f"{width_name}: tap targets under {MINIMUM_TAP_PX}px:\n  " + "\n  ".join(
        small
    )
    page.context.close()


def test_the_page_survives_200_percent_zoom(browser: Browser, live_url: str, token: str) -> None:
    """TAP-7728 requires 200% zoom without breaking layout.

    Halving the viewport while keeping the text size is the same thing to a layout.
    """
    page = _page(browser, {"width": 195, "height": 422})
    page.goto(f"{live_url}/invites/{token}/rsvp", wait_until="networkidle")
    _shoot(page, "rsvp-zoomed-200")

    overflow = page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
    )
    assert not overflow, "the RSVP form scrolls sideways at 200% zoom"
    page.context.close()
