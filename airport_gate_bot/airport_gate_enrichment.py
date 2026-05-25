from __future__ import annotations

import html
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .dme_source import enrich_dme_gates
from .settings import MOSCOW_TZ


SVO_TIMETABLE_URLS = (
    "https://www.svo.aero/ru/departure/timetable?date={date}&period={period}&terminal=all",
    "https://www.svo.aero/ru/timetable/departure?date={date}&period={period}&terminal=all",
    "https://www.svo.aero/en/departure/timetable?date={date}&period={period}&terminal=all",
    "https://www.svo.aero/en/timetable/departure?date={date}&period={period}&terminal=all",
    "https://www.svo.su/ru/departure/timetable?date={date}&period={period}&terminal=all",
    "https://www.svo.su/ru/timetable/departure?date={date}&period={period}&terminal=all",
    "https://www.svo.su/en/departure/timetable?date={date}&period={period}&terminal=all",
    "https://www.svo.su/en/timetable/departure?date={date}&period={period}&terminal=all",
)
VKO_ONLINE_URLS = (
    "https://www.vnukovo.ru/ru/for-passengers/flights/online/",
    "https://www.vnukovo.ru/ru/for-passengers/reysi/online-tablo/?bound=departure",
)
BACKUP_BOARD_URL = "https://www.airports-worldwide.info/airport/{airport}/departures"
AIRPORT_INFORMATION_URL = "https://www.airportinformation.com/{airport}/departures"
PLANEFINDER_DEPARTURES_URL = "https://planefinder.net/data/airport/{airport}/departures"
FIDS_LIVE_DEPARTURES_URL = "https://www.fids.live/{airport}/departures"
JINA_READER_PREFIX = "https://r.jina.ai/"
RUSSIAN_AIRPORT_IATAS = {
    "ABA", "AER", "ARH", "ASF", "BAX", "BQS", "BTK", "CEK", "CEE", "CSY",
    "DME", "EGO", "EYK", "GDX", "GOJ", "GRV", "HTA", "IAR", "IKT", "IJK",
    "IWA", "KHV", "KJA", "KLF", "KRR", "KUF", "KZN", "LED", "LPK", "MCX",
    "MMK", "MQF", "MRV", "NAL", "NBC", "NJC", "NOZ", "NUX", "NVR", "NYM",
    "OGZ", "OMS", "OVB", "PEE", "PES", "PEX", "PKC", "PKV", "PWE", "REN",
    "ROV", "RTW", "SCW", "SGC", "SIP", "SLY", "STW", "SVX", "SVO", "TBW",
    "TJM", "TOF", "UCT", "UFA", "ULV", "ULY", "URS", "USK", "VKO", "VOG",
    "VOZ", "VVO", "YKS", "ZIA",
}
CYR_UPPER = r"\u0410-\u042f\u0401"
CYR_LOWER = r"\u0430-\u044f\u0451"
UNKNOWN_GATE_VALUES = {"", "не указан", "РЅРµ СѓРєР°Р·Р°РЅ", "not available", "n/a", "--", "$undefined", "none", "null"}


@dataclass(frozen=True)
class GateRow:
    flight_code: str
    scheduled_time: str
    actual_time: str
    terminal: str
    gate: str
    destination_iata: str
    destination_name: str
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


def fetch_svo_official_gate_rows_for_date(target_date: date) -> tuple[list[GateRow], list[str]]:
    date_param = _svo_date_param(target_date)
    return _fetch_svo_official_gate_rows(date_param, periods=["allday"])


def _enrich_dme_gates(flights: list[dict[str, Any]]) -> dict[str, Any]:
    meta = enrich_dme_gates(flights)

    # Backup boards are used only to fill gates on flights we already trust
    # from Flighty. Some free boards expose noisy historical rows, so we do
    # not append them as standalone departures.
    backup_rows, backup_error = _fetch_backup_rows("DME")
    backup_filled, backup_conflicts = _apply_gate_rows(flights, backup_rows)
    meta.update({
        "dme_missing_after_backup": _count_missing_gates(flights),
        "dme_backup_rows": len(backup_rows),
        "dme_backup_filled": backup_filled,
        "dme_backup_added_missing_flights": 0,
        "dme_backup_conflicts": backup_conflicts,
        "dme_backup_error": backup_error,
    })
    return meta


def _enrich_svo_gates(flights: list[dict[str, Any]]) -> dict[str, Any]:
    missing_before = _count_missing_gates(flights)
    rows, errors = _fetch_svo_official_gate_rows(
        "today",
        periods=_svo_periods(datetime.now(ZoneInfo(MOSCOW_TZ))),
    )

    filled, conflicts = _apply_gate_rows(flights, rows)
    official_added = _append_gate_rows_as_flights(flights, rows, "SVO")
    missing_after_official = _count_missing_gates(flights)
    meta = {
        "svo_gate_enrichment_needed": int(missing_before > 0),
        "svo_missing_before": missing_before,
        "svo_missing_after_official": missing_after_official,
        "svo_official_checked": 1,
        "svo_official_rows": len(rows),
        "svo_official_filled": filled,
        "svo_official_added_missing_flights": official_added,
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
    now = datetime.now(ZoneInfo(MOSCOW_TZ))
    for url in _vko_official_urls(now):
        try:
            text = _request_text(url)
        except Exception as exc:
            errors.append(f"official VKO: {exc}")
            continue
        rows.extend(_parse_vko_official_rows(text, "official VKO"))

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

    # Backup boards are useful as gate hints, but not reliable enough to add
    # standalone flights: they can include stale rows and non-airport gates.
    backup_rows, backup_error = _fetch_backup_rows("VKO")
    backup_filled, backup_conflicts = _apply_gate_rows(flights, backup_rows)
    meta.update({
        "vko_missing_after_backup": _count_missing_gates(flights),
        "vko_backup_rows": len(backup_rows),
        "vko_backup_filled": backup_filled,
        "vko_backup_added_missing_flights": 0,
        "vko_backup_conflicts": backup_conflicts,
        "vko_backup_error": backup_error,
    })
    return meta


def _vko_official_urls(now: datetime) -> list[str]:
    urls = list(VKO_ONLINE_URLS)
    hours = {now.hour, max(now.hour - 1, 0), max(now.hour - 2, 0)}
    if now.hour <= 9:
        hours.add(23)
    for hour in sorted(hours, reverse=True):
        urls.append(
            "https://www.vnukovo.ru/ru/for-passengers/reysi/online-tablo/"
            f"?bound=departure&date=today&from={hour}"
        )
    if now.hour <= 9:
        urls.append(
            "https://www.vnukovo.ru/ru/for-passengers/reysi/online-tablo/"
            "?bound=departure&date=yesterday&from=23"
        )

    result: list[str] = []
    for url in urls:
        if url not in result:
            result.append(url)
    return result


def _fetch_backup_rows(airport: str) -> tuple[list[GateRow], str]:
    errors: list[str] = []
    rows: list[GateRow] = []
    planefinder_url = PLANEFINDER_DEPARTURES_URL.format(airport=airport)
    fids_live_url = FIDS_LIVE_DEPARTURES_URL.format(airport=airport.lower())
    airport_information_url = AIRPORT_INFORMATION_URL.format(airport=airport)
    sources = [
        (planefinder_url, f"PlaneFinder {airport}"),
        (_reader_url(planefinder_url), f"PlaneFinder {airport} via Reader"),
        (fids_live_url, f"fids.live {airport}"),
        (airport_information_url, f"airportinformation.com {airport}"),
        (BACKUP_BOARD_URL.format(airport=airport), f"airports-worldwide {airport}"),
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


def _fetch_svo_official_gate_rows(date_param: str, periods: list[str]) -> tuple[list[GateRow], list[str]]:
    rows: list[GateRow] = []
    errors: list[str] = []
    for period in periods:
        period_rows: list[GateRow] = []
        for template in SVO_TIMETABLE_URLS:
            url = template.format(date=date_param, period=period)
            try:
                text = _request_text(url)
            except Exception as exc:
                errors.append(f"official SVO {date_param} {period}: {exc}")
                text = ""
            source_rows = _parse_gate_rows(text, "official SVO") if text else []
            if not source_rows:
                try:
                    text = _request_text(_reader_url(url))
                    source_rows = _parse_gate_rows(text, "official SVO via Reader")
                except Exception as exc:
                    errors.append(f"official SVO Reader {date_param} {period}: {exc}")
            if source_rows:
                period_rows.extend(source_rows)
                break
        rows.extend(period_rows)
    return _dedupe_gate_rows(rows), errors


def _svo_date_param(target_date: date) -> str:
    today = datetime.now(ZoneInfo(MOSCOW_TZ)).date()
    if target_date == today:
        return "today"
    if target_date == today - timedelta(days=1):
        return "yesterday"
    return target_date.isoformat()


def _reader_url(url: str) -> str:
    return f"{JINA_READER_PREFIX}{url}"


def _parse_gate_rows(text: str, source_label: str) -> list[GateRow]:
    if "official svo" in source_label.lower():
        rows = _parse_svo_official_rows(text, source_label)
        if rows:
            return rows
    if "planefinder" in source_label.lower():
        return _parse_planefinder_rows(text, source_label)
    if "fids.live" in source_label.lower():
        return _parse_fids_live_rows(text, source_label)
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


def _parse_svo_official_rows(text: str, source_label: str) -> list[GateRow]:
    rows = _parse_svo_official_line_blocks(text, source_label)
    rows.extend(_parse_svo_official_compact_rows(text, source_label))
    return _dedupe_gate_rows(rows)


def _parse_svo_official_line_blocks(text: str, source_label: str) -> list[GateRow]:
    lines = _plain_lines(text)
    rows: list[GateRow] = []
    time_indexes = [idx for idx, line in enumerate(lines) if re.fullmatch(r"\d{1,2}:\d{2}", line)]
    for pos, idx in enumerate(time_indexes):
        next_idx = time_indexes[pos + 1] if pos + 1 < len(time_indexes) else len(lines)
        block = lines[idx:next_idx]
        row = _svo_row_from_block(block, source_label)
        if row:
            rows.extend(row)
    return rows


def _svo_row_from_block(block: list[str], source_label: str) -> list[GateRow]:
    if not block:
        return []
    scheduled_time = _first_time(block[:3])
    if not scheduled_time:
        return []

    terminal = ""
    gate = ""
    gate_pos = -1
    for idx, line in enumerate(block[1:], start=1):
        terminal, gate = _svo_terminal_gate(line)
        if gate:
            gate_pos = idx
            break
    if not gate:
        return []

    flight_codes: list[str] = []
    for code in _flight_codes_from_cell(" ".join(block[1:gate_pos])):
        if code not in flight_codes:
            flight_codes.append(code)
    if not flight_codes:
        return []

    destination_name = ""
    for line in block[1:gate_pos]:
        if _looks_like_svo_destination(line):
            destination_name = _destination_name_from_text(line)
            break

    return [
        GateRow(
            flight_code=flight_code,
            scheduled_time=scheduled_time,
            actual_time="",
            terminal=terminal,
            gate=gate,
            destination_iata="",
            destination_name=destination_name,
            source_label=source_label,
        )
        for flight_code in flight_codes
    ]


def _parse_svo_official_compact_rows(text: str, source_label: str) -> list[GateRow]:
    plain = _plain(text)
    rows: list[GateRow] = []
    flight_pattern = r"(?:[A-Z0-9]{2,3}\s*\d{1,5}[A-Z]?\s*){1,4}"
    pattern = re.compile(
        rf"(?P<time>\b\d{{1,2}}:\d{{2}}\b)"
        rf"(?:\s+\d{{1,2}}:\d{{2}})?"
        rf"(?:\s+\d{{1,2}}\s+[А-Яа-яЁёA-Za-z]+)?\s+"
        rf"(?P<destination>[A-Za-z{CYR_UPPER}{CYR_LOWER}][A-Za-z{CYR_UPPER}{CYR_LOWER} \-/]+?)\s+"
        rf"(?P<flights>{flight_pattern})"
        rf"(?P<terminal>[BCD])\s*(?P<gate>\d{{1,3}}[A-Z]?)\b",
        re.I,
    )
    for match in pattern.finditer(plain):
        flight_codes = _flight_codes_from_cell(match.group("flights"))
        terminal, gate = _svo_terminal_gate(f"{match.group('terminal')} {match.group('gate')}")
        if not flight_codes or not gate:
            continue
        destination_name = _destination_name_from_text(match.group("destination"))
        for flight_code in flight_codes:
            rows.append(
                GateRow(
                    flight_code=flight_code,
                    scheduled_time=_times_from_text(match.group("time"))[0],
                    actual_time="",
                    terminal=terminal,
                    gate=gate,
                    destination_iata="",
                    destination_name=destination_name,
                    source_label=source_label,
                )
            )
    return rows


def _svo_terminal_gate(value: str) -> tuple[str, str]:
    text = _plain(value).upper()
    match = re.search(rf"\b([BCD])\s*(\d{{1,3}}[A-Z{CYR_UPPER}]?)\b", text)
    if not match:
        return "", ""
    return _clean_terminal(match.group(1)), _clean_gate(match.group(2))


def _looks_like_svo_destination(value: str) -> bool:
    text = _plain(value)
    if not text:
        return False
    if re.fullmatch(r"[A-Z0-9]{1,3}", text.upper()):
        return False
    if _flight_codes_from_cell(text):
        return False
    if re.search(r"\d", text):
        return False
    lowered = text.lower()
    rejected = {
        "табло", "услуги", "схема", "парковка", "добраться", "вакансии", "меню",
        "сегодня", "вчера", "завтра", "любое время", "все терминалы",
        "поиск по номеру рейса, городу и авиакомпании",
    }
    if lowered in rejected:
        return False
    if re.search(r"\b(?:gate|terminal|flight|departure|arrival|status)\b", lowered):
        return False
    if re.search(r"\b(?:прибыл|в полете|вылетел|посадка|отменен|задержан|совершил)\b", lowered):
        return False
    return bool(re.search(rf"[A-Za-z{CYR_UPPER}{CYR_LOWER}]", text))


def _parse_vko_official_rows(text: str, source_label: str) -> list[GateRow]:
    lines = _plain_lines(text)
    rows: list[GateRow] = []
    idx = 0
    while idx < len(lines):
        if lines[idx] not in {"Время вылета", "Departure time"}:
            idx += 1
            continue

        next_idx = idx + 1
        while next_idx < len(lines) and lines[next_idx] not in {"Время вылета", "Departure time"}:
            next_idx += 1
        rows.extend(_vko_rows_from_block(lines[idx:next_idx], source_label))
        idx = next_idx
    return _dedupe_gate_rows(rows)


def _vko_rows_from_block(block: list[str], source_label: str) -> list[GateRow]:
    if len(block) < 10:
        return []

    time_pos = 0
    destination_pos = _find_label(block, {"Направление", "Destination"}, time_pos + 1)
    flight_pos = _find_label(block, {"Рейс", "Flight"}, destination_pos + 1 if destination_pos >= 0 else 1)
    airline_pos = _find_label(block, {"Авиакомпания", "Airline"}, flight_pos + 1 if flight_pos >= 0 else 1)
    terminal_pos = _find_label(block, {"Терминал", "Терм.", "Terminal", "Term."}, airline_pos + 1 if airline_pos >= 0 else 1)
    gate_pos = _find_label(block, {"Выход", "Gate"}, terminal_pos + 1 if terminal_pos >= 0 else 1)
    status_pos = _find_label(block, {"Статус", "Status"}, gate_pos + 1 if gate_pos >= 0 else 1)

    if min(destination_pos, flight_pos, terminal_pos, gate_pos) < 0:
        return []

    scheduled_time = _first_time(block[time_pos + 1:destination_pos])
    if not scheduled_time:
        return []
    actual_time = _last_time(block[time_pos + 1:destination_pos])
    if actual_time == scheduled_time:
        actual_time = ""

    destination_lines = block[destination_pos + 1:flight_pos]
    destination_iata = next((item.upper() for item in destination_lines if re.fullmatch(r"[A-Z]{3}", item)), "")
    destination_name = next((item for item in destination_lines if not re.fullmatch(r"[A-Z]{3}", item)), "")

    flight_end = airline_pos if airline_pos >= 0 else terminal_pos
    flight_codes: list[str] = []
    for line in block[flight_pos + 1:flight_end]:
        for code in _flight_codes_from_cell(line):
            if code not in flight_codes:
                flight_codes.append(code)

    terminal = _first_value(block, terminal_pos + 1, gate_pos)
    gate_end = status_pos if status_pos >= 0 else len(block)
    gate = _clean_gate(_first_value(block, gate_pos + 1, gate_end))
    if not flight_codes or not gate:
        return []

    return [
        GateRow(
            flight_code=flight_code,
            scheduled_time=scheduled_time,
            actual_time=actual_time,
            terminal=_clean_terminal(terminal),
            gate=gate,
            destination_iata=destination_iata,
            destination_name=destination_name,
            source_label=source_label,
        )
        for flight_code in flight_codes
    ]


def _find_label(lines: list[str], labels: set[str], start: int = 0) -> int:
    for idx in range(max(start, 0), len(lines)):
        if lines[idx] in labels:
            return idx
    return -1


def _first_value(lines: list[str], start: int, end: int) -> str:
    for idx in range(max(start, 0), min(end, len(lines))):
        value = str(lines[idx] or "").strip()
        if value:
            return value
    return ""


def _first_time(lines: list[str]) -> str:
    for line in lines:
        times = _times_from_text(line)
        if times:
            return times[0]
    return ""


def _last_time(lines: list[str]) -> str:
    result = ""
    for line in lines:
        times = _times_from_text(line)
        if times:
            result = times[-1]
    return result


def _parse_planefinder_rows(text: str, source_label: str) -> list[GateRow]:
    if not _planefinder_page_is_current(text):
        return []

    block_rows = _parse_planefinder_line_blocks(text, source_label)
    if block_rows:
        return _dedupe_gate_rows(block_rows)

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
        destination_name = ""
        for probe_idx in range(flight_idx + 1, min(flight_idx + 5, len(lines))):
            destination_iata = _destination_iata_from_text(lines[probe_idx])
            if not destination_name:
                destination_name = _destination_name_from_text(lines[probe_idx])
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
                    destination_name=destination_name,
                    source_label=source_label,
                )
            )
    return rows


def _parse_planefinder_line_blocks(text: str, source_label: str) -> list[GateRow]:
    lines = _plain_lines(text)
    rows: list[GateRow] = []
    time_indexes = [
        idx
        for idx, line in enumerate(lines)
        if re.fullmatch(r"\d{1,2}:\d{2}\s+(?:MSK|UTC|[A-Z]{3,4})", line)
    ]

    for pos, idx in enumerate(time_indexes):
        next_idx = time_indexes[pos + 1] if pos + 1 < len(time_indexes) else len(lines)
        block = lines[idx:next_idx]
        scheduled_time = _first_time(block[:1])
        if not scheduled_time:
            continue

        flight_codes: list[str] = []
        for line in block[1:]:
            for code in _flight_codes_from_cell(line):
                if code not in flight_codes:
                    flight_codes.append(code)
        if not flight_codes:
            continue

        terminal = ""
        gate = ""
        gate_idx = -1
        for probe_idx, line in enumerate(block[1:], start=1):
            terminal, gate = _planefinder_terminal_gate(line)
            if gate:
                gate_idx = probe_idx
                break
        if not gate:
            continue

        destination_iata = ""
        destination_name = ""
        for line in block[1:gate_idx]:
            found_iata = _destination_iata_from_text(line)
            if found_iata and not _flight_codes_from_cell(line):
                destination_iata = found_iata
                destination_name = _destination_name_from_text(line)
        actual_time = ""
        for line in block[gate_idx + 1 : gate_idx + 4]:
            times = _times_from_text(line)
            if times:
                actual_time = times[-1]
                break

        for flight_code in flight_codes:
            rows.append(
                GateRow(
                    flight_code=flight_code,
                    scheduled_time=scheduled_time,
                    actual_time=actual_time,
                    terminal=terminal,
                    gate=gate,
                    destination_iata=destination_iata,
                    destination_name=destination_name,
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
        destination_name = _destination_name_from_text(lines[idx + 1])
        destination_iata = _destination_iata_from_text(lines[idx + 1])

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
                    destination_iata=destination_iata,
                    destination_name=destination_name,
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


def _removed_invalid_terminal_gate(value: str) -> tuple[str, str]:
    text = _plain(value).upper().replace("вЂ”", "-").strip()
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
        destination_iata = _destination_iata_from_text(destination)
        destination_name = _destination_name_from_text(destination)
        for flight_code in _flight_codes_from_cell(flight_cell):
            rows.append(
                GateRow(
                    flight_code=flight_code,
                    scheduled_time=_times_from_text(line)[0],
                    actual_time="",
                    terminal="",
                    gate=gate,
                    destination_iata=destination_iata,
                    destination_name=destination_name,
                    source_label=source_label,
                )
            )
    return rows


def _parse_text_table_rows(text: str, source_label: str) -> list[GateRow]:
    rows: list[GateRow] = []
    plain = _plain(text)
    pattern = re.compile(
        rf"(?P<destination>[A-Za-z{CYR_UPPER}{CYR_LOWER} \-/]+)?\s*"
        r"\((?P<iata>[A-Z]{3})\)\s+"
        r"(?P<time>\d{1,2}:\d{2}(?:\s+\d{1,2}:\d{2})?)\s+"
        r"(?P<status>.*?)\s+"
        rf"(?P<flight>[A-Z{CYR_UPPER}0-9]{{1,3}}\s*\d{{1,4}}[A-Z{CYR_UPPER}]?)\s+"
        rf"(?P<terminal>[A-Z{CYR_UPPER}]?)\s+"
        rf"(?P<gate>[A-Z{CYR_UPPER}]?\d{{1,3}}[A-Z{CYR_UPPER}]?(?:\d)?)\b",
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
                    destination_name=_destination_name_from_text(match.group("destination") or ""),
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
            row.destination_name,
            row.terminal,
            row.gate,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _append_gate_rows_as_flights(flights: list[dict[str, Any]], rows: list[GateRow], airport: str) -> int:
    templates = _flight_templates_by_code(flights)
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
        template = templates.get(row.flight_code)
        template_airline = (template.get("airline") if template else {}) or {}
        template_arrival = (template.get("arrival") if template else {}) or {}
        destination_iata = row.destination_iata or str(template_arrival.get("iata") or "").upper()
        destination_country = _country_for_destination(destination_iata) or str(template_arrival.get("countryCode") or "")
        destination_flag = str(template_arrival.get("flag") or "")
        if destination_country == "RU" and not destination_flag:
            destination_flag = "/flag/RU.svg"
        destination_name = row.destination_name or str((template or {}).get("city") or "")
        data_quality = _extra_row_quality(row, bool(template), bool(destination_iata or destination_name))
        flights.append(
            {
                "id": f"{airport}-extra-gate-{row.flight_code}-{destination_iata}-{row.scheduled_time}",
                "airline": {"iata": airline_iata, "name": str(template_airline.get("name") or "")},
                "flightNumber": flight_number,
                "city": destination_name,
                "arrival": {"iata": destination_iata, "flag": destination_flag, "countryCode": destination_country},
                "departure": {
                    "terminal": row.terminal,
                    "gate": row.gate,
                    "gateSource": row.source_label,
                    "gateMatch": "РґРѕРї. С‚Р°Р±Р»Рѕ: СЂРµР№СЃ + РїР»Р°РЅРѕРІРѕРµ РІСЂРµРјСЏ",
                },
                "dataQuality": data_quality,
                "destinationSource": "СЂРµР·РµСЂРІРЅРѕРµ С‚Р°Р±Р»Рѕ" if row.destination_iata or row.destination_name else "Flighty РїРѕ РЅРѕРјРµСЂСѓ СЂРµР№СЃР°" if template else "",
                "originalTime": {"text": row.scheduled_time},
                "newTime": {"text": row.actual_time},
                "status": [{"type": "text", "text": "Scheduled"}],
            }
        )
        existing.add(key)
        existing_code_time.add((row.flight_code, row.scheduled_time))
        added += 1
    return added


def _flight_templates_by_code(flights: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    templates: dict[str, dict[str, Any] | None] = {}
    for flight in flights:
        code = _flight_code(flight)
        if not code:
            continue
        if code in templates:
            templates[code] = None
        else:
            templates[code] = flight
    return {code: flight for code, flight in templates.items() if flight is not None}


def _extra_row_quality(row: GateRow, reused_flighty_direction: bool, has_direction: bool) -> str:
    notes: list[str] = []
    if row.destination_iata or row.destination_name:
        notes.append("РЅР°РїСЂР°РІР»РµРЅРёРµ РїСЂРёС€Р»Рѕ РёР· СЂРµР·РµСЂРІРЅРѕРіРѕ live-С‚Р°Р±Р»Рѕ")
    elif reused_flighty_direction:
        notes.append("РЅР°РїСЂР°РІР»РµРЅРёРµ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРѕ РїРѕ РЅРѕРјРµСЂСѓ СЂРµР№СЃР° РёР· Flighty")
    elif not has_direction:
        notes.append("РЅР°РїСЂР°РІР»РµРЅРёРµ РЅРµ РїРѕРґС‚РІРµСЂР¶РґРµРЅРѕ СЂРµР·РµСЂРІРЅС‹Рј live-С‚Р°Р±Р»Рѕ")
    return "; ".join(notes)


def _rows_from_cells(cells: list[str], source_label: str) -> list[GateRow]:
    if len(cells) >= 8:
        destination, departure, flight, terminal, gate = cells[1], cells[2], cells[5], cells[6], cells[7]
    elif len(cells) >= 7:
        destination, departure, flight, terminal, gate = cells[0], cells[1], cells[4], cells[5], cells[6]
    else:
        return []

    if "flight" in flight.lower() or "СЂРµР№СЃ" in flight.lower():
        return []

    flight_codes = _flight_codes_from_cell(flight)
    gate = _clean_gate(gate)
    times = _times_from_text(departure)
    if not flight_codes or not gate or not times:
        return []

    iata_match = re.search(r"\(([A-Z]{3})\)", destination or "")
    destination_name = _destination_name_from_text(destination)
    return [
        GateRow(
            flight_code=flight_code,
            scheduled_time=times[0],
            actual_time=times[1] if len(times) > 1 else "",
            terminal=_clean_terminal(terminal),
            gate=gate,
            destination_iata=iata_match.group(1).upper() if iata_match else "",
            destination_name=destination_name,
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
        if _is_unknown_gate_value(current_gate):
            current_gate = ""
        if current_gate and current_gate.upper() != row_gate.upper():
            departure["gateConflict"] = f"{row.source_label}: {row_gate}"
            conflicts += 1
            continue

        if current_gate:
            departure["gateSource"] = _append_value(
                str(departure.get("gateSource") or "Flighty live-СЃРЅРёРјРѕРє"),
                f"РїРѕРґС‚РІРµСЂР¶РґРµРЅ {row.source_label}",
            )
            departure["gateMatch"] = _append_value(
                str(departure.get("gateMatch") or ""),
                "СЂРµР№СЃ + РІСЂРµРјСЏ" if any(exact.get((code, value)) is row for value in times if value) else "СЂРµР№СЃ + РЅР°РїСЂР°РІР»РµРЅРёРµ",
            )
            continue

        departure["gate"] = row_gate
        if row_terminal and not departure.get("terminal"):
            departure["terminal"] = row_terminal
        departure["gateSource"] = row.source_label
        departure["gateMatch"] = "СЂРµР№СЃ + РІСЂРµРјСЏ" if any(exact.get((code, value)) is row for value in times if value) else "СЂРµР№СЃ + РЅР°РїСЂР°РІР»РµРЅРёРµ"
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
    result.append("allday")
    return result


def _has_missing_gates(flights: list[dict[str, Any]]) -> bool:
    return any(not _known_gate(flight) for flight in flights)


def _count_missing_gates(flights: list[dict[str, Any]]) -> int:
    return sum(1 for flight in flights if not _known_gate(flight))


def _known_gate(flight: dict[str, Any]) -> bool:
    departure = flight.get("departure") or {}
    gate = str(departure.get("gate") or "").strip()
    return not _is_unknown_gate_value(gate)


def _is_unknown_gate_value(value: str) -> bool:
    return str(value or "").strip().lower() in UNKNOWN_GATE_VALUES


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


def _destination_name_from_text(value: str) -> str:
    text = _plain(value)
    text = re.sub(r"\(([A-Z]{3})\)", "", text)
    text = re.sub(r"\b[A-Z]{3}\b$", "", text).strip(" ,-")
    return text


def _country_for_destination(destination_iata: str) -> str:
    return "RU" if destination_iata.upper() in RUSSIAN_AIRPORT_IATAS else ""


def _flight_codes_from_cell(value: str) -> list[str]:
    codes: list[str] = []
    tokens = re.findall(rf"[A-Z{CYR_UPPER}0-9]+", (value or "").upper())
    index = 0
    while index < len(tokens):
        code = ""
        token = tokens[index]
        if (
            index + 1 < len(tokens)
            and _looks_airline_token(token)
            and re.fullmatch(rf"\d{{1,5}}[A-Z{CYR_UPPER}]?", tokens[index + 1])
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
    return re.sub(rf"[^A-Za-z{CYR_UPPER}{CYR_LOWER}0-9]", "", value or "").upper()


def _clean_gate(value: str) -> str:
    text = _plain(value).upper()
    text = text.replace("\u0410", "A").replace("\u0412", "B").replace("\u0421", "C").replace("\u0415", "E")
    text = re.sub(r"\s+", "", text)
    if not text or text in {"-", "N/A", "$UNDEFINED"}:
        return ""
    if text == "0":
        return ""
    match = re.search(rf"([A-Z{CYR_UPPER}]?\d{{1,3}}[A-Z{CYR_UPPER}]?(?:\d)?)", text)
    return match.group(1) if match else ""


def _clean_terminal(value: str) -> str:
    text = _plain(value).upper().strip()
    match = re.search(rf"[A-Z{CYR_UPPER}0-9]", text)
    return match.group(0) if match else ""


def _split_gate_terminal(gate: str, terminal: str) -> tuple[str, str]:
    gate = gate.upper().strip()
    terminal = terminal.upper().strip()
    match = re.fullmatch(rf"([BCD])(\d{{1,3}}[A-Z{CYR_UPPER}]?)", gate)
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

