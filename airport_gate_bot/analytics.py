from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any


DEPARTED_WORDS = ("departed", "landed", "arrived")
CANCELLED_WORDS = ("cancel", "cancelled", "canceled")


def latest_records_from_snapshots(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        airport = snapshot.get("airport", "")
        service_date = _parse_date(snapshot.get("service_date"))
        collected_at = _parse_datetime(snapshot.get("collected_at"))
        source_url = snapshot.get("source_url", "")
        for flight in snapshot.get("flights", []):
            record = normalize_flight(flight, airport, service_date, collected_at, source_url)
            uid = _tracking_key(record)
            previous = latest.get(uid)
            if previous is None:
                latest[uid] = record
            elif record["collected_at"] > previous["collected_at"]:
                latest[uid] = _carry_forward_known_gate(previous, record)
            elif _has_known_gate(record) and not _has_known_gate(previous):
                latest[uid] = _carry_forward_known_gate(record, previous)
    return list(latest.values())


def build_operational_flights(
    records: list[dict[str, Any]],
    target_date: date,
    factual_only: bool = True,
) -> list[dict[str, Any]]:
    filtered = []
    for record in records:
        if record["is_cancelled"]:
            continue
        if factual_only and not record["is_departed"]:
            continue
        if record["departure_dt"].date() != target_date:
            continue
        filtered.append(record)

    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for record in filtered:
        minute = record["departure_dt"].replace(second=0, microsecond=0)
        unknown_gate_guard = record["destination_iata"] if record["gate"] == "не указан" else ""
        key = (
            record["airport"],
            minute,
            record["terminal"],
            record["gate"],
            unknown_gate_guard,
        )
        groups[key].append(record)

    operational = []
    for key, items in groups.items():
        airport, departure_dt, terminal, gate, _unknown_gate_guard = key
        line_types = sorted({item["line_type"] for item in items})
        destinations = _unique_join(item["destination"] for item in items)
        destination_iatas = _unique_join(item["destination_iata"] for item in items)
        airlines = _unique_join(item["airline_name"] for item in items)
        flight_numbers = _unique_join(item["flight_code"] for item in items)
        statuses = _unique_join(item["status"] for item in items)
        operational.append(
            {
                "date": departure_dt.date(),
                "airport": airport,
                "line_type": line_types[0] if len(line_types) == 1 else "/".join(line_types),
                "terminal": terminal,
                "gate": gate,
                "departure_dt": departure_dt,
                "scheduled_time": _unique_join(item["scheduled_time"] for item in items),
                "airlines": airlines,
                "flight_numbers": flight_numbers,
                "destination": destinations,
                "destination_iata": destination_iatas,
                "status": statuses,
                "source_url": items[0]["source_url"],
                "codeshare_rows": len(items),
            }
        )

    operational.sort(key=lambda row: (row["airport"], row["line_type"], row["terminal"], row["gate"], row["departure_dt"]))
    return operational


def summarize_by_gate(operational: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in operational:
        groups[(row["date"], row["airport"], row["line_type"], row["terminal"], row["gate"])].append(row)

    result = []
    for (day, airport, line_type, terminal, gate), rows in groups.items():
        rows.sort(key=lambda item: item["departure_dt"])
        result.append(
            {
                "date": day,
                "airport": airport,
                "line_type": line_type,
                "terminal": terminal,
                "gate": gate,
                "flights_count": len(rows),
                "first_departure": rows[0]["departure_dt"].strftime("%H:%M"),
                "last_departure": rows[-1]["departure_dt"].strftime("%H:%M"),
                "destinations": _unique_join(row["destination"] for row in rows),
                "flight_timeline": _timeline_text(rows),
            }
        )
    result.sort(key=lambda row: (row["airport"], row["line_type"], row["terminal"], _gate_sort_key(row["gate"])))
    return result


def summarize_gate_hour_grid(operational: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in operational:
        groups[(row["date"], row["airport"], row["line_type"], row["terminal"], row["gate"])].append(row)

    result: list[dict[str, Any]] = []
    for (day, airport, line_type, terminal, gate), rows in groups.items():
        hourly = {hour: 0 for hour in range(24)}
        for row in rows:
            hourly[row["departure_dt"].hour] += 1
        result.append(
            {
                "date": day,
                "airport": airport,
                "line_type": line_type,
                "terminal": terminal,
                "gate": gate,
                "total": len(rows),
                "hours": hourly,
            }
        )
    result.sort(key=lambda row: (row["airport"], row["line_type"], row["terminal"], _gate_sort_key(row["gate"])))
    return result


def gate_load_rows(operational: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in operational:
        rows.append(
            {
                "date": row["date"],
                "airport": row["airport"],
                "line_type": row["line_type"],
                "terminal": row["terminal"],
                "gate": row["gate"],
                "hour": row["departure_dt"].hour,
                "time": row["departure_dt"].strftime("%H:%M"),
                "airlines": row["airlines"],
                "flight_numbers": row["flight_numbers"],
                "destination": row["destination"],
                "destination_iata": row["destination_iata"],
                "codeshare_rows": row["codeshare_rows"],
            }
        )
    rows.sort(key=lambda item: (item["airport"], item["terminal"], item["gate"], item["time"], item["flight_numbers"]))
    return rows


def summarize_by_hour(operational: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], int] = defaultdict(int)
    for row in operational:
        groups[(row["date"], row["airport"], row["line_type"], row["departure_dt"].hour)] += 1
    result = []
    for (day, airport, line_type, hour), count in groups.items():
        result.append(
            {
                "date": day,
                "airport": airport,
                "line_type": line_type,
                "hour": f"{hour:02d}:00-{hour:02d}:59",
                "flights_count": count,
            }
        )
    result.sort(key=lambda row: (row["airport"], row["line_type"], row["hour"]))
    return result


def normalize_flight(
    flight: dict[str, Any],
    airport: str,
    service_date: date,
    collected_at: datetime,
    source_url: str,
) -> dict[str, Any]:
    airline = flight.get("airline") or {}
    departure = flight.get("departure") or {}
    arrival = flight.get("arrival") or {}
    original_time = _time_text(flight.get("originalTime"))
    new_time = _time_text(flight.get("newTime"))
    scheduled_dt = _combine_date_time(service_date, original_time)
    departure_dt = _combine_date_time(service_date, new_time, scheduled_dt) if new_time else scheduled_dt
    status = _status_text(flight.get("status", []))
    flight_code = f"{airline.get('iata', '').strip()} {str(flight.get('flightNumber', '')).strip()}".strip()
    arrival_country = _country_from_flag(arrival.get("flag", ""))

    return {
        "flight_uid": str(flight.get("id") or f"{airport}-{flight_code}-{original_time}-{arrival.get('iata', '')}"),
        "airport": airport,
        "service_date": service_date,
        "collected_at": collected_at,
        "source_url": source_url,
        "scheduled_dt": scheduled_dt,
        "departure_dt": departure_dt,
        "scheduled_time": scheduled_dt.strftime("%H:%M"),
        "departure_time": departure_dt.strftime("%H:%M"),
        "terminal": str(departure.get("terminal") or "").strip() or "не указан",
        "gate": str(departure.get("gate") or "").strip() or "не указан",
        "airline_iata": str(airline.get("iata") or "").strip(),
        "airline_name": str(airline.get("name") or "").strip(),
        "flight_number": str(flight.get("flightNumber") or "").strip(),
        "flight_code": flight_code,
        "destination": str(flight.get("city") or "").strip(),
        "destination_iata": str(arrival.get("iata") or "").strip(),
        "arrival_country": arrival_country,
        "line_type": "ВВЛ" if arrival_country == "RU" else "МВЛ",
        "status": status,
        "is_departed": _contains_any(status, DEPARTED_WORDS),
        "is_cancelled": _contains_any(status, CANCELLED_WORDS),
    }


def _parse_date(value: str | None) -> date:
    if not value:
        return datetime.now().date()
    return date.fromisoformat(value[:10])


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now()
    return datetime.fromisoformat(value)


def _time_text(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    return str(value.get("text") or "").strip()


def _status_text(items: list[dict[str, Any]]) -> str:
    texts = [str(item.get("text", "")).strip() for item in items if item.get("type") == "text" and item.get("text")]
    return " ".join(texts)


def _country_from_flag(flag: str) -> str:
    match = re.search(r"/flag/([A-Z]{2})\.svg", flag or "")
    return match.group(1) if match else ""


def _combine_date_time(service_date: date, hhmm: str, scheduled_dt: datetime | None = None) -> datetime:
    parsed = _parse_hhmm(hhmm) or time(0, 0)
    result = datetime.combine(service_date, parsed)
    if scheduled_dt is not None and result < scheduled_dt - timedelta(hours=6):
        result += timedelta(days=1)
    return result


def _parse_hhmm(value: str) -> time | None:
    match = re.search(r"(\d{1,2}):(\d{2})", value or "")
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    text_lower = text.lower()
    return any(word in text_lower for word in words)


def _tracking_key(record: dict[str, Any]) -> str:
    flight_code = str(record.get("flight_code") or "").replace(" ", "").upper()
    destination_iata = str(record.get("destination_iata") or "").upper()
    scheduled_time = str(record.get("scheduled_time") or "")
    if flight_code and destination_iata and scheduled_time:
        return "|".join([record["airport"], record["service_date"].isoformat(), flight_code, destination_iata, scheduled_time])
    return str(record["flight_uid"])


def _carry_forward_known_gate(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    if not _has_known_gate(merged) and _has_known_gate(previous):
        merged["gate"] = previous["gate"]
        if previous.get("terminal") and previous.get("terminal") != "не указан":
            merged["terminal"] = previous["terminal"]
        merged["gate_carried_from"] = previous["collected_at"]
    return merged


def _has_known_gate(record: dict[str, Any]) -> bool:
    gate = str(record.get("gate") or "").strip().lower()
    return bool(gate and gate not in {"не указан", "n/a", "--", "$undefined"})


def _unique_join(values) -> str:
    result = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in result:
            result.append(value)
    return ", ".join(result)


def _timeline_text(rows: list[dict[str, Any]]) -> str:
    parts = []
    for row in rows:
        parts.append(
            " | ".join(
                value
                for value in [
                    row["departure_dt"].strftime("%H:%M"),
                    row.get("airlines", ""),
                    row.get("flight_numbers", ""),
                    row.get("destination", ""),
                ]
                if value
            )
        )
    return "\n".join(parts)


def _gate_sort_key(gate: str) -> tuple[int, str]:
    match = re.search(r"\d+", gate or "")
    return (int(match.group(0)) if match else 9999, gate or "")
