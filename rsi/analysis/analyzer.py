import json

from rsi.evolution.state import save_json


def analyze(llm, prompt, context, branch_factor, log_path):
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(
        {"branch_factor": branch_factor, **context}, ensure_ascii=False)}]
    try:
        for attempt in range(2):
            message = llm.complete(messages)
            messages.append(message)
            try:
                data = json.loads(message.get("content") or "")
                if not isinstance(data, dict) or not isinstance(data.get("problem"), str) or not data["problem"].strip():
                    raise ValueError("problem must be nonempty text")
                candidates = data.get("candidates")
                if (not isinstance(candidates, list) or len(candidates) != branch_factor
                        or not all(isinstance(item, str) and item.strip() for item in candidates)
                        or len({item.strip().casefold() for item in candidates}) != branch_factor):
                    raise ValueError(f"Expected {branch_factor} distinct nonempty candidate strings")
                return data
            except (ValueError, TypeError) as exc:
                if attempt:
                    raise ValueError("Analyzer returned invalid JSON twice") from exc
                messages.append({"role": "user", "content": f"Invalid result: {exc}. Return only the required JSON object."})
    finally:
        save_json(log_path, messages)
