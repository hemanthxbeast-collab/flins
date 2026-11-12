"""weather: current conditions and a three-day forecast from Open-Meteo (no account needed).

The input is a place name and nothing else. Two requests: find the place, then its forecast.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, ValidationError

from flins.assistant.tools.base import Tool, ToolResult, fail, ok
from flins.assistant.tools.net import (
    ONLINE_TOOL_TIMEOUT_S,
    Fetcher,
    NetError,
    QueryError,
    safe_query,
)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_PLACE = re.compile(r"[^\W\d_][\w\s,.'-]*", re.UNICODE)

# WMO weather interpretation codes, as Open-Meteo returns them.
_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "violent showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Place(_In):
    name: str
    latitude: float
    longitude: float
    country: str | None = None
    admin1: str | None = None

    def label(self) -> str:
        parts = [self.name]
        for extra in (self.admin1, self.country):
            if extra and extra not in parts:
                parts.append(extra)
        return ", ".join(parts)


class _Places(_In):
    results: list[_Place] = []


class _Current(_In):
    temperature_2m: float
    apparent_temperature: float | None = None
    relative_humidity_2m: float | None = None
    weather_code: int | None = None
    wind_speed_10m: float | None = None


class _Daily(_In):
    time: list[date]
    temperature_2m_max: list[float | None]
    temperature_2m_min: list[float | None]
    weather_code: list[int | None] = []
    precipitation_probability_max: list[float | None] = []


class _Forecast(_In):
    current: _Current
    daily: _Daily


def _sky(code: int | None) -> str | None:
    return None if code is None else _CODES.get(code, "mixed conditions")


def _n(value: float | None) -> str:
    return "?" if value is None else str(round(value))


def _pick(places: list[_Place], hint: str) -> _Place:
    """'Springfield, Illinois': prefer the match whose region or country fits the hint."""
    want = hint.lower()
    if want:
        for p in places:
            if any(want in (x or "").lower() for x in (p.admin1, p.country)):
                return p
    return places[0]


def _at(values: list[float | None] | list[int | None], i: int) -> float | int | None:
    return values[i] if i < len(values) else None


def render(place: _Place, f: _Forecast) -> str:
    c = f.current
    now = [f"{_n(c.temperature_2m)}°C"]
    if c.apparent_temperature is not None:
        now[0] += f" (feels like {_n(c.apparent_temperature)}°C)"
    if (sky := _sky(c.weather_code)) is not None:
        now.append(sky)
    if c.relative_humidity_2m is not None:
        now.append(f"humidity {_n(c.relative_humidity_2m)}%")
    if c.wind_speed_10m is not None:
        now.append(f"wind {_n(c.wind_speed_10m)} km/h")
    lines = [f"Weather for {place.label()} (source: Open-Meteo):", "Now: " + ", ".join(now) + "."]
    d = f.daily
    for i, day in enumerate(d.time[:3]):
        name = ("Today", "Tomorrow")[i] if i < 2 else day.strftime("%A")
        bits = [f"{_n(_at(d.temperature_2m_min, i))} to {_n(_at(d.temperature_2m_max, i))}°C"]
        code = _at(d.weather_code, i)
        if (sky := _sky(None if code is None else int(code))) is not None:
            bits.append(sky)
        rain = _at(d.precipitation_probability_max, i)
        if rain is not None:
            bits.append(f"rain chance {_n(rain)}%")
        lines.append(f"{name} ({day.strftime('%a %d %b')}): " + ", ".join(bits) + ".")
    return "\n".join(lines)


def make_weather_tool(fetcher: Fetcher, default_place: str | None) -> Tool:
    async def run(arg: str, now: datetime) -> ToolResult:
        wanted = arg.strip() or (default_place or "").strip()
        if not wanted:
            return fail("Which place? No default place is set (assistant.tools.online).")
        try:
            wanted = safe_query(wanted, max_chars=80)
        except QueryError as exc:
            return fail(f"Can't look that up: {exc}.")
        if not _PLACE.fullmatch(wanted):
            return fail("The weather tool takes only a place name, like 'Pune' or 'Paris, France'.")
        name, _, hint = (part.strip() for part in wanted.partition(","))
        try:
            found = _Places.model_validate(
                await fetcher.get_json(
                    GEOCODE_URL, {"name": name, "count": "5", "language": "en", "format": "json"}
                )
            )
            if not found.results:
                return fail(f"I couldn't find a place called '{wanted}'.")
            place = _pick(found.results, hint)
            forecast = _Forecast.model_validate(
                await fetcher.get_json(
                    FORECAST_URL,
                    {
                        "latitude": f"{place.latitude:.4f}",
                        "longitude": f"{place.longitude:.4f}",
                        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                        "weather_code,wind_speed_10m",
                        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                        "precipitation_probability_max",
                        "timezone": "auto",
                        "forecast_days": "3",
                    },
                )
            )
        except NetError as exc:
            return fail(f"Couldn't get the weather: {exc}.")
        except ValidationError:
            return fail("Couldn't get the weather: the service sent something I couldn't read.")
        return ok(render(place, forecast))

    return Tool(
        name="weather",
        description=(
            "Current weather and a 3-day forecast for a place. Goes online (Open-Meteo): "
            "only the place name is sent."
        ),
        usage="a place name only, e.g. 'Pune' or 'Paris, France'. Empty = the user's usual place.",
        risk="online",
        run=run,
        outside_content=True,
        timeout_s=ONLINE_TOOL_TIMEOUT_S,
    )
