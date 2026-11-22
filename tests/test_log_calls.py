"""Every log call in `app/` passes a fixed message and puts its data in `extra=`. TAP-12484.

The error collector keeps a log record's MESSAGE and drops its arguments and extras
(`app.error_collector`): a name or an email address has no pattern to redact, so the
only safe place for one is a field the collector never sends. That holds only while
nobody formats data into the message itself. This test reads the source rather than
trusting each new call site to remember.

A call is refused if its message is anything but a plain string literal (an f-string,
a `%` expression, a `.format()` call, a variable) or if it passes positional format
arguments after the message, which the SDK renders into the message it sends.
"""

from __future__ import annotations

import ast

import pytest

from tests.conftest import PROJECT_ROOT

LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})
LOGGER_NAMES = frozenset({"logger", "log", "logging"})


def _is_logger(node: ast.expr) -> bool:
    """`logger`, `log`, `logging`, anything named `..._logger`, or `logging.getLogger(...)`."""
    if isinstance(node, ast.Name):
        return node.id in LOGGER_NAMES or node.id.endswith("_logger")
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "getLogger"
    )


def log_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in LOG_METHODS
        and _is_logger(node.func.value)
    ]


def problem(call: ast.Call) -> str | None:
    """Why this call could put data in the message, or None if it cannot."""
    assert isinstance(call.func, ast.Attribute)
    at = 1 if call.func.attr == "log" else 0  # `log(level, msg, ...)`
    if len(call.args) <= at:
        return "no positional message"
    message = call.args[at]
    if not (isinstance(message, ast.Constant) and isinstance(message.value, str)):
        return "message is not a plain string literal"
    if len(call.args) > at + 1:
        return "positional format arguments after the message"
    return None


def _problems_in(source: str) -> list[str | None]:
    return [problem(call) for call in log_calls(ast.parse(source))]


@pytest.mark.parametrize(
    "source",
    [
        'logger.info(f"sent to {guest.email}")',
        'logger.info("sent to %s", guest.email)',
        'logger.warning("sent to %s" % guest.email)',
        'logger.error("sent to {}".format(guest.email))',
        "logger.exception(message)",
        'log.log(logging.INFO, "sent to %s", guest.email)',
        'logging.critical("sent to " + guest.email)',
        'request_logger.debug("for %s", name)',
        'logging.getLogger("app").info("for %s", name)',
    ],
)
def test_the_check_refuses_a_message_that_could_carry_data(source: str) -> None:
    """Positive control: if these passed, a clean result over `app/` would prove nothing."""
    problems = _problems_in(source)
    assert len(problems) == 1, "the check did not recognize this as a log call"
    assert problems[0] is not None


@pytest.mark.parametrize(
    "source",
    [
        'logger.info("email sent", extra={"to": guest.email})',
        'log.log(logging.WARNING, "slow request", extra={"ms": 900})',
        'request_logger.exception("request failed", extra={"path": path})',
    ],
)
def test_the_check_accepts_a_fixed_message_with_extras(source: str) -> None:
    assert _problems_in(source) == [None]


def test_no_log_call_in_the_app_formats_data_into_its_message() -> None:
    found: list[str] = []
    refused: list[str] = []
    for path in sorted((PROJECT_ROOT / "app").rglob("*.py")):
        for call in log_calls(ast.parse(path.read_text(), filename=str(path))):
            where = f"{path.relative_to(PROJECT_ROOT)}:{call.lineno}"
            found.append(where)
            why = problem(call)
            if why is not None:
                refused.append(f"{where}: {why}")

    # Four calls existed when this was written (mail.py, and three in main.py). Fewer
    # found means the walk stopped recognizing them, not that they went away.
    assert len(found) >= 4, found
    assert refused == []
