from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "02-start-agent-memory.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _base_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "DEPLOYMENT_TARGET=local",
                "AGENTMEMORY_CONN_STRING=couchbase://host.docker.internal",
                "AGENTMEMORY_USERNAME=agentMemory",
                "AGENTMEMORY_PASSWORD=password",
                "AGENTMEMORY_BUCKET=agent_memory",
                "AGENTMEMORY_EMBEDDING_MODEL=nomic-embed-text",
                "AGENTMEMORY_EMBEDDING_URL=http://ollama:11434/v1",
                "AGENTMEMORY_LLM_MODEL=llama3.2:1b",
                "AGENTMEMORY_LLM_URL=http://ollama:11434/v1",
            ]
        )
        + "\n"
    )
    _write_executable(fake_bin / "uname", "#!/usr/bin/env bash\necho arm64\n")
    _write_executable(fake_bin / "curl", "#!/usr/bin/env bash\necho '{\"status\":\"healthy\"}'\n")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "ENV_FILE": str(env_file),
            "FAKE_DOCKER_LOG": str(log),
        }
    )
    return env, fake_bin, log


def test_agent_memory_start_auto_detects_and_retags_loaded_image(tmp_path: Path) -> None:
    env, fake_bin, log = _base_env(tmp_path)
    _write_executable(
        fake_bin / "docker",
        r'''#!/usr/bin/env bash
set -e
echo "$*" >> "$FAKE_DOCKER_LOG"
if [[ "$1 $2" == "image inspect" ]]; then
  if [[ "$3" == "--format" ]]; then
    [[ "${@: -1}" == "vendor/agentmemory-server:1.0.0-rc2" ]] && { echo arm64; exit 0; }
    exit 1
  fi
  [[ "$3" == "vendor/agentmemory-server:1.0.0-rc2" ]] && exit 0
  exit 1
fi
if [[ "$1 $2" == "image ls" ]]; then
  echo "vendor/agentmemory-server:1.0.0-rc2"
  exit 0
fi
if [[ "$1" == "tag" ]]; then exit 0; fi
if [[ "$1 $2" == "network inspect" ]]; then exit 0; fi
if [[ "$1" == "rm" ]]; then exit 0; fi
if [[ "$1" == "run" ]]; then echo fake-container-id; exit 0; fi
if [[ "$1" == "exec" ]]; then echo "  AGENTMEMORY_BUCKET='agent_memory'"; exit 0; fi
if [[ "$1 $2" == "context show" ]]; then echo desktop-linux; exit 0; fi
exit 0
''',
    )

    completed = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    combined = completed.stdout + completed.stderr
    assert "Found compatible Agent Memory image under tag" in combined
    assert "Using Agent Memory image: agentmemory-server:arm64" in combined
    assert "tag vendor/agentmemory-server:1.0.0-rc2 agentmemory-server:arm64" in log.read_text()


def test_agent_memory_start_can_auto_load_nearby_tar(tmp_path: Path) -> None:
    env, fake_bin, log = _base_env(tmp_path)
    image_tar = tmp_path / "agentmemory-server-arm64-1.0.0-rc2.tar"
    image_tar.write_bytes(b"test-placeholder")
    env["AGENT_MEMORY_IMAGE_TAR"] = str(image_tar)
    state = tmp_path / "loaded"
    env["FAKE_DOCKER_STATE"] = str(state)

    _write_executable(
        fake_bin / "docker",
        r'''#!/usr/bin/env bash
set -e
echo "$*" >> "$FAKE_DOCKER_LOG"
if [[ "$1 $2" == "image inspect" ]]; then
  if [[ "$3" == "--format" ]]; then
    [[ -f "$FAKE_DOCKER_STATE" && "${@: -1}" == "loaded/agentmemory-server:rc2" ]] && { echo arm64; exit 0; }
    exit 1
  fi
  exit 1
fi
if [[ "$1 $2" == "image ls" ]]; then
  [[ -f "$FAKE_DOCKER_STATE" ]] && echo "loaded/agentmemory-server:rc2"
  exit 0
fi
if [[ "$1" == "load" ]]; then touch "$FAKE_DOCKER_STATE"; exit 0; fi
if [[ "$1" == "tag" ]]; then exit 0; fi
if [[ "$1 $2" == "network inspect" ]]; then exit 0; fi
if [[ "$1" == "rm" ]]; then exit 0; fi
if [[ "$1" == "run" ]]; then echo fake-container-id; exit 0; fi
if [[ "$1" == "exec" ]]; then exit 0; fi
if [[ "$1 $2" == "context show" ]]; then echo desktop-linux; exit 0; fi
exit 0
''',
    )

    completed = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    combined = completed.stdout + completed.stderr
    assert f"Loading Couchbase Agent Memory image from: {image_tar}" in combined
    assert "Using Agent Memory image: agentmemory-server:arm64" in combined
    assert f"load -i {image_tar}" in log.read_text()


def test_agent_memory_image_configuration_is_documented() -> None:
    env_text = (ROOT / ".env.local.example").read_text()
    readme = (ROOT / "README.md").read_text()
    script = SCRIPT.read_text()

    assert "AGENT_MEMORY_IMAGE=" in env_text
    assert "AGENT_MEMORY_IMAGE_TAR=" in env_text
    assert "AGENT_MEMORY_AUTO_LOAD=true" in env_text
    assert "AGENT_MEMORY_IMAGE_TAR" in readme
    assert "find_compatible_loaded_image" in script
    assert "docker tag" in script
