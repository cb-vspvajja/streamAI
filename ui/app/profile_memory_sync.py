"""Mirror explicit operational preferences into a dedicated Agent Memory session."""
from __future__ import annotations
import hashlib
from agentmemory import ConflictError, NotFoundError

SOURCE = "streamai_operational_profile_sync"


def reconcile(user, user_id, facts, *, apply, blocks_from_result, is_ready):
    desired = list(dict.fromkeys(str(f).strip() for f in facts if str(f).strip()))
    wanted = set(desired)
    session_id = "streamai-profile-" + hashlib.sha256(str(user_id).encode()).hexdigest()[:24]
    result = {"status": "not_synced", "expectedFacts": len(desired), "storedFacts": 0, "readyFacts": 0,
              "acceptedFacts": 0, "addedFacts": 0, "removedFacts": 0, "retriedFacts": 0,
              "sessionId": session_id, "blockIds": [], "source": SOURCE,
              "note": "Stored or accepted blocks may still be awaiting embedding generation."}
    try:
        session = user.get_session(session_id=session_id)
    except NotFoundError:
        if not apply or not desired:
            result["status"] = "not_synced" if desired else "empty"
            return result, []
        try:
            session = user.create_session(session_id=session_id,
                annotations={"purpose": "operational_profile_sync"}, memory_blocks_ttl=0)
        except ConflictError:
            session = user.get_session(session_id=session_id)
    if getattr(session, "end_time", None):
        raise RuntimeError("The profile-sync session was ended externally; it cannot accept preference updates")
    all_blocks, offset = [], 0
    while True:
        page = session.list_memories(limit=200, offset=offset)
        batch = blocks_from_result(page)
        all_blocks.extend(batch)
        offset += len(batch)
        total = getattr(page, "total", None)
        if not batch or len(batch) < 200 or (total is not None and offset >= total):
            break
    managed = [b for b in all_blocks if (b.get("annotations") or {}).get("source") == SOURCE]
    matching, obsolete = {}, []
    for block in managed:
        fact = block.get("fact")
        if fact in wanted and fact not in matching:
            matching[fact] = block
        elif block.get("block_id"):
            obsolete.append(str(block["block_id"]))
    missing = [fact for fact in desired if fact not in matching]
    failed = [b for b in matching.values() if "fail" in str(b.get("status", "")).lower()]
    annotations = {"memory_scope": "long_term", "memory_type": "viewer_preference", "source": SOURCE}
    # Only our own mirrored preference blocks are reconciled. Conversation,
    # manually entered facts and like/watch-history records are never deleted.
    if apply and missing:
        response = session.add_memory(facts=missing, annotations=annotations,
                                      memory_block_ttl=0, async_processing=True, context_required=False)
        ids = list(getattr(response, "block_ids", []) or [])
        accepted = int(getattr(response, "accepted_count", len(ids)))
        if accepted != len(missing) or len(ids) != accepted or getattr(response, "rejected_count", 0):
            raise RuntimeError("Agent Memory accepted only part of the profile; retry will reconcile the missing facts")
        result["addedFacts"] = accepted
        result["blockIds"].extend(ids)
    if apply:
        for block in failed:
            session.update_memory(block_id=block["block_id"], fact=block["fact"],
                annotations=annotations, memory_block_ttl=0, async_processing=True, context_required=False)
            result["retriedFacts"] += 1
        if obsolete:
            response = session.delete_memory(block_ids=obsolete)
            result["removedFacts"] = int(response.deleted_count)
            if result["removedFacts"] != len(obsolete):
                raise RuntimeError("Profile memory removal was incomplete; retry to verify the mirror")
    result["storedFacts"] = len(matching)
    result["readyFacts"] = sum(is_ready(b) for b in matching.values())
    result["acceptedFacts"] = len(matching) + result["addedFacts"]
    result["blockIds"].extend(str(b["block_id"]) for b in matching.values() if b.get("block_id"))
    if not desired:
        result["status"] = "empty" if apply or not obsolete else "out_of_sync"
    elif (missing and not apply) or (obsolete and not apply):
        result["status"] = "out_of_sync"
    elif failed and not apply:
        result["status"] = "enrichment_failed"
    else:
        result["status"] = "ready" if result["readyFacts"] == len(desired) else "pending"
    return result, list(matching.values())
