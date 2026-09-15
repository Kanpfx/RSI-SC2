import json


STRING = {"type": "string"}
INTEGER = {"type": "integer"}


def schema(name, description, properties, required, examples):
    description += "\nExample arguments: " + "; ".join(json.dumps(item, ensure_ascii=False) for item in examples)
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False}}}
