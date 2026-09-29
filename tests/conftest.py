"""Configuration tests inspect the effective environment, including shared defaults."""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def effective_profile():
    def read(relative):
        if relative != ".env.capella-full-aidp.example":
            # Historical profiles retain their original text-based contract.
            # Only the current full Capella profile delegates to runtime defaults.
            return (ROOT / relative).read_text()
        command = 'set -a; source "$1"; if [[ "$2" == full ]]; then source "$3"; fi; env -0'
        result = subprocess.run(
            ["bash", "-ec", command, "profile", str(ROOT / relative),
             "full" if relative == ".env.capella-full-aidp.example" else "legacy",
             str(ROOT / "config/capella-runtime.sh")],
            env={"PATH": os.defpath}, text=True, capture_output=True, check=True,
        )
        return "\n".join(result.stdout.split("\0"))
    return read
