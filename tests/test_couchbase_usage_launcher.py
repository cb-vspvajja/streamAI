"""Exercise the real launcher with a Docker double; no live services are stopped."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("fail_transfer",[False,True])
def test_launcher_prepares_and_verifies_couchbase_before_replacing_meter(tmp_path,fail_transfer):
    root=tmp_path/"app with spaces"
    root.mkdir()
    for relative in ("START-CAPELLA.sh","scripts/capella-common.sh","config/capella-runtime.sh"):
        target=root/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/relative,target)
    (root/"certs").mkdir()
    (root/"certs/capella-ca.pem").write_text("fixture certificate")
    (root/"ui/.agent-catalog").mkdir(parents=True)
    for name in ("tools.json","prompts.json","streamai-publish.json"):
        (root/"ui/.agent-catalog"/name).write_text("{}")
    (root/"capella.env").write_text("""CB_CONN_STRING=couchbases://cluster.example
CB_USERNAME=demo
CB_PASSWORD=fixture
MCP_CB_USERNAME=reader
MCP_CB_PASSWORD=fixture
CHAT_BASE_URL=https://chat.example
EMBEDDING_BASE_URL=https://embedding.example
CAPELLA_MODEL_API_KEY=fixture
""")
    binaries=tmp_path/"bin"
    binaries.mkdir()
    for name,content in {
        "uname":"#!/usr/bin/env bash\necho arm64\n",
        "docker":'''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DOCKER_TEST_LOG"
if [[ "$*" == *bootstrap_usage.py* && "$FAIL_TRANSFER" == 1 ]]; then exit 7; fi
exit 0
''',
    }.items():
        path=binaries/name
        path.write_text(content)
        path.chmod(0o755)
    log=tmp_path/"docker.log"
    env={**os.environ,"PATH":str(binaries)+os.pathsep+os.environ["PATH"],
         "DOCKER_TEST_LOG":str(log),"FAIL_TRANSFER":"1" if fail_transfer else "0",
         "CAPELLA_CONFIG":str(root/"capella.env")}
    result=subprocess.run(["bash",str(root/"START-CAPELLA.sh")],cwd=root,env=env,capture_output=True,text=True)
    calls=log.read_text()
    assert calls.index("stop ui memory mcp") < calls.index("tools/bootstrap_usage.py")
    if fail_transfer:
        assert result.returncode != 0
        assert "up -d --no-deps usage" not in calls
        assert "check_capella_models.py" not in calls
        assert "stop usage" not in calls
        assert "Existing data is retained" in result.stderr
    else:
        assert result.returncode == 0,result.stderr
        assert calls.index("tools/bootstrap_usage.py") < calls.index("up -d --no-deps usage")
        assert calls.index("tools/wait_usage_gateway.py") < calls.index("tools/check_capella_models.py")
        assert "StreamAI is ready" in result.stdout
