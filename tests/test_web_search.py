"""web_search talks to Tavily; here Tavily is faked, so these run offline."""
import httpx

from app import agent, web_search as ws


def fake_tavily(monkeypatch, status=200, payload=None):
    sent = {}

    def post(url, json, headers, timeout):
        sent.update(url=url, body=json, headers=headers)
        return httpx.Response(status, json=payload or {}, request=httpx.Request("POST", url))

    monkeypatch.setattr(ws, "TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(ws.httpx, "post", post)
    return sent


def test_results_are_trimmed_to_title_link_and_short_snippet(monkeypatch):
    sent = fake_tavily(monkeypatch, payload={"results": [
        {"title": "Weekend forecast", "url": "https://weather.example/sd", "content": "Sunny. " * 200, "score": 0.9},
    ]})
    result = ws.web_search("weather San Diego this weekend", recent="week", max_results=50)

    assert result["results"] == [{"title": "Weekend forecast", "url": "https://weather.example/sd",
                                  "snippet": ("Sunny. " * 200)[:ws.MAX_SNIPPET]}]
    assert sent["body"] == {"query": "weather San Diego this weekend", "max_results": 8, "time_range": "week"}
    assert sent["headers"]["Authorization"] == "Bearer tvly-test"


def test_missing_key_explains_instead_of_calling_out(monkeypatch):
    monkeypatch.setattr(ws, "TAVILY_API_KEY", "")
    assert "TAVILY_API_KEY" in ws.web_search("anything")["error"]


def test_monthly_limit_tells_the_model_not_to_retry(monkeypatch):
    fake_tavily(monkeypatch, status=432)
    assert "Do not retry" in ws.web_search("anything")["error"]


def test_the_model_can_see_and_call_web_search():
    assert "web_search" in {spec.name for spec in agent.tool_specs(agent.TOOLS)}
    assert "web_search" not in agent.WRITE_TOOLS  # a read: runs without approval
