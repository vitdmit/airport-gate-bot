from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from .analytics import is_unknown_gate
from .flightstats_source import FlightStatsFlight, fetch_daily_departures
from .settings import AIRPORTS


def fetch_historical_operational_rows(target_date: date, airports: list[str] | None = None) -> list[dict]:
    airports = airports or list(AIRPORTS.keys())
    all_flights: list[FlightStatsFlight] = []
    for airport in airports:
        flights = fetch_daily_departures(airport, target_date)
        print(f"{airport}: loaded {len(flights)} historical rows before filtering")
        all_flights.extend(flights)
    return operational_rows_from_flights(all_flights, target_date)


def operational_rows_from_flights(flights: list[FlightStatsFlight], target_date: date) -> list[dict]:
    candidates = []
    for flight in flights:
        if flight.is_cancelled or not flight.has_departed or not flight.actual_departure:
            continue
        if flight.actual_departure.date() != target_date:
            continue
        candidates.append(flight)

    groups: dict[tuple, list[FlightStatsFlight]] = defaultdict(list)
    for flight in candidates:
        minute = flight.actual_departure.replace(second=0, microsecond=0)
        unknown_gate_guard = flight.destination_iata if is_unknown_gate(flight.gate) else ""
        groups[(flight.airport, minute, flight.terminal, flight.gate, unknown_gate_guard)].append(flight)

    rows = []
    for (airport, departure_dt, terminal, gate, _unknown_gate_guard), items in groups.items():
        rows.append(
            {
                "date": departure_dt.date(),
                "airport": airport,
                "line_type": _line_type(items),
                "terminal": terminal,
                "gate": gate,
                "departure_dt": departure_dt,
                "scheduled_time": _unique_join(_fmt_time(item.scheduled_departure) for item in items),
                "airlines": _unique_join(item.airline for item in items),
                "flight_numbers": _unique_join(item.flight_code for item in items),
                "destination": _unique_join(item.destination for item in items),
                "destination_iata": _unique_join(item.destination_iata for item in items),
                "status": _unique_join(item.status for item in items),
                "source_url": items[0].source_url,
                "codeshare_rows": len(items),
            }
        )
    rows.sort(key=lambda row: (row["airport"], row["line_type"], row["terminal"], row["gate"], row["departure_dt"]))
    return rows


def _line_type(items: list[FlightStatsFlight]) -> str:
    values = sorted({"ВВЛ" if item.destination_country == "RU" else "МВЛ" for item in items})
    return values[0] if len(values) == 1 else "/".join(values)


def _fmt_time(value: datetime | None) -> str:
    return value.strftime("%H:%M") if value else ""


def _unique_join(values) -> str:
    result = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in result:
            result.append(value)
    return ", ".join(result)
