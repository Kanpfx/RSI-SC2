import json


def working_messages(messages, threshold=60000, keep_rounds=6):
    """Shorten old retrieval results for inference; leave the saved trace intact."""
    if sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) <= threshold:
        return messages
    turns = [i for i, message in enumerate(messages) if message["role"] == "assistant"]
    if len(turns) <= keep_rounds:
        return messages
    cutoff = turns[-keep_rounds]
    calls = {call["id"]: call["function"] for message in messages
             for call in message.get("tool_calls", []) or []}
    result = []
    for i, message in enumerate(messages):
        call = calls.get(message.get("tool_call_id"), {})
        if (i < cutoff and message["role"] == "tool" and len(message.get("content", "")) > 1500
                and call.get("name") in ("read_file", "search", "api_query", "entity_info", "tech_tree")):
            content = json.loads(message["content"])
            if not isinstance(content, dict) or "error" not in content:
                message = {**message, "content": json.dumps({
                    "omitted": "Older retrieval output omitted from working context; full result remains in agent.json.",
                    "tool": call["name"], "arguments": call["arguments"],
                    "hint": "Repeat the query if needed; files may have changed since the original read."})}
        result.append(message)
    return result
