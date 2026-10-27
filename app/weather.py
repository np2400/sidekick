"""Weather forecasts from Open-Meteo (free for non-commercial use, no API key): real numbers, not search snippets.

The place name goes to Open-Meteo's geocoding service to find it, then its coordinates to the forecast service.
For US places, the US National Weather Service is also asked for official alerts at those coordinates. If the
forecast or an alert shows something severe, one news search about it runs too (Tavily), so a storm warning is
mentioned without being asked; calm days cost no search. Nothing about the user is sent, only the place.
"""
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from app.config import OWNER_TIMEZONE
from app.web_search import web_search

log = logging.getLogger("sidekick.weather")
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
ALERTS = "https://api.weather.gov/alerts/active"  # US only; free; asks apps to identify themselves
NWS_HEADERS = {"User-Agent": "(Sidekick personal assistant, github.com/np2400)", "Accept": "application/geo+json"}
SEVERE_CODES = {65, 66, 67, 75, 82, 86, 95, 96, 99}  # heavy rain, freezing rain, heavy snow, violent showers, storms
WINDY_MPH = 31  # the US Weather Service's wind-advisory level
DAILY = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max"
CONDITIONS = {  # WMO weather codes, as Open-Meteo documents them
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "violent showers", 85: "light snow showers", 86: "snow showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "thunderstorms with heavy hail",
}
UNAVAILABLE = "The weather service couldn't be reached. Tell the user; don't guess a forecast."


def get_weather(place: str, days: int = 3, *, ctx=None) -> dict:
    """Daily forecast, today first: high and low in °F and °C, chance of rain, conditions, strongest wind. Each day
    says when it is for the person asking ("today", "tomorrow", "in 2 days"), worked out in their timezone.
    At least 3 days, whatever `days` says: on Oct 27 a model asked for 1 day ("tomorrow" = 1?), got only today and
    called it tomorrow. With tomorrow always there, labeled, that can't happen."""
    days = max(3, min(int(days or 3), 16))
    today = today_in(ctx.tz if ctx else OWNER_TIMEZONE)
    try:
        found = httpx.get(GEOCODE, params={"name": place, "count": 1, "language": "en"}, timeout=10)
        matches = (found.json().get("results") or []) if found.status_code == 200 else None
        if matches is None:
            log.error("Open-Meteo geocoding HTTP %s", found.status_code)
            return {"error": UNAVAILABLE}
        if not matches:
            return {"error": f"No place called '{place}' was found. Ask the user which city they mean."}
        spot = matches[0]
        forecast = httpx.get(FORECAST, timeout=10, params={
            "latitude": spot["latitude"], "longitude": spot["longitude"], "daily": DAILY, "timezone": "auto",
            "forecast_days": days, "temperature_unit": "fahrenheit", "wind_speed_unit": "mph"})
    except (httpx.HTTPError, ValueError) as exc:  # ValueError: a body that isn't JSON
        log.error("Open-Meteo error: %s", type(exc).__name__)
        return {"error": UNAVAILABLE}
    if forecast.status_code != 200:
        log.error("Open-Meteo forecast HTTP %s", forecast.status_code)
        return {"error": UNAVAILABLE}

    daily = forecast.json().get("daily", {})
    out, severe, topics = [], [], []
    for i, day in enumerate(daily.get("time", [])):
        high, low = daily["temperature_2m_max"][i], daily["temperature_2m_min"][i]
        code, wind = daily["weather_code"][i], _round(daily["wind_speed_10m_max"][i])
        out.append({
            "date": day, "weekday": date.fromisoformat(day).strftime("%A"), "when": when(date.fromisoformat(day), today),
            "high_f": _round(high), "low_f": _round(low), "high_c": _celsius(high), "low_c": _celsius(low),
            "rain_chance_pct": daily["precipitation_probability_max"][i],
            "conditions": CONDITIONS.get(code, "unknown"), "wind_max_mph": wind,
        })
        windy = (wind or 0) >= WINDY_MPH
        if code in SEVERE_CODES or windy:
            topics.append(out[-1]["conditions"] if code in SEVERE_CODES else "high wind")
            severe.append(f"{out[-1]['weekday']} {day}: {topics[-1]}" + (f", wind up to {wind} mph" if windy else ""))
    where = ", ".join(part for part in (spot.get("name"), spot.get("admin1"), spot.get("country")) if part)
    result = {"place": where, "source": "Open-Meteo", "days": out}
    if spot.get("country_code") == "US":
        result["alerts"] = _official_alerts(spot["latitude"], spot["longitude"])  # None: couldn't check
        result["alerts_source"] = "US National Weather Service"
    if severe:
        result["severe"] = severe
    if severe or result.get("alerts"):  # something worth a warning: what does the news say about it?
        topic = result["alerts"][0]["event"] if result.get("alerts") else f"{topics[0]} warning"
        result["news"] = _news(f"{spot.get('name', place)} {topic}")  # e.g. "Seattle Wind Advisory"
    return result


def _official_alerts(latitude: float, longitude: float) -> list[dict] | None:
    """Active official alerts at a US point (storm, flood, wind, heat...): [] if none, None if it can't be checked."""
    try:
        response = httpx.get(ALERTS, params={"point": f"{latitude:.4f},{longitude:.4f}"}, headers=NWS_HEADERS,
                             timeout=10)
        features = response.json().get("features", []) if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        log.error("Weather Service alerts error: %s", type(exc).__name__)
        return None
    if features is None:
        log.error("Weather Service alerts HTTP %s", response.status_code)
        return None
    return [{"event": p.get("event"), "severity": p.get("severity"), "headline": p.get("headline"),
             "until": p.get("ends") or p.get("expires")} for p in (f.get("properties", {}) for f in features[:5])]


def _news(query: str) -> list[dict] | None:
    """A few recent news results about the warning (untrusted text, like any web result); None if search failed."""
    found = web_search(query, recent="week", max_results=3)
    return None if "error" in found else found["results"]


def today_in(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()  # tests replace this to fix "today"


def when(day: date, today: date) -> str:
    """A forecast day as the person asking would say it: "today", "tomorrow", "yesterday" (a place a day behind
    them, e.g. Hawaii seen from India), or "in 3 days"."""
    gap = (day - today).days
    return {0: "today", 1: "tomorrow", -1: "yesterday"}.get(gap) or (f"in {gap} days" if gap > 0 else f"{-gap} days ago")


def _round(value):
    return None if value is None else round(value)


def _celsius(fahrenheit):
    return None if fahrenheit is None else round((fahrenheit - 32) * 5 / 9)
