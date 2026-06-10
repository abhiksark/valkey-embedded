# tests/test_examples.py
"""Every example script must run end-to-end and exit cleanly.

The examples in examples/ double as living documentation; executing them here
guarantees the public API they demonstrate keeps working.
"""

import glob
import os
import subprocess
import sys

import pytest

_EXAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples"
)
_EXAMPLES = sorted(glob.glob(os.path.join(_EXAMPLES_DIR, "*.py")))


def test_examples_directory_is_not_empty():
    assert _EXAMPLES, "no example scripts found in examples/"


@pytest.mark.examples
@pytest.mark.parametrize(
    "script", _EXAMPLES, ids=[os.path.basename(p) for p in _EXAMPLES]
)
def test_example_runs_cleanly(script):
    result = subprocess.run(
        [sys.executable, script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "example {0} exited {1}\n--- stdout ---\n{2}\n--- stderr ---\n{3}".format(
            os.path.basename(script), result.returncode, result.stdout, result.stderr
        )
    )
    assert result.stdout.strip(), "example produced no output"
