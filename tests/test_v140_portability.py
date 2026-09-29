from pathlib import Path

def test_profiles_exist():
    root=Path(__file__).resolve().parents[1]
    for name in ('.env.local.example','.env.server.example','.env.capella.example'):
        assert (root/name).exists()

def test_no_ui_launcher_hardcodes_local_couchbase():
    text=(Path(__file__).resolve().parents[1]/'scripts/05-start-ui.sh').read_text()
    assert 'CB_CONN_STRING=couchbase://host.docker.internal' not in text
    assert '--env-file' in text

def test_provider_and_sdk_adapters_present():
    root=Path(__file__).resolve().parents[1]
    assert 'class ChatProvider' in (root/'ui/app/providers.py').read_text()
    assert 'class SdkSearchClient' in (root/'ui/app/search_adapter.py').read_text()
    assert 'wan_development' in (root/'ui/app/connection.py').read_text()
