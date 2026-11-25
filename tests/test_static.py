"""The front end has no build step, so a syntax slip would only show in a browser. Catch it here."""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "rackmap" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("name", ["app.js", "faceplate.js"])
def test_script_parses(name):
    check = "new (require('vm').Script)(require('fs').readFileSync(process.argv[1], 'utf8'))"
    result = subprocess.run(["node", "-e", check, str(STATIC / name)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-400:]
