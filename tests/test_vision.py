"""Photos: Telegram download, adapters, vision-only routing, webhook path. All offline."""
import httpx
from fastapi.testclient import TestClient

from app import main, telegram
from app.agent import run_agent
from app.llm.gemini import GeminiAdapter
from app.llm.openai_compat import OpenAICompatAdapter
from app.llm.router import Candidate, Router, parse_candidates
from app.llm.types import Image, LLMResponse, Message
from app.trace import AgentResult, ToolContext, Trace
from tests.fakes import ScriptedProvider, text

PHOTO = Image("image/jpeg", b"\xff\xd8fake-jpeg")


def test_openai_and_gemini_send_the_photo_inline_next_to_the_text():
    history = [Message("user", text="What's on this flyer?", images=[PHOTO])]

    content = OpenAICompatAdapter.to_messages(history, "sys")[1]["content"]
    assert content[0] == {"type": "text", "text": "What's on this flyer?"}
    assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,/9hmYWtlLWpwZWc="

    parts = GeminiAdapter("KEY").to_contents(history)[0]["parts"]
    assert parts[1] == {"inlineData": {"mimeType": "image/jpeg", "data": "/9hmYWtlLWpwZWc="}}


class Recorder:
    def __init__(self, name):
        self.name, self.models = name, []

    def complete(self, model, messages, tools, system, timeout):
        self.models.append(model)
        return LLMResponse(Message("assistant", text="a flyer"), self.name, model)


def test_photos_only_go_to_models_that_can_see():
    candidates = parse_candidates("groq:openai/gpt-oss-120b,groq:qwen/qwen3.8-27b,gemini:gemini-3.5-flash", set())
    assert [c.vision for c in candidates] == [False, True, True]
    groq = Recorder("groq")
    router = Router(candidates, {"groq": groq, "gemini": Recorder("gemini")})

    r = router.complete([Message("user", text="what's this?", images=[PHOTO])], [], "sys")
    assert groq.models == ["qwen/qwen3.8-27b"]  # GPT-OSS was never asked
    assert [(a.candidate, a.kind) for a in r.attempts] == [("groq:openai/gpt-oss-120b", "no_vision")]

    router.complete([Message("user", text="hi")], [], "sys")
    assert groq.models[-1] == "openai/gpt-oss-120b"  # text-only messages still start with GPT-OSS


def test_photos_can_have_their_own_model_order():
    text_order = parse_candidates("groq:openai/gpt-oss-120b,groq:qwen/qwen3.8-27b,gemini:gemini-3.5-flash", set())
    photo_order = parse_candidates("gemini:gemini-3.5-flash,groq:qwen/qwen3.8-27b", set())
    groq, gemini = Recorder("groq"), Recorder("gemini")
    router = Router(text_order, {"groq": groq, "gemini": gemini}, vision_candidates=photo_order)

    assert router.complete([Message("user", text="what's this?", images=[PHOTO])], [], "sys").provider == "gemini"
    assert router.complete([Message("user", text="hi")], [], "sys").model == "openai/gpt-oss-120b"
    assert groq.models == ["openai/gpt-oss-120b"]  # the photo never went to Groq


def test_text_only_candidates_cannot_answer_a_photo():
    router = Router([Candidate("groq", "openai/gpt-oss-120b")], {"groq": Recorder("groq")})
    result = run_agent("what's this?", ToolContext(chat_id=1), llm=router.complete, tools={}, images=[PHOTO])
    assert result.trace.outcome == "llm_unavailable"


def test_agent_puts_the_photo_in_the_first_message_and_treats_it_as_private():
    llm = ScriptedProvider([text("It's a concert on Nov 2 at 8pm.")])
    result = run_agent("what's this?", ToolContext(chat_id=1), llm=llm, tools={}, images=[PHOTO])

    assert llm.received[0][0].images == [PHOTO]
    assert llm.sensitive_flags == [True]
    assert result.reply == "It's a concert on Nov 2 at 8pm."


def test_download_picks_the_largest_size_up_to_1280px(monkeypatch):
    sizes = [{"file_id": "small", "width": 320, "height": 240}, {"file_id": "medium", "width": 1280, "height": 960},
             {"file_id": "huge", "width": 2560, "height": 1920}]
    asked = []
    monkeypatch.setattr(telegram, "call", lambda method, payload: asked.append(payload["file_id"]) or
                        {"ok": True, "result": {"file_path": "photos/file_1.jpg"}})
    monkeypatch.setattr(telegram.httpx, "get", lambda url, timeout: httpx.Response(
        200, content=b"jpeg-bytes", request=httpx.Request("GET", url)))

    assert telegram.download_photo(sizes) == Image("image/jpeg", b"jpeg-bytes")
    assert asked == ["medium"]


def test_webhook_sends_a_captioned_photo_to_the_agent(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(main, "TELEGRAM_OWNER_ID", "111")
    monkeypatch.setattr(main, "download_photo", lambda sizes: PHOTO)
    sent, seen = [], []
    monkeypatch.setattr(main, "send_message", lambda chat_id, reply, buttons=None: sent.append(reply))
    monkeypatch.setattr(main, "run_agent", lambda text_, ctx, images=None, **_: seen.append((text_, images)) or
                        AgentResult("Concert, Nov 2 at 8pm.", Trace()))
    client = TestClient(main.app, headers={"X-Telegram-Bot-Api-Secret-Token": "s"})

    def photo_update(update_id, caption=None):
        message = {"photo": [{"file_id": "a", "width": 800, "height": 600}],
                   "chat": {"id": 111}, "from": {"id": 111}}
        return {"update_id": update_id, "message": {**message, **({"caption": caption} if caption else {})}}

    client.post("/telegram/webhook", json=photo_update(1, caption="remind me about this"))
    client.post("/telegram/webhook", json=photo_update(2))

    assert seen == [("remind me about this", [PHOTO]), (main.PHOTO_PROMPT, [PHOTO])]
    assert sent == ["Concert, Nov 2 at 8pm.", "Concert, Nov 2 at 8pm."]
