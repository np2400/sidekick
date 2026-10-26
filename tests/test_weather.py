"""The weather tool (Open-Meteo), the dates in the model's prompt, and /forget_all. Offline: HTTP is faked."""
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import weather
from app.agent import MEMBER_REGISTRIES, SYSTEM_PROMPT, TOOLS, system_prompt, tool_specs
from evals.scorers import reply_contains_none
from evals.world import FakeWorld

SEATTLE = {"results": [{"name": "Seattle", "admin1": "Washington", "country": "United States", "country_code": "US",
                        "latitude": 47.6, "longitude": -122.33, "timezone": "America/Los_Angeles"}]}
PUNE = {"results": [{"name": "Pune", "admin1": "Maharashtra", "country": "India", "country_code": "IN",
                     "latitude": 18.52, "longitude": 73.86}]}
FORECAST = {"daily": {"time": ["2026-10-26", "2026-10-27"], "weather_code": [3, 61],
                      "temperature_2m_max": [55.4, 52.1], "temperature_2m_min": [46.0, 43.6],
                      "precipitation_probability_max": [40, 85], "wind_speed_10m_max": [8.6, None]}}
STORMY = {"daily": {**FORECAST["daily"], "weather_code": [3, 95], "wind_speed_10m_max": [8.6, 12.0]}}
WINDY = {"daily": {**FORECAST["daily"], "weather_code": [3, 3], "wind_speed_10m_max": [8.6, 38.2]}}
NO_ALERTS = {"type": "FeatureCollection", "features": []}
WIND_ADVISORY = {"features": [{"properties": {
    "event": "Wind Advisory", "severity": "Moderate", "headline": "Wind Advisory until October 27 at 6:00PM PDT",
    "ends": "2026-10-27T18:00:00-07:00", "expires": "2026-10-27T09:00:00-07:00", "description": "Long text..."}}]}


@pytest.fixture
def open_meteo(monkeypatch):
    """Answers like Open-Meteo and the US Weather Service would; records what was asked and every news search."""
    asked, answers = [], {"geocode": (200, SEATTLE), "forecast": (200, FORECAST), "alerts": (200, NO_ALERTS)}
    searches = []

    def fake_get(url, params=None, timeout=None, headers=None):
        step = "geocode" if "geocoding" in url else "alerts" if "weather.gov" in url else "forecast"
        asked.append((step, params, headers))
        status, body = answers[step]
        if isinstance(status, Exception):
            raise status
        return httpx.Response(status, json=body)
    monkeypatch.setattr(weather.httpx, "get", fake_get)
    monkeypatch.setattr(weather, "web_search", lambda query, recent="", max_results=5: searches.append(query) or {
        "query": query, "results": [{"title": "Storm watch - KOMO News", "url": "https://komo.example", "snippet": "..."}]})
    answers["searches"] = searches
    return asked, answers


def test_a_forecast_has_real_numbers_in_both_units_for_the_place_found(open_meteo):
    asked, _ = open_meteo
    result = weather.get_weather("Seattle", days=2)
    assert result["place"] == "Seattle, Washington, United States" and result["source"] == "Open-Meteo"
    assert result["days"][1] == {"date": "2026-10-27", "weekday": "Tuesday", "high_f": 52, "low_f": 44,
                                 "high_c": 11, "low_c": 6,  # from the real 43.6°F, not the rounded 44
                                 "rain_chance_pct": 85, "conditions": "light rain",
                                 "wind_max_mph": None}  # a missing value stays missing, never invented
    assert asked[0][1]["name"] == "Seattle"  # only the place is sent
    assert asked[1][1]["latitude"] == 47.6 and asked[1][1]["forecast_days"] == 2


def test_a_calm_us_forecast_checks_official_alerts_and_spends_no_search(open_meteo):
    asked, answers = open_meteo
    result = weather.get_weather("Seattle")
    assert result["alerts"] == [] and result["alerts_source"] == "US National Weather Service"
    assert "severe" not in result and "news" not in result and answers["searches"] == []
    alerts_call = next(a for a in asked if a[0] == "alerts")
    assert alerts_call[1] == {"point": "47.6000,-122.3300"} and "Sidekick" in alerts_call[2]["User-Agent"]


def test_an_official_alert_comes_with_news_about_it(open_meteo):
    open_meteo[1]["alerts"] = (200, WIND_ADVISORY)
    result = weather.get_weather("Seattle")
    assert result["alerts"] == [{"event": "Wind Advisory", "severity": "Moderate",
                                 "headline": "Wind Advisory until October 27 at 6:00PM PDT",
                                 "until": "2026-10-27T18:00:00-07:00"}]  # the long description stays out
    assert open_meteo[1]["searches"] == ["Seattle Wind Advisory"] and result["news"][0]["title"].startswith("Storm")


@pytest.mark.parametrize("forecast, search", [(STORMY, "Pune thunderstorms warning"), (WINDY, "Pune high wind warning")])
def test_severe_weather_abroad_flags_the_day_and_checks_the_news(open_meteo, forecast, search):
    asked, answers = open_meteo
    answers["geocode"], answers["forecast"] = (200, PUNE), (200, forecast)
    result = weather.get_weather("Pune, India")
    assert "alerts" not in result and not any(a[0] == "alerts" for a in asked)  # the US service is US-only
    assert result["severe"][0].startswith("Tuesday 2026-10-27: ") and answers["searches"] == [search]


@pytest.mark.parametrize("trouble", [503, httpx.ConnectTimeout("no answer")])
def test_if_alerts_or_news_cant_be_checked_the_forecast_still_comes(open_meteo, monkeypatch, trouble):
    open_meteo[1]["alerts"] = (trouble, {})
    result = weather.get_weather("Seattle")
    assert result["alerts"] is None and result["days"][1]["high_f"] == 52  # None = couldn't check, not "no alerts"

    open_meteo[1]["forecast"] = (200, STORMY)
    monkeypatch.setattr(weather, "web_search", lambda *a, **k: {"error": "Web search limit reached for now."})
    assert weather.get_weather("Seattle")["news"] is None


def test_an_unknown_place_asks_instead_of_guessing(open_meteo):
    open_meteo[1]["geocode"] = (200, {})
    assert weather.get_weather("Xyzzyville") == {
        "error": "No place called 'Xyzzyville' was found. Ask the user which city they mean."}


@pytest.mark.parametrize("trouble", [("geocode", 503), ("forecast", 500), ("forecast", httpx.ReadTimeout("slow"))])
def test_when_the_service_is_down_it_says_so(open_meteo, trouble):
    step, status = trouble
    open_meteo[1][step] = (status, {})
    assert weather.get_weather("Seattle") == {"error": weather.UNAVAILABLE}


def test_days_are_kept_between_1_and_16(open_meteo):
    weather.get_weather("Seattle", days=40)
    weather.get_weather("Seattle", days=0)
    assert [params["forecast_days"] for step, params, _ in open_meteo[0] if step == "forecast"] == [16, 3]


def test_everyone_gets_the_weather_tool_and_is_told_to_use_it():
    assert TOOLS["get_weather"] is weather.get_weather
    assert all("get_weather" in tools for tools in MEMBER_REGISTRIES.values())
    assert "get_weather" in [spec.name for spec in tool_specs({"get_weather": weather.get_weather})]
    assert "call get_weather, not web_search" in SYSTEM_PROMPT and "Polymarket" in SYSTEM_PROMPT


def test_the_prompt_spells_out_the_next_week_so_dates_cant_slip():
    """Regression: after /new the model said 'the day after tomorrow is Wednesday, 27 October' (it was the 28th)."""
    now = datetime(2026, 10, 26, 15, 29, tzinfo=ZoneInfo("America/Los_Angeles"))
    prompt = system_prompt(tz="America/Los_Angeles", now=now)
    assert "Current time: Monday 2026-10-26 15:29" in prompt and "Tomorrow is Tuesday 2026-10-27." in prompt
    assert "Then: Wednesday 2026-10-28, Thursday 2026-10-29," in prompt and "Monday 2026-11-02." in prompt


def test_the_eval_world_has_the_same_weather_tool_shape(open_meteo):
    world = FakeWorld(weather={"place": "Seattle, Washington, United States", "start": "2026-10-26",
                               "days": [{"high_f": 55}, {"high_f": 52}], "alerts": [],
                               "alerts_source": "US National Weather Service"})
    fake, real = world.get_weather("Seattle", days=2), weather.get_weather("Seattle", days=2)
    assert fake.keys() == real.keys() and fake["days"][1]["date"] == real["days"][1]["date"] == "2026-10-27"
    assert world.calls == [("get_weather", {"place": "Seattle", "days": 2})]


def test_a_reply_that_cites_a_betting_market_fails_the_safety_check():
    ok, reason = reply_contains_none({"final_reply": "Highs near 54°F (Polymarket odds)."}, ["polymarket"])
    assert not ok and "polymarket" in reason
    assert reply_contains_none({"final_reply": "High 52°F, low 44°F (Open-Meteo)."}, ["polymarket"])[0]
