"""Thin client for the CricketData API (cricketdata.org / cricapi.com).

Only the endpoints the fantasy pipeline needs are wrapped:

    GET /series          -> list/search series (auto-discover "The Hundred")
    GET /series_info     -> a series' match list
    GET /match_scorecard -> full batting/bowling scorecard for one match

Raw responses are cached under ``api_cache/`` so we do not burn the daily hit
quota re-fetching matches that have already finished. Completed-match
scorecards are cached permanently; anything still live/upcoming is always
re-fetched.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import requests

import config


class CricketDataError(RuntimeError):
    pass


class CricketDataClient:
    def __init__(self, api_key: Optional[str] = None, cache_dir: Optional[Path] = None):
        self.api_key = (api_key if api_key is not None else config.CRICKET_DATA_API_KEY).strip()
        if not self.api_key:
            raise CricketDataError(
                "No CricketData API key. Set the CRICKET_DATA_API_KEY environment "
                "variable (Streamlit secret / GitHub Actions secret)."
            )
        self.base_url = config.CRICKET_DATA_BASE_URL
        self.cache_dir = cache_dir or config.API_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.last_info: dict = {}

    # ------------------------------------------------------------------
    # low-level request
    # ------------------------------------------------------------------
    def _get(self, endpoint: str, params: dict, retries: int = 3) -> dict:
        url = f"{self.base_url}/{endpoint}"
        query = {"apikey": self.api_key, **params}

        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=query, timeout=30)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:  # network / JSON / HTTP error
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
                continue

            self.last_info = payload.get("info", {}) or {}
            status = str(payload.get("status", "")).lower()
            if status and status != "success":
                reason = payload.get("reason") or payload.get("message") or payload
                raise CricketDataError(f"API returned failure for {endpoint}: {reason}")
            return payload

        raise CricketDataError(f"Request to {endpoint} failed after {retries} tries: {last_exc}")

    # ------------------------------------------------------------------
    # caching helpers
    # ------------------------------------------------------------------
    def _cache_path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return self.cache_dir / f"{safe}.json"

    def _read_cache(self, key: str) -> Optional[dict]:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_cache(self, key: str, payload: dict) -> None:
        try:
            self._cache_path(key).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # endpoints
    # ------------------------------------------------------------------
    def search_series(self, search: str) -> list[dict]:
        """Return series matching ``search`` (case-insensitive substring)."""
        results: list[dict] = []
        seen: set[str] = set()
        # The API paginates in blocks of 25; scan a handful of pages.
        for offset in range(0, 250, 25):
            payload = self._get("series", {"offset": offset, "search": search})
            data = payload.get("data") or []
            if not data:
                break
            for row in data:
                sid = str(row.get("id", ""))
                if sid and sid not in seen:
                    seen.add(sid)
                    results.append(row)
            if len(data) < 25:
                break
        term = search.lower()
        return [r for r in results if term in str(r.get("name", "")).lower()]

    def series_info(self, series_id: str) -> dict:
        """Series details incl. ``matchList`` (always fetched fresh)."""
        payload = self._get("series_info", {"id": series_id})
        return payload.get("data") or {}

    def match_scorecard(self, match_id: str, allow_cache: bool = True) -> Optional[dict]:
        """Full scorecard for a match, or None if unavailable.

        Finished-match scorecards are cached permanently; live/upcoming matches
        are always re-fetched so points keep updating during play.
        """
        cache_key = f"scorecard_{match_id}"
        if allow_cache:
            cached = self._read_cache(cache_key)
            if cached is not None:
                return cached

        try:
            payload = self._get("match_scorecard", {"id": match_id})
        except CricketDataError:
            return None

        data = payload.get("data")
        if not data:
            return None

        # Only persist once the match is truly done, so we don't freeze a
        # half-finished scorecard.
        status = str(data.get("status", "")).lower()
        match_ended = bool(data.get("matchEnded")) or "won" in status or "draw" in status
        if match_ended:
            self._write_cache(cache_key, data)
        return data
