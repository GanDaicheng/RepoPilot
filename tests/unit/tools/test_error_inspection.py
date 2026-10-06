from __future__ import annotations

from repopilot.tools.tests import inspect_error


def test_extracts_pytest_failed_node_ids() -> None:
    output = """================ failures ================
FAILED tests/test_calc.py::test_add - AssertionError: mismatch
FAILED tests/test_calc.py::test_subtract - ValueError: bad input
FAILED tests/test_calc.py::test_add - repeated summary
"""

    result = inspect_error(output)

    assert result.ok is True
    assert result.data is not None
    assert result.data.failed_tests == (
        "tests/test_calc.py::test_add",
        "tests/test_calc.py::test_subtract",
    )


def test_extracts_exception_types_and_locations() -> None:
    output = """src/calculator.py:18: in divide
    raise DomainError("bad")
E   DomainError: bad
tests/test_calc.py:9: in test_divide
E   AssertionError: expected value
"""

    result = inspect_error(output)

    assert result.ok is True
    assert result.data is not None
    assert result.data.exception_types == ("DomainError", "AssertionError")
    assert result.data.locations == ("src/calculator.py:18", "tests/test_calc.py:9")


def test_limits_items_and_tail_size() -> None:
    output = """FAILED tests/test_a.py::test_a - failed
FAILED tests/test_b.py::test_b - failed
FAILED tests/test_c.py::test_c - failed
E   FirstError: a
E   SecondError: b
E   ThirdError: c
src/a.py:1: in a
src/b.py:2: in b
src/c.py:3: in c
abcdefghijklmnopqrstuvwxyz
"""

    result = inspect_error(output, max_chars=12, max_items=2)

    assert result.ok is True
    assert result.data is not None
    assert len(result.data.failed_tests) == 2
    assert len(result.data.exception_types) == 2
    assert len(result.data.locations) == 2
    assert result.data.tail == output[-12:]
    assert result.data.truncated is True


def test_empty_output_returns_empty_summary() -> None:
    result = inspect_error("")

    assert result.ok is True
    assert result.data is not None
    assert result.data.failed_tests == ()
    assert result.data.exception_types == ()
    assert result.data.locations == ()
    assert result.data.tail == ""
    assert result.data.truncated is False

