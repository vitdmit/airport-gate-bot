from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .analytics import gate_load_rows, summarize_by_gate, summarize_by_hour, summarize_gate_hour_grid


HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
SUBTLE_FILL = PatternFill("solid", fgColor="D9EAF7")
WHITE_FONT = Font(color="FFFFFF", bold=True)
BOLD_FONT = Font(bold=True)


def create_report(
    output_path: Path,
    target_date: date,
    operational: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    factual_only: bool,
    mode_label: str | None = None,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws_summary = wb.active
    ws_summary.title = "Итог сутки"
    ws_details = wb.create_sheet("Рейсы")
    ws_gate_load = wb.create_sheet("Загрузка гейтов")
    ws_gate_hours = wb.create_sheet("Гейты x часы")
    ws_hours = wb.create_sheet("Часы")
    ws_quality = wb.create_sheet("Качество")
    ws_snapshots = wb.create_sheet("Снимки")

    _write_rows(
        ws_summary,
        [
            [
                "Дата",
                "Аэропорт",
                "ВВЛ/МВЛ",
                "Терминал",
                "Гейт",
                "Кол-во рейсов",
                "Первый вылет",
                "Последний вылет",
                "Направления",
                "Рейсы по времени",
            ],
            *[
                [
                    row["date"].isoformat(),
                    row["airport"],
                    row["line_type"],
                    row["terminal"],
                    row["gate"],
                    row["flights_count"],
                    row["first_departure"],
                    row["last_departure"],
                    row["destinations"],
                    row["flight_timeline"],
                ]
                for row in summarize_by_gate(operational)
            ],
        ],
    )

    _write_rows(
        ws_details,
        [
            [
                "Дата",
                "Аэропорт",
                "ВВЛ/МВЛ",
                "Терминал",
                "Гейт",
                "Время вылета факт/текущее",
                "План",
                "Авиакомпания",
                "Рейс",
                "Направление",
                "Код",
                "Статус",
                "Кодшеринг строк",
                "Источник гейта",
                "Способ сопоставления",
                "Источник",
            ],
            *[
                [
                    row["date"].isoformat(),
                    row["airport"],
                    row["line_type"],
                    row["terminal"],
                    row["gate"],
                    row["departure_dt"].strftime("%H:%M"),
                    row["scheduled_time"],
                    row["airlines"],
                    row["flight_numbers"],
                    row["destination"],
                    row["destination_iata"],
                    row["status"],
                    row["codeshare_rows"],
                    row.get("gate_source", ""),
                    row.get("gate_match", ""),
                    row["source_url"],
                ]
                for row in operational
            ],
        ],
    )

    _write_rows(
        ws_gate_load,
        [
            [
                "Дата",
                "Аэропорт",
                "ВВЛ/МВЛ",
                "Терминал",
                "Гейт",
                "Час",
                "Время вылета",
                "Авиакомпания",
                "Рейс",
                "Направление",
                "Код направления",
                "Кодшеринг строк",
            ],
            *[
                [
                    row["date"].isoformat(),
                    row["airport"],
                    row["line_type"],
                    row["terminal"],
                    row["gate"],
                    row["hour"],
                    row["time"],
                    row["airlines"],
                    row["flight_numbers"],
                    row["destination"],
                    row["destination_iata"],
                    row["codeshare_rows"],
                ]
                for row in gate_load_rows(operational)
            ],
        ],
    )

    hour_headers = [f"{hour:02d}:00" for hour in range(24)]
    _write_rows(
        ws_gate_hours,
        [
            ["Дата", "Аэропорт", "ВВЛ/МВЛ", "Терминал", "Гейт", "Всего", *hour_headers],
            *[
                [
                    row["date"].isoformat(),
                    row["airport"],
                    row["line_type"],
                    row["terminal"],
                    row["gate"],
                    row["total"],
                    *[row["hours"][hour] for hour in range(24)],
                ]
                for row in summarize_gate_hour_grid(operational)
            ],
        ],
    )

    _write_rows(
        ws_hours,
        [
            ["Дата", "Аэропорт", "ВВЛ/МВЛ", "Час", "Кол-во рейсов"],
            *[
                [row["date"].isoformat(), row["airport"], row["line_type"], row["hour"], row["flights_count"]]
                for row in summarize_by_hour(operational)
            ],
        ],
    )

    quality_rows = _quality_rows(operational)
    _write_rows(
        ws_quality,
        [
            ["Проверка", "Аэропорт", "Значение"],
            *quality_rows,
        ],
    )

    snapshot_rows = []
    for snapshot in sorted(snapshots, key=lambda item: (item.get("service_date", ""), item.get("airport", ""), item.get("collected_at", ""))):
        snapshot_rows.append(
            [
                snapshot.get("collected_at", ""),
                snapshot.get("service_date", ""),
                snapshot.get("airport", ""),
                len(snapshot.get("flights", [])),
                snapshot.get("source_url", ""),
            ]
        )
    _write_rows(
        ws_snapshots,
        [
            ["Собрано", "Дата табло", "Аэропорт", "Строк в снимке", "Источник"],
            *snapshot_rows,
            [],
            ["Параметр", "Значение"],
            ["Дата отчета", target_date.isoformat()],
            ["Режим", mode_label or ("только фактически улетевшие" if factual_only else "предпросмотр: все не отмененные")],
            ["Сформировано", datetime.now().isoformat(timespec="seconds")],
        ],
    )

    for ws in wb.worksheets:
        _format_sheet(ws)

    wb.save(output_path)
    return output_path


def _write_rows(ws, rows: list[list[Any]]) -> None:
    for row in rows:
        ws.append(row)


def _format_sheet(ws) -> None:
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = WHITE_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    widths = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            length = min(max(len(str(cell.value)), 8), 60)
            widths[cell.column] = max(widths.get(cell.column, 0), length)
    for col_idx, width in widths.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = width + 2

    if ws.max_row == 1:
        ws.append(["Нет данных"])
        ws["A2"].font = BOLD_FONT
        ws["A2"].fill = SUBTLE_FILL


def _quality_rows(operational: list[dict[str, Any]]) -> list[list[Any]]:
    airports = sorted({row["airport"] for row in operational})
    result: list[list[Any]] = [["Всего операционных вылетов", "Все", len(operational)]]
    for airport in airports:
        rows = [row for row in operational if row["airport"] == airport]
        result.extend(
            [
                ["Всего операционных вылетов", airport, len(rows)],
                ["ВВЛ", airport, sum(1 for row in rows if row["line_type"] == "ВВЛ")],
                ["МВЛ", airport, sum(1 for row in rows if row["line_type"] == "МВЛ")],
                ["Гейт не указан источником", airport, sum(1 for row in rows if row["gate"] == "не указан")],
                ["Терминал не указан источником", airport, sum(1 for row in rows if row["terminal"] == "не указан")],
                [
                    "Гейт взят из live-снимков",
                    airport,
                    sum(1 for row in rows if str(row.get("gate_source", "")).startswith("live-снимок")),
                ],
                [
                    "Гейт подтвержден live-снимками",
                    airport,
                    sum(1 for row in rows if "live подтвержден" in str(row.get("gate_source", ""))),
                ],
                ["Схлопнутые кодшеринги", airport, sum(max((row.get("codeshare_rows") or 1) - 1, 0) for row in rows)],
            ]
        )
    return result
