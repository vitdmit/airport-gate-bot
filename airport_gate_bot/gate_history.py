from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .analytics import UNKNOWN_GATE, is_unknown_gate, normalize_flight
from .airport_gate_enrichment import GateRow, fetch_svo_official_gate_rows_for_date, fetch_vko_official_gate_rows_for_date
from .historical import fetch_historical_operational_rows
from .storage import load_snapshots_around


UNKNOWN = UNKNOWN_GATE


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
    gate_source: str
    gate_match: str
    data_quality: str


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
            item["gate_source"] = _candidate_gate_source(match)
            item["gate_match"] = _join_sources(match_name, match.gate_match)
            item["data_quality"] = _join_sources(item.get("data_quality", ""), _candidate_quality(match))
            item["source_url"] = _join_sources(item.get("source_url", ""), match.source_url)
        elif match and current_gate_is_known:
            if match.terminal and not _is_known(item.get("terminal")):
                item["terminal"] = match.terminal
            item["gate_source"] = _join_sources("post-fact источник; live подтвержден", match.gate_source)
            item["gate_match"] = _join_sources(match_name, match.gate_match)
            item["data_quality"] = _join_sources(item.get("data_quality", ""), _candidate_quality(match))
        elif current_gate_is_known:
            item["gate_source"] = "post-fact источник"
            item["gate_match"] = ""
            item["data_quality"] = item.get("data_quality", "")
        else:
            item["gate_source"] = "не найден"
            item["gate_match"] = ""
            item["data_quality"] = item.get("data_quality", "")

        enriched.append(item)

    _propagate_codeshare_gates(enriched)
    enriched = _merge_codeshare_rows(enriched)
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
                gate_source=record.get("gate_source", ""),
                gate_match=record.get("gate_match", ""),
                data_quality=record.get("data_quality", ""),
            )

            normalized_code = _normalize_flight_code(candidate.flight_code)
            if normalized_code:
                by_flight[(airport, normalized_code)].append(candidate)
            if candidate.destination_iata and candidate.scheduled_time:
                by_destination_scheduled[(airport, candidate.destination_iata, candidate.scheduled_time)].append(candidate)
            if candidate.destination_iata and candidate.departure_time:
                by_destination_departure[(airport, candidate.destination_iata, candidate.departure_time)].append(candidate)

    _add_svo_official_rows(
        target_date,
        by_flight,
        by_destination_scheduled,
        by_destination_departure,
    )
    _add_vko_official_rows(
        target_date,
        by_flight,
        by_destination_scheduled,
        by_destination_departure,
    )

    return {
        "by_flight": by_flight,
        "by_destination_scheduled": by_destination_scheduled,
        "by_destination_departure": by_destination_departure,
    }


def _add_svo_official_rows(
    target_date: date,
    by_flight: dict[tuple[str, ...], list[GateCandidate]],
    by_destination_scheduled: dict[tuple[str, ...], list[GateCandidate]],
    by_destination_departure: dict[tuple[str, ...], list[GateCandidate]],
) -> None:
    try:
        rows, errors = fetch_svo_official_gate_rows_for_date(target_date)
    except Exception as exc:
        print(f"SVO: official archive skipped: {exc}")
        return
    if errors and not rows:
        print(f"SVO: official archive rows not found: {errors[:2]}")
    if rows:
        print(f"SVO: loaded {len(rows)} official archive gate rows")
    collected_at = datetime.now().astimezone()
    for row in rows:
        _add_official_gate_candidate(
            "SVO",
            row,
            target_date,
            collected_at,
            by_flight,
            by_destination_scheduled,
            by_destination_departure,
        )


def _add_vko_official_rows(
    target_date: date,
    by_flight: dict[tuple[str, ...], list[GateCandidate]],
    by_destination_scheduled: dict[tuple[str, ...], list[GateCandidate]],
    by_destination_departure: dict[tuple[str, ...], list[GateCandidate]],
) -> None:
    try:
        rows, errors = fetch_vko_official_gate_rows_for_date(target_date)
    except Exception as exc:
        print(f"VKO: official archive skipped: {exc}")
        return
    if errors and not rows:
        print(f"VKO: official archive rows not found: {errors[:2]}")
    if rows:
        print(f"VKO: loaded {len(rows)} official archive gate rows")
    collected_at = datetime.now().astimezone()
    for row in rows:
        _add_official_gate_candidate(
            "VKO",
            row,
            target_date,
            collected_at,
            by_flight,
            by_destination_scheduled,
            by_destination_departure,
        )


def _add_official_gate_candidate(
    airport: str,
    row: GateRow,
    target_date: date,
    collected_at: datetime,
    by_flight: dict[tuple[str, ...], list[GateCandidate]],
    by_destination_scheduled: dict[tuple[str, ...], list[GateCandidate]],
    by_destination_departure: dict[tuple[str, ...], list[GateCandidate]],
) -> None:
    airport = airport.upper()
    candidate = GateCandidate(
        airport=airport,
        terminal=row.terminal or UNKNOWN,
        gate=row.gate,
        flight_code=row.flight_code,
        destination_iata=row.destination_iata,
        scheduled_time=row.scheduled_time,
        departure_time=row.actual_time or row.scheduled_time,
        collected_at=collected_at,
        source_url=f"official {airport} archive {target_date.isoformat()}",
        gate_source=row.source_label,
        gate_match=f"official {airport} archive: flight + scheduled time",
        data_quality=f"gate from official {airport} archive",
    )
    normalized_code = _normalize_flight_code(candidate.flight_code)
    if normalized_code:
        by_flight[(airport, normalized_code)].append(candidate)
    if candidate.destination_iata and candidate.scheduled_time:
        by_destination_scheduled[(airport, candidate.destination_iata, candidate.scheduled_time)].append(candidate)
    if candidate.destination_iata and candidate.departure_time:
        by_destination_departure[(airport, candidate.destination_iata, candidate.departure_time)].append(candidate)


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

    candidates.sort(key=lambda item: (item[0], _datetime_sort_value(item[2].collected_at)), reverse=True)
    score, match_name, candidate = candidates[0]
    return candidate, f"{match_name}; confidence={score}"


def _candidate_gate_source(candidate: GateCandidate) -> str:
    if "official SVO" in str(candidate.gate_source) or "official VKO" in str(candidate.gate_source):
        return candidate.gate_source
    return _join_sources("live-снимок", candidate.gate_source)


def _candidate_quality(candidate: GateCandidate) -> str:
    notes = []
    if candidate.data_quality:
        notes.append(candidate.data_quality)
    if not candidate.destination_iata:
        notes.append("направление в live-снимке не подтверждено; направление взято из post-fact источника")
    return _join_sources(*notes)


def _propagate_codeshare_gates(rows: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = _codeshare_key(row)
        if key[3]:
            groups[key].append(row)

    for items in groups.values():
        donors = [item for item in items if _is_known(item.get("gate"))]
        if not donors:
            continue
        donor = _best_gate_donor(donors)
        for item in items:
            if _is_known(item.get("gate")):
                continue
            item["terminal"] = donor.get("terminal") or item.get("terminal") or UNKNOWN
            item["gate"] = donor["gate"]
            item["gate_source"] = _join_sources(donor.get("gate_source", ""), "gate взят из кодшера того же вылета")
            item["gate_match"] = _join_sources(
                item.get("gate_match", ""),
                f"кодшеринг: gate взят из {donor.get('flight_numbers', '')}".strip(),
            )
            item["data_quality"] = _join_sources(item.get("data_quality", ""), "gate заполнен по кодшерингу")


def _merge_codeshare_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        minute = _row_minute(row)
        destination_iata = _first_csv(row.get("destination_iata", "")).upper()
        groups[
            (
                str(row.get("airport", "")).upper(),
                minute,
                str(row.get("gate") or UNKNOWN),
                destination_iata,
            )
        ].append(row)

    merged: list[dict[str, Any]] = []
    for items in groups.values():
        if len(items) == 1:
            merged.append(items[0])
            continue
        base = dict(sorted(items, key=lambda row: str(row.get("flight_numbers", "")))[0])
        base["line_type"] = _unique_csv(item.get("line_type", "") for item in items)
        known_terminal = _first_known(item.get("terminal", "") for item in items)
        if known_terminal:
            base["terminal"] = known_terminal
        known_gate = _first_known(item.get("gate", "") for item in items)
        if known_gate:
            base["gate"] = known_gate
        base["scheduled_time"] = _unique_csv(item.get("scheduled_time", "") for item in items)
        base["airlines"] = _unique_csv(item.get("airlines", "") for item in items)
        base["flight_numbers"] = _unique_csv(item.get("flight_numbers", "") for item in items)
        base["destination"] = _unique_csv(item.get("destination", "") for item in items)
        base["destination_iata"] = _unique_csv(item.get("destination_iata", "") for item in items)
        base["status"] = _unique_csv(item.get("status", "") for item in items)
        base["source_url"] = _join_sources(*(item.get("source_url", "") for item in items))
        base["codeshare_rows"] = sum(int(item.get("codeshare_rows") or 1) for item in items)
        base["gate_source"] = _join_sources(*(item.get("gate_source", "") for item in items))
        base["gate_match"] = _join_sources(*(item.get("gate_match", "") for item in items))
        base["data_quality"] = _join_sources(*(item.get("data_quality", "") for item in items))
        merged.append(base)
    return merged


def _codeshare_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("airport", "")).upper(),
        _row_minute(row),
        _first_time(row.get("scheduled_time", "")),
        _first_csv(row.get("destination_iata", "")).upper(),
    )


def _row_minute(row: dict[str, Any]) -> str:
    departure_dt = row.get("departure_dt")
    if departure_dt:
        return departure_dt.replace(second=0, microsecond=0).isoformat()
    return _first_time(row.get("departure_time", ""))


def _best_gate_donor(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(
        rows,
        key=lambda row: (
            "live" in str(row.get("gate_source", "")).lower(),
            "post-fact" in str(row.get("gate_source", "")).lower(),
            str(row.get("flight_numbers", "")),
        ),
        reverse=True,
    )[0]


def _parse_date(value: str | None) -> date:
    if not value:
        return datetime.now().date()
    return date.fromisoformat(value[:10])


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now()
    return datetime.fromisoformat(value)


def _datetime_sort_value(value: datetime) -> float:
    if value.tzinfo is None:
        return value.timestamp()
    return value.astimezone().timestamp()


def _is_known(value: Any) -> bool:
    return not is_unknown_gate(value)


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


def _unique_csv(values) -> str:
    result = []
    for value in values:
        for part in str(value or "").split(","):
            part = part.strip()
            if part and part not in result:
                result.append(part)
    return ", ".join(result)


def _first_known(values) -> str:
    for value in values:
        text = str(value or "").strip()
        if _is_known(text):
            return text
    return ""


def _join_sources(*values: Any) -> str:
    sources = []
    for source in values:
        source = str(source or "").strip()
        if source and source not in sources:
            sources.append(source)
    return " | ".join(sources)
