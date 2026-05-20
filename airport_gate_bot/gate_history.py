from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .analytics import normalize_flight
from .historical import fetch_historical_operational_rows
from .storage import load_snapshots_around


UNKNOWN = "не указан"


@dataclass(frozen=True)
class GateCandidate:
    airport: str
    terminal: str
    gate: str
    flight_code: str
    destination_iata: str
    scheduled_time: str
    departure_time: str
    collected_at: datetime
    source_url: str


def build_daily_rows(
    target_date: date,
    airports: list[str],
    data_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    facts = fetch_historical_operational_rows(target_date, airports)
    snapshots = load_snapshots_around(data_dir, target_date)
    return enrich_rows_with_snapshot_gates(facts, snapshots, target_date), snapshots


def enrich_rows_with_snapshot_gates(
    rows: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    target_date: date,
) -> list[dict[str, Any]]:
    index = _build_gate_index(snapshots, target_date)
    enriched: list[dict[str, Any]] = []

    for row in rows:
        item = dict(row)
        match, match_name = _find_best_candidate(item, index)
        current_gate_is_known = _is_known(item.get("gate"))

        if match and not current_gate_is_known:
            item["terminal"] = match.terminal or item.get("terminal") or UNKNOWN
            item["gate"] = match.gate
            item["gate_source"] = "live-снимок"
            item["gate_match"] = match_name
            item["source_url"] = _join_sources(item.get("source_url", ""), match.source_url)
        elif match and current_gate_is_known:
            item["gate_source"] = "post-fact источник; live подтвержден"
            item["gate_match"] = match_name
        elif current_gate_is_known:
            item["gate_source"] = "post-fact источник"
            item["gate_match"] = ""
        else:
            item["gate_source"] = "не найден"
            item["gate_match"] = ""

        enriched.append(item)

    enriched.sort(key=lambda row: (row["airport"], row["line_type"], row["terminal"], row["gate"], row["departure_dt"]))
    return enriched


def _build_gate_index(snapshots: list[dict[str, Any]], target_date: date) -> dict[str, dict[tuple[str, ...], list[GateCandidate]]]:
    by_flight: dict[tuple[str, ...], list[GateCandidate]] = defaultdict(list)
    by_destination_scheduled: dict[tuple[str, ...], list[GateCandidate]] = defaultdict(list)
    by_destination_departure: dict[tuple[str, ...], list[GateCandidate]] = defaultdict(list)

    for snapshot in snapshots:
        airport = str(snapshot.get("airport", "")).upper()
        service_date = _parse_date(snapshot.get("service_date"))
        collected_at = _parse_datetime(snapshot.get("collected_at"))
        source_url = str(snapshot.get("source_url") or "")
        for flight in snapshot.get("flights", []):
            record = normalize_flight(flight, airport, service_date, collected_at, source_url)
            if not _is_known(record.get("gate")):
                continue
            if record["scheduled_dt"].date() != target_date and record["departure_dt"].date() != target_date:
                continue

            candidate = GateCandidate(
                airport=airport,
                terminal=record["terminal"],
                gate=record["gate"],
                flight_code=record["flight_code"],
                destination_iata=record["destination_iata"],
                scheduled_time=record["scheduled_time"],
                departure_time=record["departure_time"],
                collected_at=collected_at,
                source_url=source_url,
            )

            normalized_code = _normalize_flight_code(candidate.flight_code)
            if normalized_code:
                by_flight[(airport, normalized_code)].append(candidate)
            if candidate.destination_iata and candidate.scheduled_time:
                by_destination_scheduled[(airport, candidate.destination_iata, candidate.scheduled_time)].append(candidate)
            if candidate.destination_iata and candidate.departure_time:
                by_destination_departure[(airport, candidate.destination_iata, candidate.departure_time)].append(candidate)

    return {
        "by_flight": by_flight,
        "by_destination_scheduled": by_destination_scheduled,
        "by_destination_departure": by_destination_departure,
    }


def _find_best_candidate(row: dict[str, Any], index: dict[str, dict[tuple[str, ...], list[GateCandidate]]]) -> tuple[GateCandidate | None, str]:
    airport = str(row.get("airport", "")).upper()
    destination_iata = _first_csv(row.get("destination_iata", ""))
    scheduled_time = _first_time(row.get("scheduled_time", ""))
    departure_time = row.get("departure_dt").strftime("%H:%M") if row.get("departure_dt") else ""

    candidates: list[tuple[int, str, GateCandidate]] = []
    for code in _flight_codes(row.get("flight_numbers", "")):
        for candidate in index["by_flight"].get((airport, code), []):
            candidates.append((100, "номер рейса", candidate))

    if destination_iata and scheduled_time:
        for candidate in index["by_destination_scheduled"].get((airport, destination_iata, scheduled_time), []):
            candidates.append((70, "направление + плановое время", candidate))

    if destination_iata and departure_time:
        for candidate in index["by_destination_departure"].get((airport, destination_iata, departure_time), []):
            candidates.append((60, "направление + фактическое время", candidate))

    if not candidates:
        return None, ""

    candidates.sort(key=lambda item: (item[0], item[2].collected_at), reverse=True)
    score, match_name, candidate = candidates[0]
    return candidate, f"{match_name}; confidence={score}"


def _parse_date(value: str | None) -> date:
    if not value:
        return datetime.now().date()
    return date.fromisoformat(value[:10])


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now()
    return datetime.fromisoformat(value)


def _is_known(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text and text.lower() not in {"не указан", "n/a", "--", "$undefined"})


def _flight_codes(value: Any) -> list[str]:
    result: list[str] = []
    for part in str(value or "").split(","):
        normalized = _normalize_flight_code(part)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _normalize_flight_code(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    return text


def _first_csv(value: Any) -> str:
    return str(value or "").split(",", 1)[0].strip()


def _first_time(value: Any) -> str:
    match = re.search(r"\d{1,2}:\d{2}", str(value or ""))
    return match.group(0) if match else ""


def _join_sources(left: str, right: str) -> str:
    sources = []
    for source in (left, right):
        source = str(source or "").strip()
        if source and source not in sources:
            sources.append(source)
    return " | ".join(sources)
