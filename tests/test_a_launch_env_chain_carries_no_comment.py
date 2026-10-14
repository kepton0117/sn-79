"""No comment line sits inside a backslash-continued command in the launchers.

A `#` line ends the continuation: every `VAR=value \\` above it becomes a plain shell assignment, and
the command at the end of the chain starts without them. `bash -n` accepts it, so only a scan finds it.
"""

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHERS = sorted(p for p in _ROOT.glob("run_*.sh") if p.is_file())


def _comments_inside_continuations(text):
    out = []
    prev = ""
    for n, line in enumerate(text.split("\n"), 1):
        if prev.rstrip().endswith("\\") and not prev.lstrip().startswith("#") and line.lstrip().startswith("#"):
            out.append(f"{n}: {line.strip()[:80]}")
        prev = line
    return out


@pytest.mark.parametrize("launcher", _LAUNCHERS, ids=lambda p: p.name)
def test_no_comment_inside_a_continued_command(launcher):
    found = _comments_inside_continuations(launcher.read_text())
    assert not found, f"{launcher.name}: a comment ends the command it sits in: {found}"


def test_the_check_finds_the_defect_it_names():
    broken = 'A=1 \\\n    LOG_LEVEL="$LOG_LEVEL" \\\n    # a note\n    pm2 start x\n'
    assert _comments_inside_continuations(broken) == ["3: # a note"]
    assert _comments_inside_continuations('A=1 \\\n    pm2 start x\n# after\n') == []
    assert _comments_inside_continuations("# usage: run.sh -E url \\\n#        -w key\n") == []
