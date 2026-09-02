"""Backend API for the Hermes Codex Limits desktop plugin.

The endpoint performs no model inference. It reuses Hermes' authenticated,
read-only Codex account-usage client and keeps an isolated cache for every
validated Hermes profile.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query

log = logging.getLogger(__name__)
router = APIRouter()


class UsageUnavailable(RuntimeError):
    """Raised when no current or previously cached usage snapshot is available."""


def _default_fetcher(provider: str):
    import httpx

    from agent.account_usage import _fetch_codex_account_usage
    from hermes_cli.auth import AuthError

    try:
        return _fetch_codex_account_usage()
    except (AuthError, httpx.HTTPError, json.JSONDecodeError) as exc:
        raise UsageUnavailable("Codex usage is temporarily unavailable") from exc
    except RuntimeError as exc:
        if str(exc) != "No available openai-codex credential in credential pool":
            raise
        raise UsageUnavailable("Codex usage is temporarily unavailable") from exc


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _serialize_snapshot(snapshot: Any) -> dict[str, Any]:
    if snapshot is None or getattr(snapshot, "unavailable_reason", None):
        raise UsageUnavailable("Codex usage is temporarily unavailable")

    windows: list[dict[str, Any]] = []
    for window in getattr(snapshot, "windows", ()) or ():
        used_raw = getattr(window, "used_percent", None)
        if used_raw is None:
            continue
        used = max(0.0, min(100.0, float(used_raw)))
        windows.append(
            {
                "label": str(getattr(window, "label", "Usage")),
                "remaining_percent": round(100.0 - used),
                "reset_at": _iso(getattr(window, "reset_at", None)),
            }
        )

    if not windows:
        raise UsageUnavailable("Codex usage is temporarily unavailable")

    details = [str(item) for item in (getattr(snapshot, "details", ()) or ())]
    banked_reset_count = 0
    for detail in details:
        match = re.search(r"\bYou have (\d+) reset", detail)
        if match:
            banked_reset_count = int(match.group(1))
            break

    return {
        "plan": getattr(snapshot, "plan", None),
        "windows": windows,
        "banked_reset_count": banked_reset_count,
        "stale": False,
    }


def _public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Enforce the documented UI-only privacy boundary at the HTTP edge."""
    windows = []
    for window in payload.get("windows", ()) or ():
        windows.append(
            {
                "label": window.get("label"),
                "remaining_percent": window.get("remaining_percent"),
                "reset_at": window.get("reset_at"),
            }
        )
    return {
        "plan": payload.get("plan"),
        "windows": windows,
        "banked_reset_count": payload.get("banked_reset_count", 0),
        "stale": bool(payload.get("stale", False)),
    }


class UsageService:
    """Thread-safe TTL cache and single-flight Codex usage refresh."""

    def __init__(
        self,
        *,
        fetcher: Callable[[str], Any] = _default_fetcher,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = 300.0,
        refresh_cooldown_seconds: float = 15.0,
    ) -> None:
        self._fetcher = fetcher
        self._clock = clock
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._refresh_cooldown_seconds = max(
            1.0, float(refresh_cooldown_seconds)
        )
        self._condition = threading.Condition()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._last_expected_attempt_completed_at: float | None = None
        self._last_expected_attempt_failed = False
        self._refreshing = False
        self._generation = 0
        self._completed_generation = 0
        self._completed_result: dict[str, Any] | None = None
        self._completed_error: Exception | None = None

    def _cached_result(self, *, stale: bool) -> dict[str, Any]:
        if self._cached is None:
            raise UsageUnavailable("Codex usage is temporarily unavailable")
        result = copy.deepcopy(self._cached)
        result["stale"] = stale
        return result

    def _finish(
        self,
        generation: int,
        *,
        result: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._completed_generation = generation
        self._completed_result = copy.deepcopy(result)
        self._completed_error = error
        self._refreshing = False
        self._condition.notify_all()

    def get(self, *, force: bool = False) -> dict[str, Any]:
        with self._condition:
            if self._refreshing:
                generation = self._generation
                while self._refreshing and generation == self._generation:
                    self._condition.wait()
                if self._completed_generation == generation:
                    if self._completed_error is not None:
                        raise self._completed_error
                    assert self._completed_result is not None
                    return copy.deepcopy(self._completed_result)

            now = self._clock()
            if (
                self._last_expected_attempt_completed_at is not None
                and now - self._last_expected_attempt_completed_at
                < self._refresh_cooldown_seconds
            ):
                return self._cached_result(
                    stale=self._last_expected_attempt_failed
                )

            if (
                not force
                and self._cached is not None
                and now - self._cached_at < self._ttl_seconds
            ):
                return self._cached_result(stale=False)

            self._refreshing = True
            self._generation += 1
            generation = self._generation

        try:
            fresh = _serialize_snapshot(self._fetcher("openai-codex"))
        except UsageUnavailable:
            log.warning("Codex usage refresh failed")
            with self._condition:
                self._last_expected_attempt_completed_at = self._clock()
                self._last_expected_attempt_failed = True
                if self._cached is None:
                    error = UsageUnavailable(
                        "Codex usage is temporarily unavailable"
                    )
                    self._finish(generation, error=error)
                    raise error from None
                stale = self._cached_result(stale=True)
                self._finish(generation, result=stale)
                return stale
        except Exception as exc:
            with self._condition:
                self._finish(generation, error=exc)
            raise

        with self._condition:
            completed_at = self._clock()
            self._cached = copy.deepcopy(fresh)
            self._cached_at = completed_at
            self._last_expected_attempt_completed_at = completed_at
            self._last_expected_attempt_failed = False
            self._finish(generation, result=fresh)
        return fresh


class _ProfileState:
    def __init__(self) -> None:
        self.service = UsageService()
        self.request_lock = asyncio.Lock()


_PROFILE_STATES: dict[str, _ProfileState] = {}
_PROFILE_STATES_LOCK = threading.Lock()


def _resolve_profile_scope(profile: str | None) -> tuple[str, Path]:
    from hermes_cli.profiles import (
        get_profile_dir,
        normalize_profile_name,
        profile_exists,
        validate_profile_name,
    )

    if not isinstance(profile, str) or not profile.strip():
        raise HTTPException(
            status_code=400,
            detail="The profile query parameter is required",
        )

    try:
        canonical = normalize_profile_name(profile)
        validate_profile_name(canonical)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not profile_exists(canonical):
        raise HTTPException(
            status_code=404,
            detail=f"Hermes profile {canonical!r} does not exist",
        )
    return canonical, get_profile_dir(canonical)


def _profile_state(profile: str) -> _ProfileState:
    with _PROFILE_STATES_LOCK:
        state = _PROFILE_STATES.get(profile)
        if state is None:
            state = _ProfileState()
            _PROFILE_STATES[profile] = state
        return state


def _scoped_get(service: UsageService, home: Path, force: bool) -> dict[str, Any]:
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    token = set_hermes_home_override(home)
    try:
        return service.get(force=force)
    finally:
        reset_hermes_home_override(token)


@router.get("/usage")
async def usage(
    force: bool = Query(
        default=False,
        description="Refresh early, subject to the 15-second cooldown",
    ),
    profile: str = Query(
        ...,
        min_length=1,
        description="Hermes profile whose Codex credentials should be used",
    ),
):
    profile_key, home = _resolve_profile_scope(profile)
    state = _profile_state(profile_key)
    try:
        async with state.request_lock:
            result = await asyncio.to_thread(
                _scoped_get, state.service, home, force
            )
        return _public_payload(result)
    except UsageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
