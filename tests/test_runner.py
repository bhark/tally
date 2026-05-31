from __future__ import annotations

import pytest

from tally.runner import CommandError, run


def test_stderr_tail_on_failure():
    with pytest.raises(CommandError) as exc:
        run(["bash", "-c", "echo boom >&2; exit 3"], label="boom")
    err = exc.value
    assert err.returncode == 3
    assert any("boom" in line for line in err.stderr_tail)


def test_success_captures_stdout():
    res = run(["bash", "-c", "echo hello"], label="hello")
    assert res.returncode == 0
    assert res.out == "hello"
