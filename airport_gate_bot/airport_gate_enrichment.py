from __future__ import annotations

import html
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .dme_source import enrich_dme_gates
from .settings import MOSCOW_TZ


SVO_TIMETABLE_URL = "https://www.svo.aero/ru/timetable/departure?date=today&period={period}&terminal=all"
VKO_ONLINE_URLS = (
    "https://www.vnukovo.ru/ru/for-passengers/flights/online/",
    "https://www.vnukovo.ru/flights/online-timetable/#tab-sortie",
)
BACKUP_BOARD_URL = "https://www.airports-worldwide.info/airport/{airport}/departures"
AIRPORT_INFORMATION_URL = "https://www.airportinformation.com/{airport}/departures"
PLANEFINDER_DEPARTURES_URL = "https://planefinder.net/data/airport/{airport}/departures"
FIDS_LIVE_DEPARTURES_URL = "https://www.fids.live/{airport}/departures"
KUPI_TIMETABLE_URL = "https://www.kupi.com/en-ae/explore/russian-federation/moscow/{airport}/timetable"
KUPI_AIRPORT_SLUGS = {
    "DME": "domodedovo",
    "VKO": "vnukovo",
}
JINA_READER_PREFIX = "https://r.jina.ai/"


@dataclass(frozen=True)
class GateRow:
    flight_code: str
    scheduled_time: str
    actual_time: str
    terminal: str
    gate: str
    destination_iata: str
    source_label: str


def enrich_airport_gates(airport: str, flights: list[dict[str, Any]]) -> dict[str, Any]:
    airport = airport.upper()
    if airport == "DME":
        return _enrich_dme_gates(flights)
    if airport == "SVO":
        return _enrich_svo_gates(flights)
    if airport == "VKO":
        return _enrich_vko_gates(flights)
    return {}


def _enrich_dme_gates(flights: list[dict[str, Any]]) -> dict[str, Any]:
    meta = enrich_dme_gates(flights)

    # DME official mobile board fills rows we already have. Backup boards can
    # also add rows that the main live source omitted.
    backup_rows, backup_error = _fetch_backup_rows("DME")
    backup_filled, backup_conflicts = _apply_gate_rows(flights, backup_rows)
    backup_added = _append_gate_rows_as_flights(flights, backup_rows, "DME")
    meta.update({
        "dme_missing_after_backup": _count_missing_gates(flights),
        "dme_backup_rows": len(backup_rows),
        "dme_backup_filled": backup_filled,
        "dme_backup_added_missing_flights": backup_added,
        "dme_backup_conflicts": backup_conflicts,
        "dme_backup_error": backup_error,
    })
    return meta


def _enrich_svo_gates(flights: list[dict[str, Any]]) -> dict[str, Any]:
    missing_before = _count_missing_gates(flights)
    rows: list[GateRow] = []
    errors: list[str] = []
    for period in _svo_periods(datetime.now(ZoneInfo(MOSCOW_TZ))):
        url = SVO_TIMETABLE_URL.format(period=period)
        try:
            text = _request_text(url)
        except Exception as exc:
            errors.append(f"official SVO {period}: {exc}")
            continue
        rows.extend(_parse_gate_rows(text, "официальный SVO"))

    filled, conflicts = _apply_gate_rows(flights, rows)
    missing_after_official = _count_missing_gates(flights)
    meta = {
        "svo_gate_enrichment_needed": int(missing_before > 0),
        "svo_missing_before": missing_before,
        "svo_missing_after_official": missing_after_official,
        "svo_official_checked": 1,
        "svo_official_rows": len(rows),
        "svo_official_filled": filled,
        "svo_official_conflicts": conflicts,
        "svo_official_errors": errors[:3],
    }

    if not missing_after_official:
        return meta

    backup_rows, backup_error = _fetch_backup_rows("SVO")
    backup_filled, backup_conflicts = _apply_gate_rows(flights, backup_rows)
    meta.update({
        "svo_missing_after_backup": _count_missing_gates(flights),
        "svo_backup_rows": len(backup_rows),
        "svo_backup_filled": backup_filled,
        "svo_backup_conflicts": backup_conflicts,
        "svo_backup_error": backup_error,
    })
    return meta


def _enrich_vko_gates(flights: list[dict[str, Any]]) -> dict[str, Any]:
    missing_before = _count_missing_gates(flights)
    rows: list[GateRow] = []
    errors: list[str] = []
    for url in VKO_ONLINE_URLS:
        try:
            text = _request_text(url)
        except Exception as exc:
            errors.append(f"official VKO: {exc}")
            continue
        rows.extend(_parse_gate_rows(text, "официальный VKO"))

    filled, conflicts = _apply_gate_rows(flights, rows)
    missing_after_official = _count_missing_gates(flights)
    official_added = _append_gate_rows_as_flights(flights, rows, "VKO")
    meta = {
        "vko_gate_enrichment_needed": int(missing_before > 0),
        "vko_missing_before": missing_before,
        "vko_missing_after_official": missing_after_official,
        "vko_official_checked": 1,
        "vko_official_rows": len(rows),
        "vko_official_filled": filled,
        "vko_official_added_missing_flights": official_added,
        "vko_official_conflicts": conflicts,
        "vko_official_errors": errors[:3],
    }

    # VKO/Flighty can omit some carriers entirely, especially Pobeda (DP).
    # So the backup board is useful even when all Flighty rows already have gates.
    backup_rows, backup_error = _fetch_backup_rows("VKO")
    backup_filled, backup_conflicts = _apply_gate_rows(flights, backup_rows)
    backup_added = _append_gate_rows_as_flights(flights, backup_rows, "VKO")
    meta.update({
        "vko_missing_after_backup": _count_missing_gates(flights),
        "vko_backup_rows": len(backup_rows),
        "vko_backup_filled": backup_filled,
        "vko_backup_added_missing_flights": backup_added,
        "vko_backup_conflicts": backup_conflicts,
        "vko_backup_error": backup_error,
    })
    return meta


def _fetch_backup_rows(airport: str) -> tuple[list[GateRow], str]:
    errors: list[str] = []
    rows: list[GateRow] = []
    sources = [
        (PLANEFINDER_DEPARTURES_URL.format(airport=airport), f"PlaneFinder {airport}"),
        (FIDS_LIVE_DEPARTURES_URL.format(airport=airport.lower()), f"fids.live {airport}"),
        *(
            [(KUPI_TIMETABLE_URL.format(airport=KUPI_AIRPORT_SLUGS[airport]), f"Kupi {airport}")]
            if airport in KUPI_AIRPORT_SLUGS
            else []
        ),
        (AIRPORT_INFORMATION_URL.format(airport=airport), f"airportinformation.com {airport}"),
        (BACKUP_BOARD_URL.format(airport=airport), f"доп. live-табло {airport}"),
    ]
    for url, label in sources:
        try:
            text = _request_text(url)
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            continue
        source_rows = _parse_gate_rows(text, label)
        if source_rows:
            rows.extend(source_rows)
            continue
        errors.append(f"{label}: rows not found")
    return _dedupe_gate_rows(rows), "; ".join(errors)


def _parse_gate_rows(text: str, source_label: str) -> list[GateRow]:
    if "planefinder" in source_label.lower():
        return _parse_planefinder_rows(text, source_label)
    if "fids.live" in source_label.lower():
        return _parse_fids_live_rows(text, source_label)
    if "kupi" in source_label.lower():
        return _parse_kupi_rows(text, source_label)
    rows = _parse_html_table_rows(text, source_label)
    if rows:
        return rows
    rows = _parse_markdown_table_rows(text, source_label)
    if rows:
        return rows
    rows = _parse_airport_information_rows(text, source_label)
    if rows:
        return rows
    return _parse_text_table_rows(text, source_label)


def _parse_planefinder_rows(text: str, source_label: str) -> list[GateRow]:
    if not _planefinder_page_is_current(text):
        return []

    rows: list[GateRow] = []
    lines = _plain_lines(text)
    for idx, line in enumerate(lines):
        if not re.fullmatch(r"\d{1,2}:\d{2}\s+[A-Z]{2,4}", line):
            continue

        times = _times_from_text(line)
        if not times:
            continue

        flight_idx = -1
        flight_codes: list[str] = []
        for probe_idx in range(idx + 1, min(idx + 8, len(lines))):
            flight_codes = _flight_codes_from_cell(lines[probe_idx])
            if flight_codes:
                flight_idx = probe_idx
                break
        if flight_idx < 0:
            continue

        destination_iata = ""
        for probe_idx in range(flight_idx + 1, min(flight_idx + 5, len(lines))):
            destination_iata = _destination_iata_from_text(lines[probe_idx])
            if destination_iata:
                break

        terminal = ""
        gate = ""
        status_idx = -1
        for probe_idx in range(flight_idx + 1, min(flight_idx + 9, len(lines))):
            terminal, gate = _planefinder_terminal_gate(lines[probe_idx])
            if gate:
                status_idx = probe_idx
                break
        if not gate:
            continue

        actual_time = ""
        if status_idx + 1 < len(lines):
            next_times = _times_from_text(lines[status_idx + 1])
            if next_times and not re.search(r"\b[A-Z]{2,4}\b", lines[status_idx + 1]):
                actual_time = next_times[0]

        for flight_code in flight_codes:
            rows.append(
                GateRow(
                    flight_code=flight_code,
                    scheduled_time=times[0],
                    actual_time=actual_time,
                    terminal=terminal,
                    gate=gate,
                    destination_iata=destination_iata,
                    source_label=source_label,
                )
            )
    return rows


def _planefinder_terminal_gate(value: str) -> tuple[str, str]:
    if not re.search(r"\b(?:boarding|cancelled|canceled|departed|departing|delayed|estimated|on time|scheduled)\b", value or "", re.I):
        return "", ""

    text = re.sub(
        r"\b(?:boarding|cancelled|canceled|departed|departing|delayed|estimated|on time|scheduled)\b.*$",
        "",
        _plain(value),
        flags=re.I,
    ).strip()
    if not text:
        return "", ""

    parts = text.split()
    if len(parts) >= 2 and re.fullmatch(r"[A-Z]", parts[0], re.I) and re.search(r"\d", parts[1]):
        return _clean_terminal(parts[0]), _clean_gate(parts[1])
    return "", _clean_gate(text)


def _planefinder_page_is_current(text: str) -> bool:
    match = re.search(
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
        r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b",
        text or "",
        re.I,
    )
    if not match:
        return True

    months = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    month = months.get(match.group(2).lower())
    if not month:
        return False
    page_date = datetime(int(match.group(3)), month, int(match.group(1))).date()
    return page_date == datetime.now(ZoneInfo(MOSCOW_TZ)).date()


def _parse_fids_live_rows(text: str, source_label: str) -> list[GateRow]:
    if not _fids_live_page_is_current(text):
        return []

    rows: list[GateRow] = []
    lines = _plain_lines(text)
    for idx in range(0, max(len(lines) - 4, 0)):
        times = _times_from_text(lines[idx])
        if not times or not re.fullmatch(r"\d{1,2}:\d{2}", lines[idx]):
            continue

        flight_codes = _flight_codes_from_cell(lines[idx + 2])
        gate = _clean_gate(lines[idx + 4])
        if not flight_codes or not gate:
            continue

        actual_time = ""
        status_times = _times_from_text(lines[idx + 3])
        if status_times:
            actual_time = status_times[-1]

        for flight_code in flight_codes:
            rows.append(
                GateRow(
                    flight_code=flight_code,
                    scheduled_time=times[0],
                    actual_time=actual_time,
                    terminal="",
                    gate=gate,
                    destination_iata="",
                    source_label=source_label,
                )
            )
    return rows


def _fids_live_page_is_current(text: str) -> bool:
    match = re.search(r"\bToday\s+([A-Za-z]+)\s+(\d{1,2})\b", text or "", re.I)
    if not match:
        return True

    months = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    month = months.get(match.group(1).lower())
    if not month:
        return False
    today = datetime.now(ZoneInfo(MOSCOW_TZ)).date()
    return month == today.month and int(match.group(2)) == today.day


def _parse_kupi_rows(text: str, source_label: str) -> list[GateRow]:
    if not _kupi_page_is_current(text):
        return []

    rows: list[GateRow] = []
    lines = _plain_lines(text)
    for idx, line in enumerate(lines):
        times = _times_from_text(line)
        if not times or not re.fullmatch(r"\d{1,2}:\d{2}", line):
            continue
        if (
            idx > 0
            and re.fullmatch(r"\d{1,2}:\d{2}", lines[idx - 1])
            and idx + 1 < len(lines)
            and _destination_iata_from_text(lines[idx + 1])
        ):
            continue

        actual_time = ""
        destination_idx = idx + 1
        if destination_idx < len(lines):
            possible_actual = _times_from_text(lines[destination_idx])
            if (
                possible_actual
                and re.fullmatch(r"\d{1,2}:\d{2}", lines[destination_idx])
                and destination_idx + 1 < len(lines)
                and _destination_iata_from_text(lines[destination_idx + 1])
            ):
                actual_time = possible_actual[0]
                destination_idx += 1

        if destination_idx >= len(lines):
            continue
        destination_iata = _destination_iata_from_text(lines[destination_idx])
        if not destination_iata:
            continue

        flight_idx = -1
        flight_codes: list[str] = []
        for probe_idx in range(destination_idx + 1, min(destination_idx + 7, len(lines))):
            flight_codes = _flight_codes_from_cell(lines[probe_idx])
            if flight_codes:
                flight_idx = probe_idx
                break
        if flight_idx < 0:
            continue

        status_idx = -1
        for probe_idx in range(flight_idx + 1, min(flight_idx + 6, len(lines))):
            if _looks_kupi_status(lines[probe_idx]):
                status_idx = probe_idx
                break
        if status_idx < 0 or status_idx + 1 >= len(lines):
            continue

        if not actual_time:
            status_times = _times_from_text(lines[status_idx])
            if status_times:
                actual_time = status_times[-1]

        terminal, gate = _kupi_terminal_gate(lines[status_idx + 1])
        if not gate:
            continue

        for flight_code in flight_codes:
            rows.append(
                GateRow(
                    flight_code=flight_code,
                    scheduled_time=times[0],
                    actual_time=actual_time,
                    terminal=terminal,
                    gate=gate,
                    destination_iata=destination_iata,
                    source_label=source_label,
                )
            )
    return rows


def _kupi_page_is_current(text: str) -> bool:
    match = re.search(r"\bDeparture date\s+(\d{1,2})\s+([A-Za-z]+),\s+today\b", text or "", re.I)
    if not match:
        return "Departure date" not in (text or "")

    months = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    month = months.get(match.group(2).lower())
    if not month:
        return False
    today = datetime.now(ZoneInfo(MOSCOW_TZ)).date()
    return int(match.group(1)) == today.day and month == today.month


def _looks_kupi_status(value: str) -> bool:
    return bool(
        re.search(
            r"\b(?:arrived|cancelled|canceled|delayed|departed|in flight|on schedule|boarding|scheduled)\b",
            value or "",
            re.I,
        )
    )


def _kupi_terminal_gate(value: str) -> tuple[str, str]:
    text = _plain(value).upper().replace("—", "-").strip()
    text = re.sub(r"\s+", " ", text)
    if not text or text in {"-", "--", "N/A"}:
        return "", ""

    match = re.fullmatch(r"T?([A-Z])\s+(\d{1,3}[A-Z]?)", text)
    if match:
        return _clean_terminal(match.group(1)), _clean_gate(match.group(2))

    match = re.fullmatch(r"([A-Z])(\d{1,3}[A-Z]?)", text)
    if match:
        return _clean_terminal(match.group(1)), _clean_gate(match.group(2))

    if re.fullmatch(r"\d{1,3}[A-Z]?", text):
        return "", _clean_gate(text)

    return "", ""


def _parse_html_table_rows(text: str, source_label: str) -> list[GateRow]:
    rows: list[GateRow] = []
    for block in re.findall(r"<tr\b.*?</tr>", text or "", flags=re.I | re.S):
        cells = [_plain(cell) for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", block, flags=re.I | re.S)]
        rows.extend(_rows_from_cells(cells, source_label))
    return rows


def _parse_markdown_table_rows(text: str, source_label: str) -> list[GateRow]:
    rows: list[GateRow] = []
    for line in (text or "").splitlines():
        if "|" not in line:
            continue
        cells = [_plain(cell) for cell in line.split("|")]
        if cells and not cells[0]:
            cells = cells[1:]
        if cells and not cells[-1]:
            cells = cells[:-1]
        if len(cells) < 7:
            continue
        lowered = " ".join(cells).lower()
        if "destination" in lowered and "flight" in lowered:
            continue
        if all(set(cell) <= {"-"} for cell in cells if cell):
            continue
        rows.extend(_rows_from_cells(cells, source_label))
    return rows


def _parse_airport_information_rows(text: str, source_label: str) -> list[GateRow]:
    rows: list[GateRow] = []
    lines = _plain_lines(text)
    for idx, line in enumerate(lines):
        if not re.fullmatch(r"\d{1,2}:\d{2}", line):
            continue
        if idx + 4 >= len(lines):
            continue
        destination = lines[idx + 1]
        flight_cell = lines[idx + 2]
        gate = _clean_gate(lines[idx + 4])
        if not gate:
            continue
        for flight_code in _flight_codes_from_cell(flight_cell):
            rows.append(
                GateRow(
                    flight_code=flight_code,
                    scheduled_time=_times_from_text(line)[0],
                    actual_time="",
                    terminal="",
                    gate=gate,
                    destination_iata="",
                    source_label=source_label,
                )
            )
    return rows


def _parse_text_table_rows(text: str, source_label: str) -> list[GateRow]:
    rows: list[GateRow] = []
    plain = _plain(text)
    pattern = re.compile(
        r"(?P<destination>[A-Za-zА-Яа-яёЁ \-/]+)?\s*"
        r"\((?P<iata>[A-Z]{3})\)\s+"
        r"(?P<time>\d{1,2}:\d{2}(?:\s+\d{1,2}:\d{2})?)\s+"
        r"(?P<status>.*?)\s+"
        r"(?P<flight>[A-ZА-Я0-9]{1,3}\s*\d{1,4}[A-ZА-Я]?)\s+"
        r"(?P<terminal>[A-ZА-Я]?)\s+"
        r"(?P<gate>[A-ZА-Я]?\d{1,3}[A-ZА-Я]?(?:\d)?)\b",
        re.I,
    )
    for match in pattern.finditer(plain):
        times = _times_from_text(match.group("time"))
        gate = _clean_gate(match.group("gate"))
        if not times or not gate:
            continue
        for flight_code in _flight_codes_from_cell(match.group("flight")):
            rows.append(
                GateRow(
                    flight_code=flight_code,
                    scheduled_time=times[0],
                    actual_time=times[1] if len(times) > 1 else "",
                    terminal=match.group("terminal").upper(),
                    gate=gate,
                    destination_iata=match.group("iata").upper(),
                    source_label=source_label,
                )
            )
    return rows


def _dedupe_gate_rows(rows: list[GateRow]) -> list[GateRow]:
    result: list[GateRow] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for row in rows:
        key = (
            row.flight_code,
            row.scheduled_time,
            row.actual_time,
            row.destination_iata,
            row.terminal,
            row.gate,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _append_gate_rows_as_flights(flights: list[dict[str, Any]], rows: list[GateRow], airport: str) -> int:
    existing = {
        (
            _flight_code(flight),
            str(((flight.get("arrival") or {}).get("iata") or "")).upper(),
            _time_text((flight.get("originalTime") or {}).get("text")),
        )
        for flight in flights
    }
    existing_code_time = {
        (
            _flight_code(flight),
            _time_text((flight.get("originalTime") or {}).get("text")),
        )
        for flight in flights
    }
    added = 0
    for row in rows:
        key = (row.flight_code, row.destination_iata, row.scheduled_time)
        if key in existing:
            continue
        if (row.flight_code, row.scheduled_time) in existing_code_time:
            continue
        airline_iata, flight_number = _split_flight_code(row.flight_code)
        if not airline_iata or not flight_number:
            continue
        flights.append(
            {
                "id": f"{airport}-extra-gate-{row.flight_code}-{row.destination_iata}-{row.scheduled_time}",
                "airline": {"iata": airline_iata, "name": ""},
                "flightNumber": flight_number,
                "city": "",
                "arrival": {"iata": row.destination_iata, "flag": ""},
                "departure": {
                    "terminal": row.terminal,
                    "gate": row.gate,
                    "gateSource": row.source_label,
                    "gateMatch": "доп. табло: рейс + плановое время",
                },
                "originalTime": {"text": row.scheduled_time},
                "newTime": {"text": row.actual_time},
                "status": [{"type": "text", "text": "Scheduled"}],
            }
        )
        existing.add(key)
        existing_code_time.add((row.flight_code, row.scheduled_time))
        added += 1
    return added


def _rows_from_cells(cells: list[str], source_label: str) -> list[GateRow]:
    if len(cells) >= 8:
        destination, departure, flight, terminal, gate = cells[1], cells[2], cells[5], cells[6], cells[7]
    elif len(cells) >= 7:
        destination, departure, flight, terminal, gate = cells[0], cells[1], cells[4], cells[5], cells[6]
    else:
        return []

    if "flight" in flight.lower() or "рейс" in flight.lower():
        return []

    flight_codes = _flight_codes_from_cell(flight)
    gate = _clean_gate(gate)
    times = _times_from_text(departure)
    if not flight_codes or not gate or not times:
        return []

    iata_match = re.search(r"\(([A-Z]{3})\)", destination or "")
    return [
        GateRow(
            flight_code=flight_code,
            scheduled_time=times[0],
            actual_time=times[1] if len(times) > 1 else "",
            terminal=_clean_terminal(terminal),
            gate=gate,
            destination_iata=iata_match.group(1).upper() if iata_match else "",
            source_label=source_label,
        )
        for flight_code in flight_codes
    ]


def _apply_gate_rows(flights: list[dict[str, Any]], rows: list[GateRow]) -> tuple[int, int]:
    if not rows:
        return 0, 0

    exact: dict[tuple[str, str], GateRow] = {}
    code_rows: dict[str, list[GateRow]] = {}
    for row in rows:
        code_rows.setdefault(row.flight_code, []).append(row)
        for item_time in [row.scheduled_time, row.actual_time]:
            if item_time:
                exact.setdefault((row.flight_code, item_time), row)

    filled = 0
    conflicts = 0
    for flight in flights:
        code = _flight_code(flight)
        times = [_time_text((flight.get("originalTime") or {}).get("text")), _time_text((flight.get("newTime") or {}).get("text"))]
        row = next((exact.get((code, value)) for value in times if value and exact.get((code, value))), None)
        if not row:
            candidates = code_rows.get(code, [])
            destination_iata = str(((flight.get("arrival") or {}).get("iata") or "")).upper()
            matching = [item for item in candidates if destination_iata and item.destination_iata == destination_iata]
            if len(matching) == 1:
                row = matching[0]
            elif len(candidates) == 1:
                row = candidates[0]
        if not row:
            continue

        departure = flight.setdefault("departure", {})
        current_gate = str(departure.get("gate") or "").strip()
        row_gate, row_terminal = _split_gate_terminal(row.gate, row.terminal)
        if current_gate and current_gate.upper() != row_gate.upper():
            departure["gateConflict"] = f"{row.source_label}: {row_gate}"
            conflicts += 1
            continue

        if current_gate:
            departure["gateSource"] = _append_value(
                str(departure.get("gateSource") or "Flighty live-снимок"),
                f"подтвержден {row.source_label}",
            )
            departure["gateMatch"] = _append_value(
                str(departure.get("gateMatch") or ""),
                "рейс + время" if any(exact.get((code, value)) is row for value in times if value) else "рейс + направление",
            )
            continue

        departure["gate"] = row_gate
        if row_terminal and not departure.get("terminal"):
            departure["terminal"] = row_terminal
        departure["gateSource"] = row.source_label
        departure["gateMatch"] = "рейс + время" if any(exact.get((code, value)) is row for value in times if value) else "рейс + направление"
        flight["secondaryCorner"] = f"Gate {row_gate}"
        filled += 1

    return filled, conflicts


def _svo_periods(now: datetime) -> list[str]:
    hour = now.hour
    windows = [(0, 2), (3, 5), (6, 8), (9, 11), (12, 13), (14, 16), (17, 21), (22, 23)]
    result = []
    for start, end in windows:
        if start <= hour <= end:
            result.append(f"{start:02d}:00-{end:02d}:00")
    for start, end in windows:
        value = f"{start:02d}:00-{end:02d}:00"
        if value not in result:
            result.append(value)
    return result


def _has_missing_gates(flights: list[dict[str, Any]]) -> bool:
    return any(not _known_gate(flight) for flight in flights)


def _count_missing_gates(flights: list[dict[str, Any]]) -> int:
    return sum(1 for flight in flights if not _known_gate(flight))


def _known_gate(flight: dict[str, Any]) -> bool:
    departure = flight.get("departure") or {}
    gate = str(departure.get("gate") or "").strip()
    return bool(gate and gate.lower() not in {"не указан", "not available", "n/a", "--", "$undefined"})


def _flight_code(flight: dict[str, Any]) -> str:
    airline = flight.get("airline") or {}
    return _normalize_code(f"{airline.get('iata', '')} {flight.get('flightNumber', '')}")


def _split_flight_code(value: str) -> tuple[str, str]:
    code = _compact_flight_code(_normalize_code(value))
    if not code:
        return "", ""
    if len(code) >= 3 and _looks_airline_token(code[:2]) and code[2].isdigit():
        return code[:2], code[2:]
    if len(code) >= 4 and code[:3].isalpha() and code[3].isdigit():
        return code[:3], code[3:]
    return "", ""


def _destination_iata_from_text(value: str) -> str:
    matches = re.findall(r"\b([A-Z]{3})\b", value or "")
    if not matches:
        return ""
    return matches[-1].upper()


def _flight_codes_from_cell(value: str) -> list[str]:
    codes: list[str] = []
    tokens = re.findall(r"[A-ZА-Я0-9]+", (value or "").upper())
    index = 0
    while index < len(tokens):
        code = ""
        token = tokens[index]
        if (
            index + 1 < len(tokens)
            and _looks_airline_token(token)
            and re.fullmatch(r"\d{1,5}[A-ZА-Я]?", tokens[index + 1])
        ):
            code = f"{token}{tokens[index + 1]}"
            index += 2
        else:
            code = _compact_flight_code(token)
            index += 1
        if code and code not in codes:
            codes.append(code)
    return codes


def _compact_flight_code(token: str) -> str:
    token = _normalize_code(token)
    if len(token) >= 3 and _looks_airline_token(token[:2]) and token[2].isdigit():
        return token
    if len(token) >= 4 and token[:3].isalpha() and token[3].isdigit():
        return token
    return ""


def _looks_airline_token(token: str) -> bool:
    return len(token) in {2, 3} and token.isalnum() and any(char.isalpha() for char in token)


def _normalize_code(value: str) -> str:
    return re.sub(r"[^A-Za-zА-Яа-я0-9]", "", value or "").upper()


def _clean_gate(value: str) -> str:
    text = _plain(value).upper().replace("А", "A").replace("В", "B").replace("С", "C")
    text = re.sub(r"\s+", "", text)
    if not text or text in {"-", "N/A", "$UNDEFINED"}:
        return ""
    match = re.search(r"([A-ZА-Я]?\d{1,3}[A-ZА-Я]?(?:\d)?)", text)
    return match.group(1) if match else ""


def _clean_terminal(value: str) -> str:
    text = _plain(value).upper().strip()
    match = re.search(r"[A-ZА-Я0-9]", text)
    return match.group(0) if match else ""


def _split_gate_terminal(gate: str, terminal: str) -> tuple[str, str]:
    gate = gate.upper().strip()
    terminal = terminal.upper().strip()
    match = re.fullmatch(r"([BCD])(\d{3}[A-ZА-Я]?)", gate)
    if match:
        return match.group(2), terminal or match.group(1)
    return gate, terminal


def _times_from_text(value: str) -> list[str]:
    result = []
    for hour, minute in re.findall(r"\b(\d{1,2}):(\d{2})\b", value or ""):
        h = int(hour)
        m = int(minute)
        if h <= 23 and m <= 59:
            item = f"{h:02d}:{m:02d}"
            if item not in result:
                result.append(item)
    return result


def _time_text(value: str) -> str:
    times = _times_from_text(value or "")
    return times[0] if times else ""


def _request_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    last_error: Exception | None = None
    for context in (ssl.create_default_context(), ssl._create_unverified_context()):
        try:
            with urllib.request.urlopen(request, timeout=20, context=context) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            last_error = exc
    raise RuntimeError(f"Cannot fetch {url}: {last_error}")


def _append_value(current: str, value: str) -> str:
    current = str(current or "").strip()
    value = str(value or "").strip()
    if not value:
        return current
    if not current:
        return value
    parts = [item.strip() for item in current.split(";") if item.strip()]
    if value not in parts:
        parts.append(value)
    return "; ".join(parts)


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).replace("\xa0", " ").strip()


def _plain_lines(value: str) -> list[str]:
    text = re.sub(r"<\s*(br|/tr|/td|/th|/div|/p|/li|/h[1-6])\b[^>]*>", "\n", value or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    return [
        re.sub(r"\s+", " ", html.unescape(line).replace("\xa0", " ")).strip()
        for line in text.splitlines()
        if re.sub(r"\s+", " ", html.unescape(line).replace("\xa0", " ")).strip()
    ]
