"""Replay real completed chat turns into isolated native Agent Memory users."""
from __future__ import annotations
import hashlib
import json
import time
import httpx
from agentmemory import AgentMemoryClient
from . import usage_reporter
from .memory_policy import conversation_pairs

def fingerprint(messages, facts):
    return hashlib.sha256(json.dumps({"messages":messages,"facts":sorted(facts)}, sort_keys=True,
                                    ensure_ascii=False,separators=(",",":")).encode()).hexdigest()

def workload(snapshot):
    messages=conversation_pairs(snapshot.get("chat_log", []))
    if len(messages)<2:
        raise ValueError("Complete at least two short chat turns in this session, then run the comparison.")
    # Explicit facts already used in chat context. No inferred facts are created for this trial.
    facts=list(dict.fromkeys(str(b["fact"]) for b in snapshot.get("facts", []) if b.get("fact") and len(str(b["fact"]))<=512))[:6]
    return messages,facts

def verify(blocks, messages, facts):
    actual_messages,actual_facts=[],[]
    for block in blocks:
        message=block.get("message") or (block if block.get("user_content") is not None else None)
        if message:
            actual_messages.append({k:message.get(k,"") for k in ("user_content","assistant_content")})
        elif block.get("fact") is not None:
            actual_facts.append(str(block["fact"]))
    # Ingestion timestamps can tie, so compare complete content as multisets.
    sort=lambda items:sorted(json.dumps(i,sort_keys=True,ensure_ascii=False) for i in items)
    matches=sort(actual_messages)==sort(messages) and sorted(actual_facts)==sorted(facts)
    ready=sum(str(b.get("status","")).lower().split(".")[-1]=="ready" for b in blocks)
    return {"readyBlocks":ready,"contentMatches":matches,"fingerprint":fingerprint(messages,facts)}

def run(settings, messages, facts, blocks_from_result, progress=lambda text:None):
    meter=usage_reporter.url()
    if not meter:
        raise RuntimeError("The usage gateway must be enabled.")
    fp=fingerprint(messages,facts)
    # No automatic HTTP retries of non-idempotent memory writes.
    with httpx.Client(timeout=30) as http:
        def post(path,body):
            response=http.post(meter+path,json=body)
            response.raise_for_status()
            return response.json()
        trial=post("/memory-trial",{"turns":len(messages),"facts":len(facts),"fingerprint":fp})
        root="/memory-trial/"+trial["id"]
        user_id="streamai-cost-trial-"+trial["id"]
        created=False;success=False;cleanup=False;error="";client=None
        try:
            client=AgentMemoryClient(base_url=settings.agent_memory_url, timeout=360, max_retries=0)
            progress("Creating isolated trial sessions")
            user=client.create_user(user_id=user_id,name="StreamAI memory cost comparison",
                                    metadata={"purpose":"isolated_cost_trial","trial_id":trial["id"]})
            created=True
            # Vary phase order across runs; both start with empty sessions.
            order=("previous","optimized") if int(trial["id"][-1],16)%2 else ("optimized","previous")
            for phase in order:
                post(root,{"action":"start_phase","phase":phase})
                progress(("Previous policy: summarize each saved turn" if phase=="previous" else
                          "Selective policy: retain originals without redundant summaries"))
                session=user.create_session(session_id=phase,annotations={"purpose":"cost_comparison"},memory_blocks_ttl=3600)
                annotations={"streamai_trial":trial["id"],"streamai_phase":phase,
                             "source":"streamai_memory_comparison","memory_type":"comparison"}
                # Synchronous processing gives a real completion boundary, not a sleep.
                for message in messages:
                    response=session.add_memory(messages=[message], annotations=annotations, memory_block_ttl=3600,
                        context_required=phase=="previous",async_processing=False)
                    if response.accepted_count!=1 or response.rejected_count:
                        raise RuntimeError("A conversation block was not accepted.")
                if facts:
                    response=session.add_memory(facts=facts,annotations=annotations,memory_block_ttl=3600,
                        context_required=phase=="previous",async_processing=False)
                    if response.accepted_count!=len(facts) or response.rejected_count:
                        raise RuntimeError("A fact block was not accepted.")
                blocks=blocks_from_result(session.list_memories(limit=200))
                proof=verify(blocks,messages,facts)
                post(root,{"action":"finish_phase","phase":phase,"proof":proof})
                if not proof["contentMatches"] or proof["readyBlocks"]!=len(messages)+len(facts):
                    raise RuntimeError("Native memory did not return matching ready originals.")
            success=True
        except Exception as exc:
            # Safe message only. Provider errors can include hosts and private source text.
            error=type(exc).__name__+": comparison did not complete; inspect the recorded requests."
        finally:
            if created:
                try:
                    client.delete_user(user_id=user_id)
                    cleanup=True
                except Exception:
                    error=(error+" " if error else "")+"Trial cleanup failed; remove only user "+user_id
            if client:
                client.close()
            post(root,{"action":"finish","success":success,"error":error,"cleanupSucceeded":cleanup})
        return {"trialId":trial["id"],"success":success,"error":error,"cleanupSucceeded":cleanup}
