import os


class Client:
    def __init__(self):
        from openai import OpenAI

        key, self.model = os.environ.get("LLM_API_KEY"), os.environ.get("LLM_MODEL")
        if not key or not self.model:
            raise ValueError("Set LLM_API_KEY and LLM_MODEL in the environment")
        self.client = OpenAI(api_key=key, base_url=os.environ.get("LLM_BASE_URL") or None,
                             timeout=120, max_retries=1)

    def complete(self, messages, tools=None):
        options = {"tools": tools, "tool_choice": "auto"} if tools else {}
        response = self.client.chat.completions.create(model=self.model, messages=messages, **options)
        choice = response.choices[0]
        if choice.finish_reason not in ("stop", "tool_calls"):
            raise RuntimeError(f"Incomplete model response: {choice.finish_reason}")
        message = choice.message
        result = {"role": "assistant", "content": message.content}
        if message.tool_calls:
            result["tool_calls"] = [call.model_dump(exclude_none=True) for call in message.tool_calls]
        return result
