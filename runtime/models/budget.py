"""Per-request model token budget enforcement."""
import math
from openhands.sdk import LLM

class BudgetedLLM(LLM):
    token_budget: int

    def check_request(self, messages, tools, security=False):
        """Reserve estimated input and maximum output before each SDK request."""
        usage = self.metrics.accumulated_token_usage
        spent = usage.prompt_tokens + usage.completion_tokens if usage else 0
        estimate = self.get_token_count(messages, tools, security)
        required = math.ceil(estimate * 1.15) + 1024 + self.max_output_tokens
        if estimate <= 0:
            raise ValueError("Token estimation failed; check SDK token counter and tool schemas")
        if spent + required > self.token_budget:
            raise ValueError("Next request cannot fit the token budget")

    def completion(self, messages, tools=None, add_security_risk_prediction=False, **kwargs):
        """Apply the session budget to SDK completion transport."""
        self.check_request(messages, tools, add_security_risk_prediction)
        return super().completion(messages, tools, add_security_risk_prediction, **kwargs)

    def responses(self, messages, tools=None, **kwargs):
        """Apply the same budget to SDK Responses transport."""
        self.check_request(messages, tools, kwargs.get("add_security_risk_prediction", False))
        return super().responses(messages, tools, **kwargs)
