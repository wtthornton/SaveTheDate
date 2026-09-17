"""The Jinja environment for the guest-facing pages.

Times are stored as `timestamptz`, which is UTC with the zone discarded, so every
filter here takes the event's IANA zone and converts before formatting. Rendering a
schedule in the server's zone is how the Friday welcome party ends up shown on
Saturday.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

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
    """`Sunday, the thirteenth of February, two thousand twenty-eight`."""
    return (
        f"{day.strftime('%A')}, the {ORDINAL_WORDS[day.day]} of "
        f"{day.strftime('%B')}, {_year_in_words(day.year)}"
    )


def plain_date(day: date) -> str:
    """`February 13, 2028` — American order, for the print view and running text."""
    return day.strftime("%B %-d, %Y")


def deadline_date(moment: datetime, timezone: str) -> str:
    """The last day a guest may reply, in the event's own zone.

    The stored instant is exclusive — "RSVP by December 15" is held as midnight at the
    start of the 16th — so the day to put in front of a guest is the one containing the
    last moment they could still answer.
    """
    return plain_date((_in_zone(moment, timezone) - timedelta(seconds=1)).date())


templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.filters["formal_date"] = formal_date
templates.env.filters["plain_date"] = plain_date
templates.env.globals["local_day"] = local_day
templates.env.globals["local_weekday"] = local_weekday
templates.env.globals["local_time"] = local_time
templates.env.globals["local_range"] = local_range
templates.env.globals["deadline_date"] = deadline_date
