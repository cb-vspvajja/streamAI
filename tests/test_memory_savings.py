import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from ui.app.memory_policy import needs_summary, conversation_pairs
from ui.app.memory_savings import fingerprint, verify, workload

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"usage-gateway"))
sys.path.insert(0,str(ROOT/"tests"))
from memory_trial import report, start, update
from ledger import Ledger
from comparison import DEFAULTS
from usage_store_fake import FakeStore

def trial():
    return {"id":"a"*32,"status":"completed","startedAt":1,"finishedAt":50,
            "turns":2,"facts":0,"fingerprint":"b"*64,"prices":DEFAULTS,
            "phases":{n:{"startedAt":a,"finishedAt":b,"proof":{"readyBlocks":2,"contentMatches":True,"fingerprint":"b"*64}}
                      for n,a,b in (("previous",10,20),("optimized",30,40))}}
def event(name,i,operation="embeddings",status="succeeded",usage=True):
    return {"id":name+str(i)+operation,"kind":"model_request","timestamp":12+i if name=="previous" else 32+i,
            "trialId":"a"*32,"trialPhase":name,"source":"agent_memory","operation":operation,"status":status,
            "usage":{"inputTokens":100,"outputTokens":20 if operation=="chat/completions" else 0} if usage else {}}
def records():
    return [event(n,i) for n in ("previous","optimized") for i in range(2)]+[event("previous",1,"chat/completions"),event("previous",2,"chat/completions")]

def test_selective_policy_retains_long_turn_processing():
    assert not needs_summary("I like comedy","Preference saved")
    assert needs_summary("x"*4001,"")
    assert needs_summary("short","short","always")
    with pytest.raises(ValueError):needs_summary("a","b","typo")

def test_only_complete_short_turns_replayed_without_truncation():
    chat=[{"role":"user","content":"x"*4001},{"role":"assistant","content":"long"},
          {"role":"user","content":"hello"},{"role":"assistant","content":"world"},
          {"role":"user","content":"pending"}]
    assert conversation_pairs(chat)==[{"user_content":"hello","assistant_content":"world"}]
    with pytest.raises(ValueError):workload({"chat_log":chat})

def test_readback_checks_content_duplicates_status_and_flat_sdk_adapter():
    messages=[{"user_content":"hello","assistant_content":"world"}]*2
    blocks=[dict(messages[0],status="ready"),dict(messages[0],status="MemoryBlockStatus.READY"),{"fact":"likes comedy","status":"ready"}]
    p=verify(blocks,messages,["likes comedy"])
    assert p["readyBlocks"]==3 and p["contentMatches"]
    assert not verify(blocks[:1],messages,[])["contentMatches"]
    blocks[0]["assistant_content"]="changed"
    assert not verify(blocks,messages,["likes comedy"])["contentMatches"]
    assert fingerprint(messages,["b","a"])==fingerprint(messages,["a","b"])

def test_observed_trial_counts_embeddings_and_real_savings():
    r=report(trial(),records())
    assert r["validComparison"] and r["difference"]["llmRequests"]==2
    assert r["difference"]["llmTokens"]==240
    assert r["difference"]["referenceCostUsd"]==pytest.approx(.0009)
    assert r["phases"]["optimized"]["referenceCostUsd"]==pytest.approx(.00002)
    assert r["totalTrialUsage"]["referenceCostUsd"]==pytest.approx(.00094)

@pytest.mark.parametrize("failure",["missing","failed","late","content","not_ready","untagged","no_llm","external","app","unfinished"])
def test_incomplete_or_unfair_trial_never_claims_savings(failure):
    t=trial();rows=records()
    if failure=="missing":rows[0]["usage"]={}
    if failure=="failed":rows[0]["status"]="http_error"
    if failure=="late":rows[0]["timestamp"]=22
    if failure=="content":t["phases"]["optimized"]["proof"]["contentMatches"]=False
    if failure=="not_ready":t["phases"]["optimized"]["proof"]["readyBlocks"]=1
    if failure=="untagged":
        for r in rows:r.pop("trialId",None)
    if failure=="no_llm":rows=[r for r in rows if r["operation"]=="embeddings"]
    if failure=="external":rows.append({"id":"external","kind":"activity","timestamp":15,"category":"ai_functions","action":"generate"})
    if failure=="app":rows.append({**event("previous",4),"source":"app","trialId":None})
    if failure=="unfinished":t["status"]="running"
    r=report(t,rows)
    assert not r["validComparison"] and r["difference"]=={}

def test_background_is_included_and_negative_results_are_not_clamped():
    rows=records()+[{**event("optimized",i,"chat/completions"),"trialId":None} for i in range(3)]
    r=report(trial(),rows)
    assert r["validComparison"]
    assert r["difference"]["llmRequests"]==-1 and r["difference"]["llmTokens"]==-120
    assert r["phases"]["optimized"]["backgroundRequests"]==3

def test_other_overhead_and_experiment_total_are_visible():
    rows=records()+[{**event("previous",0,"chat/completions"),"id":"setup","timestamp":3,"trialId":None}]
    r=report(trial(),rows)
    assert r["otherTrialOverhead"]["llmRequests"]==1
    assert r["totalTrialUsage"]["llmRequests"]==3

def test_trial_state_is_durable_and_window_reset_does_not_erase_it():
    ledger=Ledger(FakeStore())
    t=start(ledger,{"turns":2,"facts":0,"fingerprint":"b"*64})
    with pytest.raises(ValueError):start(ledger,{"turns":2,"facts":0,"fingerprint":"b"*64})
    update(ledger,t["id"],{"action":"start_phase","phase":"previous"})
    with pytest.raises(ValueError):update(ledger,t["id"],{"action":"start_phase","phase":"optimized"})
    with pytest.raises(ValueError):update(ledger,t["id"],{"action":"finish","success":True})
    update(ledger,t["id"],{"action":"finish","success":False,"error":"failure"})
    ledger.new_window()
    assert ledger.summary()["memorySavings"]["id"]==t["id"]
    assert not ledger.summary()["memorySavings"]["validComparison"]
    assert t["prices"]["llmInputUsdPerMillion"]==2.5

def test_native_request_context_is_isolated_and_restored_on_error():
    spec=importlib.util.spec_from_file_location("trial_context",ROOT/"agent-memory-capella/trial_context.py")
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    block=SimpleNamespace(annotations={"streamai_trial":"a"*32,"streamai_phase":"optimized"})
    @mod.metered_memory
    def process(memory_blocks, fail=False):
        assert mod.trial_headers()["X-StreamAI-Phase"]=="optimized"
        assert mod.trial_headers()["X-cb-cache"]=="none"
        if fail:raise RuntimeError("test")
    process([block])
    assert mod.trial_headers()=={}
    with pytest.raises(RuntimeError):process([block],True)
    assert mod.trial_headers()=={}

def test_gateway_keeps_tags_off_upstream_and_records_all_requests():
    # Existing gateway integration test fixture uses actual ASGI routing + mock upstream.
    from test_measured_usage_gateway import UsageGatewayTests
    case=UsageGatewayTests()
    case.setUp()
    try:
        r=case.client.post("/agent_memory/v1/chat/completions",json={"model":"mistral","messages":[]},
             headers={"X-StreamAI-Trial":"a"*32,"X-StreamAI-Phase":"previous"})
        assert r.status_code==200
        row=case.summary()["recentRequests"][0]
        assert row["trialId"]=="a"*32 and row["trialPhase"]=="previous"
        assert "x-streamai-trial" not in case.requests[0].headers
        assert case.requests[0].headers["x-cb-cache"]=="none"
        assert case.client.post("/app/v1/chat/completions",json={},headers={"X-StreamAI-Trial":"bad"}).status_code==422
    finally:case.tearDown()

def test_gateway_restart_marks_incomplete_trial_failed():
    store=FakeStore()
    ledger=Ledger(store)
    t=start(ledger,{"turns":2,"facts":0,"fingerprint":"b"*64})
    update(ledger,t["id"],{"action":"start_phase","phase":"previous"})
    restarted=Ledger(store)
    result=restarted.summary()["memorySavings"]
    assert result["status"]=="failed" and not result["validComparison"]
    assert "restarted" in result["error"]

@pytest.mark.parametrize("failure",["none","rejected","cleanup"])
def test_runner_replays_identical_originals_and_cleans_only_owned_user(monkeypatch,failure):
    from ui.app import memory_savings as runner
    messages=[{"user_content":"hello","assistant_content":"world"},{"user_content":"preference","assistant_content":"saved"}]
    facts=["likes comedy"]
    ledger=Ledger(FakeStore())
    writes=[];deletions=[];clients=[]
    class Session:
        def __init__(self,phase):self.phase=phase;self.blocks=[]
        def add_memory(self,**kwargs):
            writes.append((self.phase,kwargs))
            assert kwargs["async_processing"] is False
            assert kwargs["context_required"]==(self.phase=="previous")
            assert kwargs["annotations"]["streamai_phase"]==self.phase
            blocks=[{"message":m,"status":"ready"} for m in kwargs.get("messages",[])]
            blocks += [{"fact":f,"status":"ready"} for f in kwargs.get("facts",[])]
            self.blocks.extend(blocks)
            return SimpleNamespace(accepted_count=len(blocks) if failure!="rejected" else 0,rejected_count=1 if failure=="rejected" else 0)
        def list_memories(self,**kwargs):return self.blocks
    class User:
        def create_session(self,**kwargs):return Session(kwargs["session_id"])
    class Native:
        def __init__(self,**kwargs):
            assert kwargs["max_retries"]==0
            self.closed=False;clients.append(self)
        def create_user(self,**kwargs):
            self.user_id=kwargs["user_id"]
            assert self.user_id.startswith("streamai-cost-trial-")
            return User()
        def delete_user(self,**kwargs):
            assert kwargs["user_id"]==self.user_id
            deletions.append(kwargs["user_id"])
            if failure=="cleanup":raise RuntimeError("cleanup failed")
        def close(self):self.closed=True
    class HTTP:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,json):
            data=start(ledger,json) if url.endswith("/memory-trial") else update(ledger,url.rsplit("/",1)[-1],json)
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:data)
    monkeypatch.setattr(runner,"AgentMemoryClient",Native)
    monkeypatch.setattr(runner.httpx,"Client",HTTP)
    monkeypatch.setattr(runner.usage_reporter,"url",lambda:"http://meter")
    result=runner.run(SimpleNamespace(agent_memory_url="http://memory"),messages,facts,lambda result:result)
    assert clients[0].closed and len(deletions)==1
    assert result["success"]==(failure!="rejected")
    assert result["cleanupSucceeded"]==(failure!="cleanup")
    if failure=="cleanup":assert deletions[0] in result["error"]
    if failure!="rejected":
        state=ledger._state()["memoryTrial"]
        assert all(p["proof"]["contentMatches"] and p["proof"]["readyBlocks"]==3 for p in state["phases"].values())
        for phase in ("previous","optimized"):
            phase_writes=[w for n,w in writes if n==phase]
            assert [m for w in phase_writes for m in w.get("messages",[])]==messages
            assert [f for w in phase_writes for f in w.get("facts",[])]==facts
