"""OpenAI-compatible adapter: one class for every provider speaking the chat/completions format.

Groq today; OpenRouter or others later are just a different base_url + key in config.
"""
import json
import re
import time
import uuid

import httpx

from app.llm.types import LLMResponse, Message, ProviderError, ToolCall, ToolSpec, classify_status

THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class OpenAICompatAdapter:
    def __init__(self, name: str, base_url: str, api_key: str, client: httpx.Client | None = None,
                 extra_body=None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = client or httpx.Client()
        # Per-model request extras, e.g. Groq's Qwen needs reasoning_format with tools; GPT-OSS rejects it.
        self.extra_body = extra_body or (lambda model: {})

    # --- neutral -> OpenAI ---------------------------------------------------------

    @staticmethod
    def to_messages(messages: list[Message], system: str) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "user":
                out.append({"role": "user", "content": m.text})
            elif m.role == "assistant":
                msg = {"role": "assistant", "content": m.text or None}
                if m.tool_calls:
                    msg["tool_calls"] = [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                        for c in m.tool_calls
                    ]
                out.append(msg)
            elif m.role == "tool":
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": json.dumps(m.result)})
        return out

    # --- call ----------------------------------------------------------------------

    def complete(self, model: str, messages: list[Message], tools: list[ToolSpec], system: str,
                 timeout: float) -> LLMResponse:
        body = {"model": model, "messages": self.to_messages(messages, system), **self.extra_body(model)}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.schema}} for t in tools]
        started = time.time()
        try:
            r = self.client.post(f"{self.base_url}/chat/completions", json=body, timeout=timeout,
                                 headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("network", type(exc).__name__) from exc
        if r.status_code != 200:
            raise ProviderError(classify_status(r.status_code), r.text[:200])
        return self.parse(r.json(), model, int((time.time() - started) * 1000))

    # --- OpenAI -> neutral ---------------------------------------------------------

    def parse(self, data: dict, model: str, ms: int) -> LLMResponse:
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise ProviderError("bad_response", json.dumps(data)[:200]) from exc
        calls = []
        for c in msg.get("tool_calls") or []:
            raw_args = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                # Malformed arguments from the model: keep them visible so validation rejects the call
                # and the model gets an error back, instead of the agent crashing.
                args = {"_invalid_json_arguments": raw_args[:200]}
            calls.append(ToolCall(id=c.get("id") or f"gen-{uuid.uuid4().hex[:8]}",
                                  name=c["function"]["name"], args=args))
        text = THINK.sub("", msg.get("content") or "").strip()  # safety net if reasoning leaks into content
        usage = data.get("usage", {})
        return LLMResponse(
            message=Message(role="assistant", text=text, tool_calls=calls, provider=self.name),
            provider=self.name, model=model, ms=ms,
            tokens_in=usage.get("prompt_tokens", 0), tokens_out=usage.get("completion_tokens", 0),
        )
