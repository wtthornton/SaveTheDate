"""The Jinja environment for the guest-facing pages.

Times are stored as `timestamptz`, which is UTC with the zone discarded, so every
filter here takes the event's IANA zone and converts before formatting. Rendering a
schedule in the server's zone is how the Friday welcome party ends up shown on
Saturday.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Keyed by what the file looked like when it was hashed, so a changed file is hashed
# again and an unchanged one is not.
_FINGERPRINTS: dict[tuple[str, int, int], str] = {}


def static_url(relative: str) -> str:
    """`/static/app.css?v=<content hash>` — a new URL whenever the bytes change.

    Cloudflare returns `/static/*` with `max-age=14400` and caches it at the edge, so
    a stylesheet published under a fixed URL keeps being served for four hours after
    it changes. That is not theoretical: a rebuild mid-session left a reviewer looking
    at a page with none of its new rules — no card, no animation — and the server was
    serving the correct file the whole time. Nothing about the page said so, which is
    what made it expensive to work out.

    Fingerprinting fixes it at the cause. A caching header would not: the point is not
    to cache less, it is that changed bytes should live at a different address. The
    HTML itself is uncached (`cf-cache-status: DYNAMIC`), so the new URL is seen at
    once.
    """
    path = STATIC_DIR / relative
    stat = path.stat()
    key = (relative, stat.st_mtime_ns, stat.st_size)
    fingerprint = _FINGERPRINTS.get(key)
    if fingerprint is None:
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
        _FINGERPRINTS[key] = fingerprint
    return f"/static/{relative}?v={fingerprint}"


ORDINAL_WORDS = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
    6: "sixth",
    7: "seventh",
    8: "eighth",
    9: "ninth",
    10: "tenth",
    11: "eleventh",
    12: "twelfth",
    13: "thirteenth",
    14: "fourteenth",
    15: "fifteenth",
    16: "sixteenth",
    17: "seventeenth",
    18: "eighteenth",
    19: "nineteenth",
    20: "twentieth",
    21: "twenty-first",
    22: "twenty-second",
    23: "twenty-third",
    24: "twenty-fourth",
    25: "twenty-fifth",
    26: "twenty-sixth",
    27: "twenty-seventh",
    28: "twenty-eighth",
    29: "twenty-ninth",
    30: "thirtieth",
    31: "thirty-first",
}

TENS_WORDS = {
    20: "twenty",
    30: "thirty",
    40: "forty",
    50: "fifty",
    60: "sixty",
    70: "seventy",
    80: "eighty",
    90: "ninety",
}

UNIT_WORDS = {
    0: "",
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
    17: "seventeen",
    18: "eighteen",
    19: "nineteen",
}


def _in_zone(moment: datetime, timezone: str) -> datetime:
    return moment.astimezone(ZoneInfo(timezone))


def local_day(moment: datetime, timezone: str) -> str:
    """`Friday, February 11` — the day this actually falls on where the wedding is."""
    return _in_zone(moment, timezone).strftime("%A, %B %-d")


def local_weekday(moment: datetime, timezone: str) -> str:
    return _in_zone(moment, timezone).strftime("%A")


def local_time(moment: datetime, timezone: str) -> str:
    """`6:00 pm`, and `noon` rather than `12:00 pm`, which nobody says out loud."""
    local = _in_zone(moment, timezone)
    if local.hour == 12 and local.minute == 0:
        return "noon"
    if local.hour == 0 and local.minute == 0:
        return "midnight"
    return local.strftime("%-I:%M %p").replace("AM", "am").replace("PM", "pm")


def local_range(start: datetime, end: datetime | None, timezone: str) -> str:
    if end is None:
        return f"from {local_time(start, timezone)}"
    return f"{local_time(start, timezone)} to {local_time(end, timezone)}"


def _year_in_words(year: int) -> str:
    """2028 as `two thousand twenty-eight`, the American convention."""
    if not 2000 <= year <= 2099:  # pragma: no cover - the wedding is in 2028
        return str(year)
    remainder = year - 2000
    if remainder == 0:
        return "two thousand"
    if remainder < 20:
        return f"two thousand {UNIT_WORDS[remainder]}"
    tens, units = divmod(remainder, 10)
    words = TENS_WORDS[tens * 10]
    if units:
        words = f"{words}-{UNIT_WORDS[units]}"
    return f"two thousand {words}"


def formal_date(day: date) -> str:
    """`Sunday, the twentieth of February, two thousand twenty-eight`."""
    return (
        f"{day.strftime('%A')}, the {ORDINAL_WORDS[day.day]} of "
        f"{day.strftime('%B')}, {_year_in_words(day.year)}"
    )


def plain_date(day: date) -> str:
    """`February 20, 2028` — American order, for the print view and running text."""
    return day.strftime("%B %-d, %Y")


def deadline_date(moment: datetime, timezone: str) -> str:
    """The last day a guest may reply, in the event's own zone.

    The stored instant is exclusive — "RSVP by December 15" is held as midnight at the
    start of the 16th — so the day to put in front of a guest is the one containing the
    last moment they could still answer.
    """
    return plain_date((_in_zone(moment, timezone) - timedelta(seconds=1)).date())


# Every scheduled item gets a photograph. Several are of Port Aransas itself —
# the Tarpon Inn porch, the ferry — rather than generic stock, which is worth the
# search: a guest who knows the island will recognize them.
#
# Matched on keywords rather than a column, because `segments` is host-entered content
# and a wedding's schedule is not a fixed vocabulary. Anything unrecognized still gets
# a picture — see DEFAULT_SEGMENT_IMAGE — so no card can render blank.
SEGMENT_IMAGES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("ceremony", "reception", "vows"),
        "/static/img/gulf-evening.jpg",
        "The Gulf at dusk, the sunset reflected in the wet sand",
    ),
    (
        ("welcome", "beach party", "bonfire", "fire"),
        "/static/img/beach-fire.jpg",
        "A driftwood fire burning on the sand as the light goes",
    ),
    (
        ("golf",),
        "/static/img/golf-course.jpg",
        "A golf course in the late afternoon, water along the fairway",
    ),
    (
        ("fishing", "charter", "boat", "bay"),
        "/static/img/pier-sunset.jpg",
        "A figure on a fishing pier at sunset",
    ),
    (
        ("dinner", "bar", "town", "crawl"),
        "/static/img/dinner-table.jpg",
        "A long table laid for dinner by candlelight",
    ),
    (
        ("breakfast", "brunch", "departure", "coffee"),
        "/static/img/breakfast-coffee.jpg",
        "Coffee and a pastry on a table in the morning",
    ),
    (
        ("ferry", "travel", "driving", "arrive"),
        "/static/img/ferry-sunset.jpg",
        "The Port Aransas ferry crossing at sunset",
    ),
)

# Every card gets a photograph, including one for a segment nobody has thought of yet.
DEFAULT_SEGMENT_IMAGE = (
    "/static/img/gulf-sunset.jpg",
    "An orange and purple sunset over the Gulf of Mexico",
)


def segment_image(name: str) -> tuple[str, str]:
    """The picture and alt text for one scheduled item, as (src, alt).

    Never returns nothing: an unrecognized segment falls back to a Gulf sunset, so a
    schedule the hosts change later cannot leave a grey rectangle on the page.
    """
    lowered = name.casefold()
    for keywords, src, alt in SEGMENT_IMAGES:
        if any(word in lowered for word in keywords):
            return src, alt
    return DEFAULT_SEGMENT_IMAGE


# CC BY requires the credit to be visible to a reader, not filed in a repository. Kept
# beside the mapping above so a picture cannot be swapped without its credit following.
# CC0 images need no entry — see img/CREDITS.md for the full list either way.
#
# Keyed by filename rather than held as a bare list of names, because the public pages
# (TAP-7775, TAP-7781) show one photograph each rather than all ten. A page-wide credit
# naming five photographers, four of whose work is not on the page, is as wrong as a
# missing one — img/CREDITS.md says so in those words.
CC_BY_PHOTOGRAPHS: tuple[tuple[str, str, str], ...] = (
    ("beach-fire.jpg", "Mike Dickison", "CC BY 4.0"),
    ("porch-tarpon-inn.jpg", "Gruenemann", "CC BY 2.0"),
    ("ferry-sunset.jpg", "BlankBlankBlank", "CC BY 2.0"),
    ("breakfast-coffee.jpg", "Helen.Yang", "CC BY 2.0"),
    ("dinner-table.jpg", "Dennis Wong", "CC BY 2.0"),
)

PHOTO_CREDITS: tuple[tuple[str, str], ...] = tuple(
    (photographer, license_name) for _, photographer, license_name in CC_BY_PHOTOGRAPHS
)


def _sentence(names: list[str]) -> str:
    return f"Placeholder photography by {', '.join(names)}, used under Creative Commons licenses."


def photo_credit_line() -> str:
    """One readable sentence naming everyone whose license requires naming."""
    return _sentence([name for name, _ in PHOTO_CREDITS])


def photo_credit_for(*files: str) -> str:
    """The credit owed by exactly the photographs on one page, or an empty string.

    A page built only from CC0 photographs owes nobody a line and gets none. Passing
    the filenames rather than hard-coding the outcome is what makes that safe: swap a
    CC0 picture for a CC BY one and the credit appears on its own, instead of the page
    quietly breaching a license that nobody re-read.
    """
    names = [
        photographer
        for filename, photographer, _ in CC_BY_PHOTOGRAPHS
        if any(filename in used for used in files)
    ]
    return _sentence(names) if names else ""


templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["static_url"] = static_url
templates.env.globals["photo_credit_line"] = photo_credit_line
templates.env.globals["photo_credit_for"] = photo_credit_for
templates.env.globals["segment_image"] = segment_image
templates.env.filters["formal_date"] = formal_date
templates.env.filters["plain_date"] = plain_date
templates.env.globals["local_day"] = local_day
templates.env.globals["local_weekday"] = local_weekday
templates.env.globals["local_time"] = local_time
templates.env.globals["local_range"] = local_range
templates.env.globals["deadline_date"] = deadline_date
