from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


def write_snapshot(
    data_dir: Path,
    airport: str,
    service_date: date,
    collected_at: datetime,
    source_url: str,
    flights: list[dict[str, Any]],
    meta: dict[str, Any],
) -> Path:
    day_dir = data_dir / "raw" / service_date.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    stamp = collected_at.strftime("%Y%m%d_%H%M%S")
    path = day_dir / f"{airport}_{stamp}.json"
    payload = {
        "airport": airport,
        "service_date": service_date.isoformat(),
        "collected_at": collected_at.isoformat(),
        "source_url": source_url,
        "meta": meta,
        "flights": flights,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_snapshots_around(data_dir: Path, target_date: date, radius_days: int = 1) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    raw_dir = data_dir / "raw"
    for delta in range(-radius_days, radius_days + 1):
        day = target_date + timedelta(days=delta)
        day_dir = raw_dir / day.isoformat()
        if not day_dir.exists():
            continue
        for path in sorted(day_dir.glob("*.json")):
            try:
                snapshots.append(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
    return snapshots
