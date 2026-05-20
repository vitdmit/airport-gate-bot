from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .analytics import build_operational_flights, latest_records_from_snapshots
from .flighty_source import SourceError, fetch_departures
from .gate_history import build_daily_rows
from .historical import fetch_historical_operational_rows
from .report import create_report
from .settings import AIRPORTS, MOSCOW_TZ
from .storage import load_snapshots_around, write_snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Moscow airport gate data and build daily reports.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect", help="Collect current departure-board snapshots.")
    collect_parser.add_argument("--airports", default=",".join(AIRPORTS.keys()))
    collect_parser.add_argument("--data-dir", default="data")

    report_parser = subparsers.add_parser("report", help="Build an Excel report from collected snapshots.")
    report_parser.add_argument("--date", default="yesterday", help="YYYY-MM-DD, today, or yesterday in Moscow time.")
    report_parser.add_argument("--data-dir", default="data")
    report_parser.add_argument("--output", default="")
    report_parser.add_argument("--preview", action="store_true", help="Include all non-cancelled rows, not only departed rows.")

    all_parser = subparsers.add_parser("all", help="Collect snapshots and build a report.")
    all_parser.add_argument("--airports", default=",".join(AIRPORTS.keys()))
    all_parser.add_argument("--date", default="today")
    all_parser.add_argument("--data-dir", default="data")
    all_parser.add_argument("--output", default="")
    all_parser.add_argument("--preview", action="store_true")

    history_parser = subparsers.add_parser("history-report", help="Build a completed-day report from historical FlightStats data.")
    history_parser.add_argument("--date", required=True, help="YYYY-MM-DD, today, or yesterday in Moscow time.")
    history_parser.add_argument("--airports", default=",".join(AIRPORTS.keys()))
    history_parser.add_argument("--output", default="")

    daily_parser = subparsers.add_parser(
        "daily-report",
        help="Build the recommended free daily report from collected live snapshots.",
    )
    daily_parser.add_argument("--date", default="yesterday", help="YYYY-MM-DD, today, or yesterday in Moscow time.")
    daily_parser.add_argument("--airports", default=",".join(AIRPORTS.keys()))
    daily_parser.add_argument("--data-dir", default="data")
    daily_parser.add_argument("--output", default="")

    verified_parser = subparsers.add_parser(
        "verified-report",
        help="Build a slower report: historical factual departures plus gates from collected live snapshots.",
    )
    verified_parser.add_argument("--date", default="yesterday", help="YYYY-MM-DD, today, or yesterday in Moscow time.")
    verified_parser.add_argument("--airports", default=",".join(AIRPORTS.keys()))
    verified_parser.add_argument("--data-dir", default="data")
    verified_parser.add_argument("--output", default="")

    args = parser.parse_args()
    if args.command == "collect":
        collect(args.airports, Path(args.data_dir))
    elif args.command == "report":
        report(_parse_report_date(args.date), Path(args.data_dir), args.output, preview=args.preview)
    elif args.command == "all":
        collect(args.airports, Path(args.data_dir))
        report(_parse_report_date(args.date), Path(args.data_dir), args.output, preview=args.preview)
    elif args.command == "history-report":
        history_report(_parse_report_date(args.date), _airport_list(args.airports), args.output)
    elif args.command == "daily-report":
        daily_report(_parse_report_date(args.date), _airport_list(args.airports), Path(args.data_dir), args.output)
    elif args.command == "verified-report":
        verified_report(_parse_report_date(args.date), _airport_list(args.airports), Path(args.data_dir), args.output)


def collect(airports_csv: str, data_dir: Path) -> list[Path]:
    tz = ZoneInfo(MOSCOW_TZ)
    collected_at = datetime.now(tz).replace(microsecond=0)
    service_date = collected_at.date()
    paths: list[Path] = []
    for airport in _airport_list(airports_csv):
        try:
            snapshot = fetch_departures(airport)
        except SourceError as exc:
            print(f"{airport}: source error: {exc}")
            continue
        path = write_snapshot(
            data_dir=data_dir,
            airport=airport,
            service_date=service_date,
            collected_at=collected_at,
            source_url=snapshot.source_url,
            flights=snapshot.flights,
            meta=snapshot.meta,
        )
        paths.append(path)
        print(f"{airport}: saved {len(snapshot.flights)} rows to {path}")
    return paths


def report(target_date: date, data_dir: Path, output: str = "", preview: bool = False) -> Path:
    snapshots = load_snapshots_around(data_dir, target_date)
    records = latest_records_from_snapshots(snapshots)
    operational = build_operational_flights(records, target_date, factual_only=not preview)
    output_path = Path(output) if output else Path("outputs") / f"gate_report_{target_date.isoformat()}.xlsx"
    create_report(output_path, target_date, operational, snapshots, factual_only=not preview)
    print(f"Report rows: {len(operational)}")
    print(f"Saved report: {output_path}")
    return output_path


def history_report(target_date: date, airports: list[str], output: str = "") -> Path:
    operational = fetch_historical_operational_rows(target_date, airports)
    output_path = Path(output) if output else Path("outputs") / f"gate_report_{target_date.isoformat()}_history.xlsx"
    create_report(output_path, target_date, operational, snapshots=[], factual_only=True)
    print(f"Historical report rows: {len(operational)}")
    print(f"Saved report: {output_path}")
    return output_path


def daily_report(target_date: date, airports: list[str], data_dir: Path, output: str = "") -> Path:
    snapshots = [item for item in load_snapshots_around(data_dir, target_date) if item.get("airport") in airports]
    records = latest_records_from_snapshots(snapshots)
    operational = build_operational_flights(records, target_date, factual_only=False)
    for row in operational:
        if row["gate"] == "не указан":
            row["gate_source"] = "не найден в live-снимке"
            row["gate_match"] = ""
        else:
            row["gate_source"] = "live-снимок"
            row["gate_match"] = "собран в течение дня"
    output_path = Path(output) if output else Path("outputs") / f"gate_report_{target_date.isoformat()}_daily.xlsx"
    create_report(
        output_path,
        target_date,
        operational,
        snapshots=snapshots,
        factual_only=False,
        mode_label="бесплатный режим: отчет из накопленных live-снимков",
    )
    missing_gates = sum(1 for row in operational if row["gate"] == "не указан")
    print(f"Daily report rows: {len(operational)}")
    print(f"Rows without gate: {missing_gates}")
    print(f"Saved report: {output_path}")
    return output_path


def verified_report(target_date: date, airports: list[str], data_dir: Path, output: str = "") -> Path:
    operational, snapshots = build_daily_rows(target_date, airports, data_dir)
    output_path = Path(output) if output else Path("outputs") / f"gate_report_{target_date.isoformat()}_verified.xlsx"
    create_report(
        output_path,
        target_date,
        operational,
        snapshots=snapshots,
        factual_only=True,
        mode_label="медленная сверка: фактические вылеты + gate из live-снимков",
    )
    missing_gates = sum(1 for row in operational if row["gate"] == "не указан")
    snapshot_gates = sum(1 for row in operational if str(row.get("gate_source", "")).startswith("live-снимок"))
    print(f"Verified report rows: {len(operational)}")
    print(f"Gates filled from live snapshots: {snapshot_gates}")
    print(f"Rows without gate: {missing_gates}")
    print(f"Saved report: {output_path}")
    return output_path


def _airport_list(value: str) -> list[str]:
    airports = [item.strip().upper() for item in value.split(",") if item.strip()]
    unknown = [airport for airport in airports if airport not in AIRPORTS]
    if unknown:
        raise SystemExit(f"Unknown airport code(s): {', '.join(unknown)}")
    return airports


def _parse_report_date(value: str) -> date:
    tz = ZoneInfo(MOSCOW_TZ)
    today = datetime.now(tz).date()
    normalized = value.strip().lower()
    if normalized == "today":
        return today
    if normalized == "yesterday":
        return today - timedelta(days=1)
    return date.fromisoformat(value)
