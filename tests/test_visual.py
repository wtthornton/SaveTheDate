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
    # The card and the welcome are told apart by hostname, not by path, so the browser
    # has to be able to reach this one server under two names. Chromium resolves every
    # `*.localhost` name to loopback, which gives a second hostname with no DNS, no
    # hosts file and no second process.
    env = {
        **os.environ,
        "DATABASE_URL": _test_database_url,
        "REVIEW_INSTANCE": "false",
        "SAVE_THE_DATE_HOSTS": "savethedate.localhost",
    }
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


# -- The host dashboard (TAP-7730) ----------------------------------------


def _signed_in_host_page(browser: Browser, live_url: str, viewport: ViewportSize) -> Page:
    """A browser sitting on the dashboard, signed in through the real form."""
    from tests.conftest import HOST_EMAIL, HOST_PASSWORD

    page = _page(browser, viewport)
    page.goto(f"{live_url}/host/login", wait_until="networkidle")
    page.fill("#email", HOST_EMAIL)
    page.fill("#password", HOST_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    return page


@pytest.mark.parametrize(("label", "viewport"), [("phone", PHONE), ("desktop", DESKTOP)])
def test_capture_the_host_dashboard(
    browser: Browser, live_url: str, token: str, label: str, viewport: ViewportSize
) -> None:
    """Screenshots for a person to look at, same as the guest pages get."""
    page = _signed_in_host_page(browser, live_url, viewport)

    assert "/host/events/" in page.url, f"did not land on the dashboard: {page.url}"
    _shoot(page, f"host-dashboard-{label}")
    page.context.close()


def test_an_invite_link_is_readable_in_full(browser: Browser, live_url: str, token: str) -> None:
    """A clipped link is a link a host cannot check before sending it.

    It first shipped inside a table column, where it showed 478px of the 826px it
    needed and cut off mid-token. Nothing failed: the value was correct, the input was
    valid, and only looking at it showed the problem.
    """
    page = _signed_in_host_page(browser, live_url, DESKTOP)

    clipped = page.evaluate(
        """() => Array.from(document.querySelectorAll('.host-link-field'))
              .filter(el => el.scrollWidth > el.clientWidth + 1)
              .map(el => ({shown: Math.round(el.clientWidth),
                           needed: Math.round(el.scrollWidth)}))"""
    )

    assert not clipped, f"invite links are cut off: {clipped}"
    page.context.close()


def test_the_dashboard_link_points_at_the_host_being_browsed(
    browser: Browser, live_url: str, token: str
) -> None:
    """Built from the request, not from a setting — otherwise a host on the review
    tunnel copies a localhost link and sends it to somebody."""
    page = _signed_in_host_page(browser, live_url, DESKTOP)

    values = page.evaluate(
        "() => Array.from(document.querySelectorAll('.host-link-field')).map(el => el.value)"
    )

    assert values, "no invite links rendered"
    assert all(value.startswith(live_url) for value in values), values
    page.context.close()


def test_the_dashboard_does_not_scroll_sideways(
    browser: Browser, live_url: str, token: str
) -> None:
    """Tables may scroll inside their own box; the page itself may not."""
    for viewport in (PHONE, DESKTOP):
        page = _signed_in_host_page(browser, live_url, viewport)
        width = page.evaluate("() => document.body.scrollWidth")
        assert width <= viewport["width"] + 1, (
            f"the dashboard overflows at {viewport['width']}px: body is {width}px"
        )
        page.context.close()


# -- The public front door (TAP-7775, TAP-7781) ---------------------------

# Neither page takes a token, so neither uses the `token` fixture. That is the whole
# point of them and it is worth seeing in the signatures below.
PUBLIC_PAGES = ["public-welcome", "save-the-date"]


def _public_url(live_url: str, page_name: str) -> str:
    """Both pages are `/`. Which one you get is decided by the hostname you asked."""
    if page_name == "save-the-date":
        return live_url.replace("127.0.0.1", "savethedate.localhost")
    return live_url


# Every animation on the card that ends, having ended. Asking the browser which
# animations are still running beats naming the elements: the first version of this
# waited on the flap alone, and the flap finishes 900ms before the pocket does — so
# every screenshot was taken with half the card still inside the envelope, and not one
# assertion noticed, because none of them was looking at the pocket.
#
# The background drifts forever, so infinite animations are excluded rather than
# waited for.
REVEAL_SETTLED = """() => document.getAnimations()
    .filter(a => a.effect && a.effect.getComputedTiming().iterations !== Infinity)
    .every(a => a.playState === 'finished')"""


def _settled(page: Page) -> None:
    """Wait for the envelope to have finished opening, on pages that have one."""
    page.wait_for_function(REVEAL_SETTLED, timeout=8000)


# Pause every finite animation and move it to an exact moment. Waiting on the wall
# clock and screenshotting does not work for inspecting a reveal mid-flight: each
# screenshot costs a few hundred milliseconds and the error accumulates, so the fifth
# "frame" is nowhere near the time it claims. Scrubbing asks the browser to be at a
# time instead of hoping it is.
SCRUB_TO = """(t) => {
    document.getAnimations().forEach(a => {
      const timing = a.effect && a.effect.getComputedTiming();
      if (!timing || timing.iterations === Infinity) { a.cancel(); return; }
      a.pause();
      a.currentTime = Math.min(t, (timing.delay || 0) + (timing.activeDuration || 0));
    });
}"""


@pytest.mark.parametrize("width_name,viewport", [("phone", PHONE), ("desktop", DESKTOP)])
@pytest.mark.parametrize("page_name", PUBLIC_PAGES)
def test_capture_the_public_pages(
    browser: Browser,
    live_url: str,
    width_name: str,
    viewport: ViewportSize,
    page_name: str,
) -> None:
    """Screenshots of the two tokenless pages. The files are the deliverable."""
    page = _page(browser, viewport)
    page.goto(_public_url(live_url, page_name), wait_until="networkidle")
    _settled(page)
    shot = _shoot(page, f"{page_name}-{width_name}")

    assert shot.stat().st_size > 5000, f"{shot.name} looks blank"
    overflow = page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
    )
    assert not overflow, f"{page_name} at {width_name} scrolls horizontally"
    page.context.close()


@pytest.mark.parametrize("width_name,viewport", [("phone", PHONE), ("desktop", DESKTOP)])
@pytest.mark.parametrize("page_name", PUBLIC_PAGES)
def test_no_text_on_a_public_page_is_below_18px(
    browser: Browser,
    live_url: str,
    width_name: str,
    viewport: ViewportSize,
    page_name: str,
) -> None:
    """The same floor as everywhere else, and with no `eyebrow` escape hatch.

    The invitation pages exempt `.eyebrow*`, which was a mistake worth not repeating:
    these two pages carry no such class, so every word on them is measured.
    """
    page = _page(browser, viewport)
    page.goto(_public_url(live_url, page_name), wait_until="networkidle")
    _settled(page)

    offenders = page.evaluate(
        """() => {
            const bad = [];
            for (const el of document.querySelectorAll('body *')) {
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
    assert not offenders, f"{page_name} at {width_name}: text below {MINIMUM_BODY_PX}px:\n  " + (
        "\n  ".join(offenders)
    )
    page.context.close()


@pytest.mark.parametrize("width_name,viewport", [("phone", PHONE), ("desktop", DESKTOP)])
@pytest.mark.parametrize("page_name", PUBLIC_PAGES)
def test_every_target_on_a_public_page_is_44px(
    browser: Browser,
    live_url: str,
    width_name: str,
    viewport: ViewportSize,
    page_name: str,
) -> None:
    page = _page(browser, viewport)
    page.goto(_public_url(live_url, page_name), wait_until="networkidle")
    _settled(page)

    small = page.evaluate(
        """(floor) => {
            const bad = [];
            for (const el of document.querySelectorAll('a[href], button')) {
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                const r = el.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) continue;
                if (r.height < floor) bad.push(
                    el.tagName.toLowerCase() + '.' + (el.className || '(none)') +
                    ' = ' + Math.round(r.height) + 'px');
            }
            return bad;
        }""",
        MINIMUM_TAP_PX,
    )
    assert not small, f"{page_name} at {width_name}: targets under {MINIMUM_TAP_PX}px:\n  " + (
        "\n  ".join(small)
    )
    page.context.close()


def test_the_whole_card_is_visible_without_scrolling_on_a_phone(
    browser: Browser, live_url: str
) -> None:
    """A save-the-date is one glance. A date below the fold is a date nobody read."""
    page = _page(browser, PHONE)
    page.goto(_public_url(live_url, "save-the-date"), wait_until="networkidle")
    _settled(page)

    card = _box(page, ".std-card")
    viewport_height = page.evaluate("window.innerHeight")

    assert card["y"] >= 0, "the card starts above the top of the screen"
    assert card["y"] + card["height"] <= viewport_height + 1, (
        f"the card runs {round(card['y'] + card['height'] - viewport_height)}px below the fold"
    )
    page.context.close()


def test_the_public_welcome_fits_a_laptop_without_scrolling(
    browser: Browser, live_url: str
) -> None:
    """The whole page, on a 1440x900 laptop, with no scrollbar.

    Somebody who types the domain gets this page, and what they came for — that the
    invitation is a personal link — used to start below the fold behind a 702px hero.
    Bill asked for the hero and the banner to come down so the page fits, so "fits" is
    asserted rather than eyeballed once and left to rot.

    This is the assertion most likely to be broken by an innocent edit: one more
    paragraph, or a heading that wraps to a second line, silently puts it back over.
    Phone is deliberately NOT asserted — see the test below.
    """
    page = _page(browser, DESKTOP)
    page.goto(_public_url(live_url, "public-welcome"), wait_until="networkidle")

    scroll_height = page.evaluate("() => document.documentElement.scrollHeight")
    viewport_height = page.evaluate("() => window.innerHeight")
    assert scroll_height <= viewport_height, (
        f"the welcome page runs {scroll_height - viewport_height}px past a "
        f"{DESKTOP['width']}x{DESKTOP['height']} screen"
    )
    page.context.close()


# Where a line stops being comfortable to track. The classic range is 45-75
# characters; this allows a little slack for a long word landing badly, because the
# point is to catch a column that is wrong by twenty characters, not by two.
MAX_CHARACTERS_PER_LINE = 85


def test_no_paragraph_on_the_public_welcome_runs_too_long_a_line(
    browser: Browser, live_url: str
) -> None:
    """Measured from where the browser actually broke each line, not from the column width.

    Asked whether these paragraphs were too narrow, the measurement said the opposite:
    at 18px in a 632px column they ran 85 and 95 characters, past the point where the
    eye loses its place returning to the left margin. The fix was larger type rather
    than a wider column — a wider column would have made the real problem worse while
    fixing the apparent one.

    Character counts, not pixels, because that is the unit readability is actually
    measured in: the same column is fine at 20px and too wide at 16px.
    """
    page = _page(browser, DESKTOP)
    page.goto(_public_url(live_url, "public-welcome"), wait_until="networkidle")

    # Walk the text node one character at a time and watch for the top of its box to
    # change; that is where the browser wrapped, whatever the CSS says it should have.
    longest = page.evaluate(
        """() => Array.from(document.querySelectorAll('main p')).map(p => {
             const node = p.firstChild;
             if (!node || node.nodeType !== Node.TEXT_NODE) return null;
             const range = document.createRange();
             const lines = [];
             let start = 0, previousTop = null;
             for (let i = 0; i < node.length; i++) {
               range.setStart(node, i);
               range.setEnd(node, i + 1);
               const top = Math.round(range.getBoundingClientRect().top);
               if (previousTop === null) previousTop = top;
               else if (top !== previousTop) {
                 lines.push(i - start);
                 start = i;
                 previousTop = top;
               }
             }
             lines.push(node.length - start);
             return {text: node.textContent.trim().slice(0, 40), longest: Math.max(...lines)};
           }).filter(Boolean)"""
    )
    assert longest, "no paragraphs were measured; the page or the selector changed"

    too_long = [p for p in longest if p["longest"] > MAX_CHARACTERS_PER_LINE]
    offenders = "\n  ".join(f"{p['longest']}ch — {p['text']}…" for p in too_long)
    assert not too_long, f"lines longer than {MAX_CHARACTERS_PER_LINE} characters:\n  {offenders}"
    page.context.close()


@pytest.mark.parametrize("width_name,viewport", [("phone", PHONE), ("desktop", DESKTOP)])
def test_the_card_never_hangs_out_of_the_envelope_while_it_rises(
    browser: Browser, live_url: str, width_name: str, viewport: ViewportSize
) -> None:
    """Mid-reveal, no part of the card is painted below the envelope's bottom edge.

    `std-rise-out` starts the card at `translateY(46%)`, which puts nearly half its
    height below the envelope — and `.std-front` stops exactly at that edge, so for
    the whole rise the card's lower half was visible hanging out underneath the
    paper: the date, the place and the link, sitting on the beach below the envelope.
    It slid *past* the envelope instead of coming out of it.

    Every existing assertion measured the END state, where the transform is `none`
    and nothing overhangs, so all of them stayed green. It was found by scrubbing to
    the middle and looking.

    Hit-testing rather than measuring: a clipped element still reports its full
    `getBoundingClientRect`, so geometry cannot tell whether the overhang is
    actually painted. `elementFromPoint` respects the clip, which is the question.
    """
    page = _page(browser, viewport)
    page.goto(_public_url(live_url, "save-the-date"), wait_until="networkidle")

    # 1800ms is 100ms into the 1200ms rise, so the card is still almost fully
    # displaced; 2400 is halfway up. Both are moments a person would actually see.
    for moment_ms in (1800, 2100, 2400):
        page.evaluate(SCRUB_TO, moment_ms)
        leaked = page.evaluate(
            """() => {
                 const envelope = document.querySelector('.std-back').getBoundingClientRect();
                 const card = document.querySelector('.std-card');
                 const hits = [];
                 for (const frac of [0.15, 0.35, 0.5, 0.65, 0.85]) {
                   const x = envelope.left + envelope.width * frac;
                   for (const below of [6, 40, 120]) {
                     const el = document.elementFromPoint(x, envelope.bottom + below);
                     if (el && (el === card || card.contains(el))) {
                       hits.push(`${Math.round(x)},${Math.round(envelope.bottom + below)}`);
                     }
                   }
                 }
                 return hits;
               }"""
        )
        assert not leaked, (
            f"{width_name}: at {moment_ms}ms the card is painted below the envelope "
            f"at {leaked} — it is sliding past the envelope, not out of it"
        )
    page.context.close()


def test_the_flaps_own_animation_never_carries_opacity(browser: Browser, live_url: str) -> None:
    """Because `opacity` on that element silently flattens its 3D, and hides the liner.

    The flap has two faces: cream paper outside, a teal striped liner inside, with
    `backface-visibility: hidden` on both so the liner turns toward the reader as the
    flap passes vertical. That liner is the detail the envelope was rebuilt for, after
    Bill pointed at Greenvelope — "the detail that makes an envelope look chosen
    rather than generated".

    It was never once visible. `opacity` is a grouping property, so an element that
    carries one is forced to `transform-style: flat` regardless of its own rule; the
    flap's open animation faded it, which flattened its 3D context, which disabled
    `backface-visibility` on both faces, so the cream outer face simply never hid.

    Nothing could see this. `getComputedStyle` reports `preserve-3d` the whole time,
    because the computed value is not the used value, and every screenshot showed a
    plausible cream flap. The fade now lives on the faces, which are leaves and group
    nothing.

    Asserted against the animation the browser is actually running, rather than
    against the stylesheet text, so it holds however the rule is written.
    """
    page = _page(browser, DESKTOP)
    page.goto(_public_url(live_url, "save-the-date"), wait_until="networkidle")

    offenders = page.evaluate(
        """() => document.getAnimations()
             .filter(a => a.effect && a.effect.target
                       && a.effect.target.classList.contains('std-flap'))
             .flatMap(a => a.effect.getKeyframes())
             .filter(k => k.opacity !== undefined)
             .map(k => `offset ${k.offset}: opacity ${k.opacity}`)"""
    )
    assert not offenders, (
        "the flap's own animation fades it, which forces transform-style: flat and "
        f"hides the liner for the whole reveal — {offenders}"
    )

    # And the faces must still be the things that fade, or the flap never leaves.
    faces_fade = page.evaluate(
        """() => document.getAnimations()
             .filter(a => a.effect && a.effect.target
                       && a.effect.target.classList.contains('std-flap-face'))
             .flatMap(a => a.effect.getKeyframes())
             .some(k => k.opacity === '0' || k.opacity === 0)"""
    )
    assert faces_fade, "nothing fades the flap out; it will sit over the card forever"
    page.context.close()


def test_the_card_needs_no_javascript(browser: Browser, live_url: str) -> None:
    """The envelope is CSS. With scripting off the card is simply there, open.

    Asserted rather than reasoned about, because "it degrades gracefully" is the kind
    of claim that is true right up until somebody moves the reveal into a script.
    """
    context = browser.new_context(viewport=PHONE, device_scale_factor=2, java_script_enabled=False)
    page = context.new_page()
    page.goto(_public_url(live_url, "save-the-date"), wait_until="load")
    _shoot(page, "save-the-date-no-javascript")

    body = page.content()
    assert "February 20, 2028" in body
    assert "Port Aransas" in body
    card = _box(page, ".std-card")
    assert card["height"] > 200, "the card did not render without scripting"
    context.close()


def test_reduced_motion_gets_the_finished_card_and_no_movement(
    browser: Browser, live_url: str
) -> None:
    """Not a gentler animation — none at all, and the card already open.

    Somebody whose system asks for reduced motion is saying that drifting embers and
    a rotating flap make them unwell. The card is what the animation was working
    toward, so they get it immediately.
    """
    context = browser.new_context(viewport=PHONE, device_scale_factor=2, reduced_motion="reduce")
    page = context.new_page()
    page.goto(_public_url(live_url, "save-the-date"), wait_until="networkidle")
    _shoot(page, "save-the-date-reduced-motion")

    running = page.evaluate(
        "() => document.getAnimations().map(a => a.animationName || a.constructor.name)"
    )
    assert running == [], f"animations still running under reduced motion: {running}"

    # Whether each piece renders a box, not what its own `display` says. An element
    # inside a `display: none` parent still reports its own `display`, so asking that
    # question of the flap gives the wrong answer while the envelope around it is
    # correctly hidden. `getClientRects()` answers the question actually being asked:
    # is any of this painted?
    hidden = page.evaluate(
        """() => ['.std-back', '.std-flap', '.std-front', '.std-seal', '.std-motes']
               .filter(s => {
                   const el = document.querySelector(s);
                   return el && el.getClientRects().length > 0;
               })"""
    )
    assert hidden == [], f"envelope pieces still rendered under reduced motion: {hidden}"

    card = page.evaluate("() => getComputedStyle(document.querySelector('.std-card')).opacity")
    assert card == "1", "the card is not fully visible under reduced motion"
    context.close()


def test_the_envelope_gets_out_of_the_way_of_the_card(browser: Browser, live_url: str) -> None:
    """Once open, nothing sits over the card — in sight or in the hit test.

    Both halves of that matter and only one is visible. The pocket finishes at
    `opacity: 0` but stays in the layout, and a transparent element is still hit-
    tested: without `pointer-events: none` it comes to rest across the lower edge of
    the card, over the bottom of the link to the wedding site, and takes the taps that
    land there. A screenshot could never have shown that, so this asks the browser
    what is actually on top.
    """
    page = _page(browser, PHONE)
    page.goto(_public_url(live_url, "save-the-date"), wait_until="networkidle")
    _settled(page)

    covered = page.evaluate(
        """() => {
            const bad = [];
            const parts = ['.std-tag', '.std-names', '.std-day', '.std-place',
                           '.std-note', '.std-link'];
            for (const selector of parts) {
                const el = document.querySelector(selector);
                if (!el) { bad.push(selector + ' is missing'); continue; }
                const r = el.getBoundingClientRect();
                // Five points, not one. The centre alone passes while an overlay
                // clips across the bottom edge of a target — and the bottom edge of
                // the link is exactly where the emptied envelope comes to rest.
                const points = [
                    [r.x + r.width / 2, r.y + r.height / 2],
                    [r.x + 2, r.y + 2],
                    [r.right - 2, r.y + 2],
                    [r.x + 2, r.bottom - 2],
                    [r.right - 2, r.bottom - 2],
                ];
                for (const [x, y] of points) {
                    const top = document.elementFromPoint(x, y);
                    if (!top) { bad.push(selector + ' is off-screen'); break; }
                    if (top === el || el.contains(top) || top.contains(el)) continue;
                    bad.push(selector + ' is covered by ' + top.tagName.toLowerCase() +
                             '.' + (top.className || '(none)') +
                             ' at (' + Math.round(x) + ',' + Math.round(y) + ')');
                    break;
                }
            }
            return bad;
        }"""
    )
    assert covered == [], "the opened card is obstructed:\n  " + "\n  ".join(covered)

    # And it is actually painted. Laid out is not the same as visible: setting the
    # card to `opacity: 0` leaves its box, its size and its position all intact, so
    # every geometric assertion here — and the one that checks it renders without
    # scripting — passes on a page a reader would call blank.
    opacity = page.evaluate("() => getComputedStyle(document.querySelector('.std-card')).opacity")
    assert opacity == "1", f"the card finished the reveal at opacity {opacity}"
    page.context.close()


def test_capture_the_envelope_opening(browser: Browser, live_url: str) -> None:
    """Frames through the reveal, so the animation itself can be looked at.

    Every other test here measures the end state, which is the state the animation
    exists to get to and says nothing about how it gets there. A flap that opens
    through the card, a liner that never faces the reader, a seal that survives its
    own breaking — all of that is invisible to an assertion about the finished page
    and obvious in five stills.
    """
    page = _page(browser, PHONE)
    page.goto(_public_url(live_url, "save-the-date"), wait_until="networkidle")

    # Wall-clock offsets into the sequence: sealed, seal breaking, flap lifting,
    # card rising, envelope leaving.
    last = 0
    for index, moment_ms in enumerate((150, 900, 1500, 2300, 3600)):
        page.wait_for_timeout(moment_ms - last)
        last = moment_ms
        shot = _shoot(page, f"save-the-date-opening-{index}-{moment_ms}ms")
        assert shot.stat().st_size > 5000, f"{shot.name} looks blank"

    _settled(page)
    page.context.close()
