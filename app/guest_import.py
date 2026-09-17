"""Reading a guest list out of a spreadsheet. TAP-7732.

Guest lists start life in a spreadsheet, and adding 150 invitations one POST at a time
is not a workflow anybody would use.

**The whole file is validated before anything is written.** A partial import of a
wedding guest list is worse than a rejected one: the host cannot tell which rows landed
without reading the database, and re-running the file would then double the ones that
did. So parsing produces a plan, the plan is either wholly good or wholly refused, and
only a good plan is written.

Parsed in memory in the request, as the issue specifies. A sub-100-row file is a few
kilobytes; streaming, chunking or a background job would all be more machinery than the
problem has.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from email_validator import EmailNotValidError, validate_email

# The columns, however the spreadsheet spells them. A header is optional — a file that
# is just names and numbers is a perfectly reasonable thing to be handed.
NAME_HEADERS = frozenset({"name", "invitation", "guest", "guests", "party"})
EMAIL_HEADERS = frozenset({"email", "e-mail", "email address", "address"})
SEATS_HEADERS = frozenset({"party_size", "party size", "seats", "size", "count", "number"})

MAX_SEATS = 20
MAX_ROWS = 500


@dataclass(frozen=True)
class ImportRow:
    line: int
    name: str
    email: str | None
    party_size: int


@dataclass(frozen=True)
class ImportProblem:
    """One thing wrong, with the line number a host can find in their spreadsheet."""

    line: int
    message: str

    def __str__(self) -> str:
        return f"Line {self.line}: {self.message}"


@dataclass(frozen=True)
class ImportPlan:
    rows: list[ImportRow]
    problems: list[ImportProblem]

    @property
    def ok(self) -> bool:
        return not self.problems and bool(self.rows)


def _looks_like_a_header(cells: list[str]) -> bool:
    lowered = [cell.strip().casefold() for cell in cells]
    return any(cell in NAME_HEADERS for cell in lowered)


def _column_map(cells: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(cells):
        key = cell.strip().casefold()
        if key in NAME_HEADERS and "name" not in mapping:
            mapping["name"] = index
        elif key in EMAIL_HEADERS and "email" not in mapping:
            mapping["email"] = index
        elif key in SEATS_HEADERS and "party_size" not in mapping:
            mapping["party_size"] = index
    return mapping


def _cell(cells: list[str], columns: dict[str, int], key: str) -> str:
    """One field, tolerating a short row rather than raising on it."""
    index = columns.get(key)
    if index is None or index >= len(cells):
        return ""
    return cells[index].strip()


def _clean_email(raw: str, line: int, problems: list[ImportProblem]) -> str | None:
    if not raw:
        return None
    try:
        # No deliverability check: that is a DNS lookup per row, which would make an
        # import depend on the network and on somebody else's mail server being up.
        return str(validate_email(raw, check_deliverability=False).normalized)
    except EmailNotValidError as exc:
        problems.append(ImportProblem(line, f"{raw!r} is not a valid email address ({exc})"))
        return None


def _seats(raw: str, line: int, problems: list[ImportProblem]) -> int:
    if not raw:
        return 1
    if not raw.isdigit():
        problems.append(ImportProblem(line, f"{raw!r} is not a whole number of seats"))
        return 1
    value = int(raw)
    if not 1 <= value <= MAX_SEATS:
        problems.append(ImportProblem(line, f"{value} seats is outside 1–{MAX_SEATS}"))
        return 1
    return value


def parse(text: str, existing_names: set[str] | None = None) -> ImportPlan:
    """Turn a CSV into a plan, collecting *every* problem rather than the first.

    A host fixing one error per upload round trip would rather be told all of them at
    once, so nothing here returns early on a bad row.
    """
    already = {name.strip().casefold() for name in (existing_names or set())}
    rows: list[ImportRow] = []
    problems: list[ImportProblem] = []
    seen: dict[str, int] = {}

    # `splitlines` first so a lone \r from an old Mac export is still a line break.
    reader = csv.reader(io.StringIO("\n".join(text.splitlines())))
    columns = {"name": 0, "email": 1, "party_size": 2}
    header_seen = False

    for line, cells in enumerate(reader, start=1):
        if not any(cell.strip() for cell in cells):
            continue

        if line == 1 and _looks_like_a_header(cells):
            mapped = _column_map(cells)
            if "name" in mapped:
                columns = mapped
            header_seen = True
            continue

        if len(rows) >= MAX_ROWS:
            problems.append(
                ImportProblem(line, f"more than {MAX_ROWS} rows; split the file and try again")
            )
            break

        name = _cell(cells, columns, "name")
        if not name:
            problems.append(ImportProblem(line, "no name in the first column"))
            continue

        key = name.casefold()
        if key in already:
            problems.append(
                ImportProblem(line, f"{name!r} is already on the guest list for this event")
            )
        elif key in seen:
            problems.append(
                ImportProblem(line, f"{name!r} is also on line {seen[key]} of this file")
            )
        else:
            seen[key] = line

        email = _clean_email(_cell(cells, columns, "email"), line, problems)
        seats = _seats(_cell(cells, columns, "party_size"), line, problems)
        rows.append(ImportRow(line=line, name=name, email=email, party_size=seats))

    if not rows and not problems:
        problems.append(
            ImportProblem(
                2 if header_seen else 1, "the file has no rows — nothing would be imported"
            )
        )

    return ImportPlan(rows=rows, problems=problems)
