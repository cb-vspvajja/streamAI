"""Avoid LLM extraction when the application already has usable original content."""
SHORT_CONVERSATION_LIMIT = 4000


def needs_summary(user_message, assistant_message, policy="selective"):
    if policy not in {"selective", "always"}:
        raise ValueError("MEMORY_SUMMARY_POLICY must be selective or always")
    return policy == "always" or len(user_message) + len(assistant_message) > SHORT_CONVERSATION_LIMIT


def conversation_pairs(chat_log, limit=4):
    """Only complete, short turns are eligible; never silently truncate a turn."""
    pairs, pending = [], None
    for entry in chat_log:
        if entry.get("role") == "user":
            pending = str(entry.get("content", ""))
        elif entry.get("role") == "assistant" and pending is not None:
            answer = str(entry.get("content", ""))
            if pending.strip() and answer.strip() and not needs_summary(pending, answer):
                pairs.append({"user_content": pending, "assistant_content": answer})
            pending = None
    return pairs[-limit:]
