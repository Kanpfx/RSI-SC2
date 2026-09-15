import os


def price_per_million(name):
    value = os.environ.get(name)
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, the price per million tokens") from exc


class Client:
    def __init__(self):
        from openai import OpenAI

        key, self.model = os.environ.get("LLM_API_KEY"), os.environ.get("LLM_MODEL")
        if not key or not self.model:
            raise ValueError("Set LLM_API_KEY and LLM_MODEL in the environment")
        self.input_price = price_per_million("LLM_PRICE_INPUT_PER_MILLION")
        self.output_price = price_per_million("LLM_PRICE_OUTPUT_PER_MILLION")
        self.client = OpenAI(api_key=key, base_url=os.environ.get("LLM_BASE_URL") or None,
                             timeout=120, max_retries=1)
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def count(self, response):
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.usage["calls"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            self.usage[key] += getattr(usage, key, 0) or 0

    def statistics(self):
        stats = {"model": self.model, **self.usage}
        if self.input_price is None or self.output_price is None:
            stats["cost"] = None
        else:
            stats["cost"] = round(self.usage["prompt_tokens"] * self.input_price / 1e6
                                  + self.usage["completion_tokens"] * self.output_price / 1e6, 6)
        return stats

    def complete(self, messages, tools=None):
        options = {"tools": tools, "tool_choice": "auto"} if tools else {}
        response = self.client.chat.completions.create(model=self.model, messages=messages, **options)
        self.count(response)
        choice = response.choices[0]
        if choice.finish_reason not in ("stop", "tool_calls"):
            raise RuntimeError(f"Incomplete model response: {choice.finish_reason}")
        message = choice.message
        result = {"role": "assistant", "content": message.content}
        if message.tool_calls:
            result["tool_calls"] = [call.model_dump(exclude_none=True) for call in message.tool_calls]
        return result
