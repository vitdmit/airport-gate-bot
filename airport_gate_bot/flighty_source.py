from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .airport_gate_enrichment import enrich_airport_gates
from .settings import AIRPORTS, PAGE_BASE_PATH, SOURCE_BASE_URL, SOURCE_NAME


class SourceError(RuntimeError):
    """Raised when the Flighty source cannot be read."""


@dataclass(frozen=True)
class SourceSnapshot:
    airport: str
    source: str
    source_url: str
    flights: list[dict[str, Any]]
    meta: dict[str, Any]


def fetch_departures(airport: str, limit: int = 500) -> SourceSnapshot:
    airport = airport.upper()
    if airport not in AIRPORTS:
        raise SourceError(f"Unknown airport code: {airport}")

    slug = AIRPORTS[airport]["slug"]
    page_url = f"{SOURCE_BASE_URL}{PAGE_BASE_PATH}/{slug}/departures"
    html = _request_text(page_url)
    initial = _extract_initial_payload(html)
    flights = initial.get("initialFlights", [])

    action_id = _find_get_more_action_id(html)
    if action_id:
        try:
            more = _call_get_more(page_url, action_id, slug, initial.get("phase", "DEPARTURE"), 0, limit)
            if more.get("flights"):
                flights = more["flights"]
        except SourceError:
            # Keep the server-rendered first page as a usable fallback.
            pass

    extra_meta: dict[str, Any] = {}
    try:
        extra_meta = enrich_airport_gates(airport, flights)
    except Exception as exc:
        extra_meta = {f"{airport.lower()}_gate_enrichment_error": str(exc)}

    return SourceSnapshot(
        airport=airport,
        source=SOURCE_NAME,
        source_url=page_url,
        flights=flights,
        meta={
            "airport_name": AIRPORTS[airport]["name"],
            "slug": slug,
            "type": initial.get("type"),
            "phase": initial.get("phase"),
            "rows": len(flights),
            **extra_meta,
        },
    )


def _request_text(url: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> str:
    base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    }
    if headers:
        base_headers.update(headers)

    request = urllib.request.Request(url, data=data, headers=base_headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=45, context=ssl.create_default_context()) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SourceError(f"Cannot fetch {url}: {exc}") from exc


def _extract_initial_payload(html: str) -> dict[str, Any]:
    marker = "initialFlights"
    marker_index = html.find(marker)
    if marker_index < 0:
        raise SourceError("Flighty page does not contain initial flight data")

    start = html.rfind("{", 0, marker_index)
    if start < 0:
        raise SourceError("Cannot locate initial flight payload")

    depth = 0
    end = None
    for index in range(start, len(html)):
        char = html[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break

    if end is None:
        raise SourceError("Initial flight payload is incomplete")

    # Next.js embeds the JSON object inside a React stream string, so quotes are escaped.
    raw = html[start:end].replace('\\"', '"').replace("\\/", "/")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceError("Cannot parse initial flight payload") from exc


def _find_get_more_action_id(html: str) -> str | None:
    script_urls = re.findall(r'<script[^>]+src="([^"]+)"', html)
    for src in script_urls:
        url = src if src.startswith("http") else f"{SOURCE_BASE_URL}{src}"
        try:
            js = _request_text(url)
        except SourceError:
            continue
        match = re.search(r'createServerReference\)\("([0-9a-f]+)".{0,220}?"getMoreFlights"', js)
        if match:
            return match.group(1)
    return None


def _call_get_more(
    page_url: str,
    action_id: str,
    slug: str,
    phase: str,
    offset: int = 0,
    limit: int = 500,
) -> dict[str, Any]:
    body = json.dumps([slug, phase, offset, limit], separators=(",", ":")).encode("utf-8")
    response = _request_text(
        page_url,
        data=body,
        headers={
            "Accept": "text/x-component",
            "Content-Type": "text/plain;charset=UTF-8",
            "Next-Action": action_id,
            "Origin": SOURCE_BASE_URL,
            "Referer": page_url,
        },
    )
    for line in response.splitlines():
        if re.match(r"^\d+:", line) and '"flights"' in line:
            try:
                return json.loads(line.split(":", 1)[1])
            except json.JSONDecodeError as exc:
                raise SourceError("Cannot parse getMoreFlights response") from exc
    raise SourceError("getMoreFlights response did not contain flights")
