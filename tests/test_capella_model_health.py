"""Keep paid model health probes bounded without changing non-Mistral models."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "memory_compat", ROOT / "agent-memory-capella/apply-nvidia-compat.py")
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


def test_mistral_health_uses_supported_one_token_limit():
    scope = {}
    exec(compat.HELPER, scope)
    opts = scope["llm_health_request_options"]("mistralai/mistral-7b-instruct-v0.3")
    # Verified against the deployed endpoint: max_completion_tokens was ignored.
    assert opts == {"max_tokens": 1, "temperature": 0}
    assert scope["llm_health_request_options"]("a-reasoning-model") == {
        "max_completion_tokens": 16}


def test_probe_defaults_and_operator_override(effective_profile):
    values = dict(line.split("=", 1) for line in
                  effective_profile(".env.capella-full-aidp.example").splitlines()
                  if "=" in line)
    assert int(values["AGENTMEMORY_HEALTH_REFRESH_INTERVAL_SECONDS"]) == 300
    assert int(values["AGENTMEMORY_HEALTH_CACHE_TTL_SECONDS"]) == 600
    # Configuration must still allow operators to choose their outage-detection budget.
    import os
    import subprocess
    result = subprocess.run(
        ["bash", "-ec",
         'source "$1"; source "$2"; printf "%s %s" "$AGENTMEMORY_HEALTH_REFRESH_INTERVAL_SECONDS" "$AGENTMEMORY_HEALTH_CACHE_TTL_SECONDS"',
         "config", str(ROOT / "capella.env"), str(ROOT / "config/capella-runtime.sh")],
        env={"PATH": os.defpath, "AGENTMEMORY_HEALTH_REFRESH_INTERVAL_SECONDS": "60",
             "AGENTMEMORY_HEALTH_CACHE_TTL_SECONDS": "120"},
        capture_output=True, text=True, check=True)
    assert result.stdout == "60 120"
