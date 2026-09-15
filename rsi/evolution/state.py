import json
import os
from pathlib import Path


def redact(value):
    text = str(value)
    for name in ("LLM_API_KEY", "OPENAI_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def save_json(path: Path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(redact(json.dumps(value, ensure_ascii=False, indent=2)), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
