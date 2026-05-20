from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any


BASE_URL = "https://www.flightstats.com/v2"
HOUR_WINDOWS = tuple(range(24))
JINA_PREFIX = "https://r.jina.ai/"
CACHE_DIR = Path("data/cache/flightstats")


class FlightStatsError(RuntimeError):
    pass


@dataclass(frozen=True)
class FlightStatsFlight:
    airport: str
    flight_id: int
    flight_code: str
    airline: str
    destination_iata: str
    destination: str
    destination_country: str
    scheduled_departure: datetime | None
    actual_departure: datetime | None
    terminal: str
    gate: str
    status: str
    source_url: str
    has_departed: bool
    is_cancelled: bool


def fetch_daily_departures(airport: str, target_date: date, workers: int = 1, use_jina: bool = True) -> list[FlightStatsFlight]:
    summaries = _fetch_departure_summaries_jina(airport, target_date) if use_jina else _fetch_departure_summaries(airport, target_date)
    if not summaries:
        return []

    flights: list[FlightStatsFlight] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_fetch_detail_jina if use_jina else _fetch_detail, airport, summary): summary for summary in summaries}
        for future in as_completed(futures):
            try:
                flight = future.result()
            except FlightStatsError as exc:
                print(f"{airport}: detail skipped: {exc}")
                continue
            if flight:
                flights.append(flight)
    flights.sort(key=lambda item: (item.actual_departure or item.scheduled_departure or datetime.max, item.flight_code))
    return flights


def _fetch_departure_summaries(airport: str, target_date: date) -> list[dict[str, Any]]:
    seen: dict[int, dict[str, Any]] = {}
    for hour in HOUR_WINDOWS:
        url = (
            f"{BASE_URL}/flight-tracker/departures/{airport}"
            f"?year={target_date.year}&month={target_date.month}&date={target_date.day}&hour={hour}"
        )
        data = _next_data(url)
        route = data["props"]["initialState"]["flightTracker"].get("route", {})
        for flight in route.get("flights", []):
            flight_id = _flight_id_from_url(flight.get("url", ""))
            if flight_id is None:
                continue
            seen[flight_id] = flight
        time.sleep(0.2)
    return list(seen.values())


def _fetch_departure_summaries_jina(airport: str, target_date: date) -> list[dict[str, Any]]:
    seen: dict[int, dict[str, Any]] = {}
    for hour in HOUR_WINDOWS:
        source_url = (
            f"{BASE_URL}/flight-tracker/departures/{airport}"
            f"?year={target_date.year}&month={target_date.month}&date={target_date.day}&hour={hour}"
        )
        text = _request_jina(source_url)
        for match in re.finditer(r"\]\((https://www\.flightstats\.com/v2/flight-tracker/[^)]+flightId=\d+)\)", text):
            url = match.group(1)
            flight_id = _flight_id_from_url(url)
            if flight_id is None:
                continue
            seen[flight_id] = {"url": url.removeprefix(BASE_URL), "full_url": url}
        time.sleep(0.15)
    return list(seen.values())


def _fetch_detail(airport: str, summary: dict[str, Any]) -> FlightStatsFlight | None:
    path = summary.get("url")
    if not path:
        return None
    source_url = f"{BASE_URL}{path}"
    data = _next_data(source_url)
    flight = data["props"]["initialState"]["flightTracker"].get("flight", {})
    if not flight:
        return None

    note = flight.get("flightNote") or {}
    schedule = flight.get("schedule") or {}
    result = flight.get("resultHeader") or {}
    departure_airport = flight.get("departureAirport") or {}
    arrival_airport = flight.get("arrivalAirport") or {}
    carrier = result.get("carrier") or summary.get("carrier") or {}

    flight_id = int(flight.get("flightId") or _flight_id_from_url(path) or 0)
    flight_code = f"{carrier.get('fs', '')} {result.get('flightNumber') or (summary.get('carrier') or {}).get('flightNumber', '')}".strip()
    actual_departure = _parse_dt(schedule.get("tookOff") or schedule.get("estimatedActualDeparture"))
    scheduled_departure = _parse_dt(schedule.get("scheduledDeparture"))
    has_departed = bool(
        note.get("hasDepartedRunway")
        or schedule.get("tookOff")
        or schedule.get("estimatedActualDepartureTitle") == "Actual"
    )

    return FlightStatsFlight(
        airport=airport,
        flight_id=flight_id,
        flight_code=flight_code,
        airline=str(carrier.get("name") or (summary.get("carrier") or {}).get("name") or "").strip(),
        destination_iata=str(arrival_airport.get("iata") or arrival_airport.get("fs") or (summary.get("airport") or {}).get("fs") or "").strip(),
        destination=str(arrival_airport.get("city") or (summary.get("airport") or {}).get("city") or "").strip(),
        destination_country=str(arrival_airport.get("country") or "").strip(),
        scheduled_departure=scheduled_departure,
        actual_departure=actual_departure,
        terminal=str(departure_airport.get("terminal") or "").strip() or "не указан",
        gate=str(departure_airport.get("gate") or "").strip() or "не указан",
        status=str(result.get("advisoryDisplayStatus") or result.get("status") or "").strip(),
        source_url=source_url,
        has_departed=has_departed,
        is_cancelled=bool(note.get("canceled") or (flight.get("status") or {}).get("statusCode") == "C"),
    )


def _fetch_detail_jina(airport: str, summary: dict[str, Any]) -> FlightStatsFlight | None:
    path = summary.get("url")
    full_url = summary.get("full_url") or (f"{BASE_URL}{path}" if path else "")
    if not full_url:
        return None
    text = _request_jina(full_url)
    block_start = text.find("## Flight Status")
    if block_start < 0:
        raise FlightStatsError(f"Flight status block not found: {full_url}")
    block = text[block_start:]
    lines = [line.strip() for line in block.splitlines() if line.strip()]

    flight_id = _flight_id_from_url(full_url) or 0
    flight_code = _line_after(lines, "## Flight Status") or ""
    airline = _value_after_occurrence(lines, flight_code, 1) or ""
    destination_iata = _second_airport_code(block)
    destination = _destination_city(lines, destination_iata)
    destination_country = _country_after_city(lines, destination)
    status = _first_heading(text) or ""
    scheduled_departure = _parse_report_dt(lines, "Flight Departure Times", "Scheduled")
    actual_departure = _parse_report_dt(lines, "Flight Departure Times", "Actual")
    terminal = _value_after_section(lines, "Flight Departure Times", "Terminal") or "не указан"
    gate = _value_after_section(lines, "Flight Departure Times", "Gate") or "не указан"
    is_cancelled = "cancel" in status.lower()
    has_departed = actual_departure is not None and not is_cancelled

    return FlightStatsFlight(
        airport=airport,
        flight_id=flight_id,
        flight_code=flight_code,
        airline=airline,
        destination_iata=destination_iata,
        destination=destination,
        destination_country=destination_country,
        scheduled_departure=scheduled_departure,
        actual_departure=actual_departure,
        terminal=terminal,
        gate=gate,
        status=status,
        source_url=full_url,
        has_departed=has_departed,
        is_cancelled=is_cancelled,
    )


def _next_data(url: str, retries: int = 3) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            text = _request_text(url)
            marker = "__NEXT_DATA__ = "
            start = text.find(marker)
            if start < 0:
                raise FlightStatsError(f"NEXT_DATA marker not found: {url}")
            data, _end = json.JSONDecoder().raw_decode(text[start + len(marker) :])
            return data
        except (urllib.error.URLError, TimeoutError, FlightStatsError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise FlightStatsError(f"Cannot read FlightStats data from {url}: {last_error}")


def _request_text(url: str) -> str:
    quoted_url = urllib.parse.quote(url, safe=":/?&=*-_.")
    request = urllib.request.Request(
        quoted_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=45, context=ssl.create_default_context()) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _request_jina(source_url: str) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = sha256(source_url.encode("utf-8")).hexdigest()
    cache_path = CACHE_DIR / f"{cache_key}.txt"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")

    jina_url = JINA_PREFIX + source_url
    text = ""
    last_error: Exception | None = None
    for attempt in range(4):
        request = urllib.request.Request(
            jina_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/plain",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120, context=ssl.create_default_context()) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                text = response.read().decode(charset, errors="replace")
            break
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                time.sleep(35 + 15 * attempt)
                continue
            raise
    if not text:
        raise FlightStatsError(f"Cannot read through Jina Reader: {source_url}: {last_error}")
    cache_path.write_text(text, encoding="utf-8")
    return text


def _flight_id_from_url(url: str) -> int | None:
    query = urllib.parse.urlparse(url).query
    params = urllib.parse.parse_qs(query)
    value = (params.get("flightId") or [None])[0]
    return int(value) if value and value.isdigit() else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _line_after(lines: list[str], marker: str) -> str:
    try:
        return lines[lines.index(marker) + 1]
    except (ValueError, IndexError):
        return ""


def _value_after_occurrence(lines: list[str], marker: str, occurrence: int) -> str:
    seen = 0
    for idx, line in enumerate(lines):
        if line == marker:
            seen += 1
            if seen == occurrence and idx + 1 < len(lines):
                return lines[idx + 1]
    return ""


def _value_after_section(lines: list[str], section: str, label: str) -> str:
    try:
        start = lines.index(section)
    except ValueError:
        return ""
    for idx in range(start + 1, min(start + 30, len(lines))):
        if lines[idx] == label and idx + 1 < len(lines):
            value = lines[idx + 1]
            return "" if value.upper() in {"N/A", "--"} else value
    return ""


def _parse_report_dt(lines: list[str], section: str, label: str) -> datetime | None:
    try:
        start = lines.index(section)
    except ValueError:
        return None
    date_value = ""
    for idx in range(start + 1, min(start + 30, len(lines))):
        if re.match(r"\d{1,2}-[A-Za-z]{3}-\d{4}", lines[idx]):
            date_value = lines[idx]
        if lines[idx] == label and idx + 1 < len(lines) and date_value:
            time_match = re.search(r"\d{1,2}:\d{2}", lines[idx + 1])
            if not time_match:
                return None
            time_value = time_match.group(0)
            return datetime.strptime(f"{date_value} {time_value}", "%d-%b-%Y %H:%M")
    return None


def _second_airport_code(block: str) -> str:
    codes = re.findall(r"\[([A-Z]{3})\]\(https://www\.flightstats\.com/v2/airport-conditions/[A-Z]{3}\)", block)
    return codes[1] if len(codes) > 1 else ""


def _destination_city(lines: list[str], destination_iata: str) -> str:
    marker = f"[{destination_iata}](https://www.flightstats.com/v2/airport-conditions/{destination_iata})"
    positions = [idx for idx, line in enumerate(lines) if line == marker]
    for idx in reversed(positions[:2]):
        if idx + 1 < len(lines):
            return lines[idx + 1].split(",", 1)[0].strip()
    return ""


def _country_after_city(lines: list[str], city: str) -> str:
    if not city:
        return ""
    for idx, line in enumerate(lines):
        if line == city or line.startswith(f"{city},"):
            match = re.search(r",\s*([A-Z]{2})$", line)
            if match:
                return match.group(1)
            if idx + 1 < len(lines):
                match = re.search(r",\s*([A-Z]{2})$", lines[idx + 1])
                if match:
                    return match.group(1)
    return ""


def _first_heading(text: str) -> str:
    match = re.search(r"####\s+(.+)", text)
    return match.group(1).strip() if match else ""
