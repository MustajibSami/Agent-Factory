"""One door to every model. Rooms call ask_json(task, ...) and never know
whether the answer came from Claude, a local model on your GPU, or the mock.

Which model serves which task is set in config.toml under [llm.tasks].
"""
import json
import urllib.request
from dataclasses import dataclass

from . import mock_llm

# Models that support the server-side refusal fallback.
FALLBACK_MODELS = {"claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5"}
JSON_RULE = "\n\nReply with one JSON object only. No text before or after it."


class LLMError(Exception):
    pass


@dataclass
class Answer:
    data: dict
    cost: float
    provider: str
    model: str


def parse_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("model did not return JSON")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"model returned invalid JSON: {e}") from e


class LLM:
    def __init__(self, cfg):
        self.cfg = cfg

    def route(self, task: str) -> tuple[str, str]:
        """(provider, model) for a task."""
        if self.cfg.get("llm.mode", "mock") != "live":
            return "mock", "mock"
        spec = self.cfg.get(f"llm.tasks.{task}", {})
        return spec.get("provider", "mock"), spec.get("model", "mock")

    def ask_json(self, task: str, system: str, prompt: str, context: dict) -> Answer:
        """context holds the structured inputs; only the mock reads it."""
        provider, model = self.route(task)
        if provider == "mock":
            return Answer(mock_llm.answer(task, context), 0.0, "mock", "mock")
        if provider == "anthropic":
            text, cost = self._anthropic(model, system + JSON_RULE, prompt)
        elif provider == "ollama":
            text, cost = self._ollama(model, system + JSON_RULE, prompt), 0.0
        else:
            raise LLMError(f"unknown provider '{provider}' for task '{task}'")
        return Answer(parse_json(text), cost, provider, model)

    def _anthropic(self, model, system, prompt):
        import anthropic
        client = anthropic.Anthropic()
        kwargs = dict(model=model, max_tokens=16000, system=system,
                      messages=[{"role": "user", "content": prompt}])
        try:
            if model in FALLBACK_MODELS:
                # If the model declines, the API re-runs the request on a fallback model.
                resp = client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise LLMError("rate limited by the Claude API, try again later") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("could not reach the Claude API") from e
        if resp.stop_reason == "refusal":
            raise LLMError("the model declined this request")
        if resp.stop_reason == "max_tokens":
            raise LLMError("the answer was cut off at the token limit")
        text = "".join(b.text for b in resp.content if b.type == "text")
        prices = self.cfg.get("llm.prices", {})
        p_in, p_out = prices.get(resp.model) or prices.get(model) or (0.0, 0.0)
        cost = (resp.usage.input_tokens * p_in + resp.usage.output_tokens * p_out) / 1_000_000
        return text, cost

    def _ollama(self, model, system, prompt):
        url = self.cfg.get("llm.ollama_url", "http://localhost:11434") + "/api/chat"
        body = json.dumps({
            "model": model, "stream": False, "format": "json",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())["message"]["content"]
        except OSError as e:
            raise LLMError(f"could not reach Ollama at {url}. Is it running? ({e})") from e
