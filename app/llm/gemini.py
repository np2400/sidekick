"""Gemini adapter: neutral Messages <-> Gemini generateContent."""
import json
import time
import uuid

import httpx

from app.llm.types import LLMResponse, Message, ProviderError, ToolCall, ToolSpec, classify_status

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiAdapter:
    name = "gemini"

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self.api_key = api_key
        self.client = client or httpx.Client()

    # --- neutral -> Gemini ---------------------------------------------------------

    def to_contents(self, messages: list[Message]) -> list[dict]:
        contents: list[dict] = []
        foreign_calls: set[str] = set()  # tool calls written by another provider
        for m in messages:
            if m.role == "user":
                contents.append({"role": "user", "parts": [{"text": m.text}]})
            elif m.role == "assistant" and m.provider == self.name and m.raw:
                contents.append(m.raw)  # our own turn: send back verbatim (keeps thought signatures intact)
            elif m.role == "assistant":
                # Another provider's turn (mid-conversation fallback). Gemini needs its own signatures on
                # function calls, which we can't fake, so earlier steps are passed along as plain text.
                notes = [m.text] if m.text else []
                for c in m.tool_calls:
                    foreign_calls.add(c.id)
                    notes.append(f"[Earlier step] I called {c.name} with {json.dumps(c.args)}.")
                contents.append({"role": "model", "parts": [{"text": "\n".join(notes) or "(no text)"}]})
            elif m.role == "tool" and m.tool_call_id in foreign_calls:
                contents.append({"role": "user", "parts": [
                    {"text": f"[Result of {m.tool_name}] {json.dumps(m.result)}"}]})
            elif m.role == "tool":
                part = {"functionResponse": {"name": m.tool_name, "response": m.result}}
                if m.tool_call_id and not m.tool_call_id.startswith("gen-"):
                    part["functionResponse"]["id"] = m.tool_call_id
                # Gemini wants all results for one model turn together in a single user turn.
                last = contents[-1] if contents else None
                if last and last["role"] == "user" and any("functionResponse" in p for p in last["parts"]):
                    last["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
        return contents

    # --- call ----------------------------------------------------------------------

    def complete(self, model: str, messages: list[Message], tools: list[ToolSpec], system: str,
                 timeout: float) -> LLMResponse:
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": self.to_contents(messages)}
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t.name, "description": t.description, "parametersJsonSchema": t.schema} for t in tools]}]
        started = time.time()
        try:
            r = self.client.post(URL.format(model=model), json=body, timeout=timeout,
                                 headers={"x-goog-api-key": self.api_key})  # header, not URL: never logged
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("network", type(exc).__name__) from exc
        if r.status_code != 200:
            raise ProviderError(classify_status(r.status_code), r.text[:200])
        return self.parse(r.json(), model, int((time.time() - started) * 1000))

    # --- Gemini -> neutral ---------------------------------------------------------

    def parse(self, data: dict, model: str, ms: int) -> LLMResponse:
        try:
            turn = data["candidates"][0]["content"]
        except (KeyError, IndexError) as exc:  # e.g. blocked by safety filters: no content
            raise ProviderError("bad_response", json.dumps(data)[:200]) from exc
        parts = turn.get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        calls = [
            ToolCall(id=p["functionCall"].get("id") or f"gen-{uuid.uuid4().hex[:8]}",
                     name=p["functionCall"]["name"], args=p["functionCall"].get("args", {}))
            for p in parts if "functionCall" in p
        ]
        usage = data.get("usageMetadata", {})
        return LLMResponse(
            message=Message(role="assistant", text=text, tool_calls=calls, provider=self.name, raw=turn),
            provider=self.name, model=model, ms=ms,
            tokens_in=usage.get("promptTokenCount", 0), tokens_out=usage.get("candidatesTokenCount", 0),
        )
