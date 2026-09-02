from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MODULE_PATH = Path(__file__).parents[1] / "dashboard" / "plugin_api.py"


def load_module():
    spec = importlib.util.spec_from_file_location("codex_limits_plugin_api", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def snapshot(*, used_session=6.2, used_weekly=9.4):
    now = datetime(2026, 9, 1, 18, 33, tzinfo=timezone.utc)
    return SimpleNamespace(
        provider="openai-codex",
        plan="Team",
        fetched_at=now,
        windows=(
            SimpleNamespace(
                label="Session",
                used_percent=used_session,
                reset_at=now + timedelta(hours=4),
                detail=None,
            ),
            SimpleNamespace(
                label="Weekly",
                used_percent=used_weekly,
                reset_at=now + timedelta(days=6),
                detail=None,
            ),
        ),
        details=("You have 1 reset banked",),
        unavailable_reason=None,
    )


class ManualClock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


class UsageServiceTests(unittest.TestCase):
    def test_public_snapshot_contains_only_ui_consumed_fields(self):
        module = load_module()

        result = module._serialize_snapshot(snapshot())

        self.assertEqual(
            result,
            {
                "plan": "Team",
                "windows": [
                    {
                        "label": "Session",
                        "remaining_percent": 94,
                        "reset_at": "2026-09-01T22:33:00+00:00",
                    },
                    {
                        "label": "Weekly",
                        "remaining_percent": 91,
                        "reset_at": "2026-09-07T18:33:00+00:00",
                    },
                ],
                "banked_reset_count": 1,
                "stale": False,
            },
        )

    def test_reuses_one_upstream_result_during_ttl(self):
        module = load_module()
        calls = []
        clock = ManualClock()

        def fetcher(provider):
            calls.append(provider)
            return snapshot()

        service = module.UsageService(fetcher=fetcher, clock=clock, ttl_seconds=300)

        first = service.get()
        clock.value = 101.0
        second = service.get()

        self.assertEqual(calls, ["openai-codex"])
        self.assertFalse(first["stale"])
        self.assertFalse(second["stale"])
        self.assertEqual(second["windows"][0]["remaining_percent"], 94)

    def test_force_refresh_bypasses_ttl_after_cooldown(self):
        module = load_module()
        calls = []
        clock = ManualClock()

        def fetcher(provider):
            calls.append(provider)
            return snapshot(used_session=10 * len(calls))

        service = module.UsageService(fetcher=fetcher, clock=clock, ttl_seconds=300)

        first = service.get()
        clock.value = 116.0
        refreshed = service.get(force=True)

        self.assertEqual(calls, ["openai-codex", "openai-codex"])
        self.assertEqual(first["windows"][0]["remaining_percent"], 90)
        self.assertEqual(refreshed["windows"][0]["remaining_percent"], 80)

    def test_force_refreshes_are_throttled_from_attempt_completion(self):
        module = load_module()
        calls = []
        clock = ManualClock()

        def fetcher(provider):
            calls.append(provider)
            clock.value = 120.0
            return snapshot()

        service = module.UsageService(fetcher=fetcher, clock=clock, ttl_seconds=300)

        service.get()
        throttled = service.get(force=True)

        self.assertEqual(calls, ["openai-codex"])
        self.assertFalse(throttled["stale"])

    def test_slow_concurrent_refresh_longer_than_cooldown_is_single_flight(self):
        module = load_module()
        calls = []
        clock = ManualClock()
        started = threading.Event()
        release = threading.Event()
        results = []

        def fetcher(provider):
            calls.append(provider)
            started.set()
            release.wait(1)
            clock.value = 120.0
            return snapshot()

        service = module.UsageService(fetcher=fetcher, clock=clock)
        first = threading.Thread(target=lambda: results.append(service.get(force=True)))
        second = threading.Thread(target=lambda: results.append(service.get(force=True)))

        first.start()
        self.assertTrue(started.wait(1))
        second.start()
        time.sleep(0.02)
        release.set()
        first.join(1)
        second.join(1)

        self.assertEqual(calls, ["openai-codex"])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])

    def test_concurrent_waiter_reuses_expected_refresh_failure(self):
        module = load_module()
        calls = []
        started = threading.Event()
        release = threading.Event()
        errors = []

        def fetcher(provider):
            calls.append(provider)
            started.set()
            release.wait(1)
            return None

        def refresh(service):
            try:
                service.get(force=True)
            except module.UsageUnavailable as error:
                errors.append(str(error))

        service = module.UsageService(fetcher=fetcher, clock=lambda: 100.0)
        first = threading.Thread(target=refresh, args=(service,))
        second = threading.Thread(target=refresh, args=(service,))

        with self.assertLogs(module.log, level="WARNING"):
            first.start()
            self.assertTrue(started.wait(1))
            second.start()
            time.sleep(0.02)
            release.set()
            first.join(1)
            second.join(1)

        self.assertEqual(calls, ["openai-codex"])
        self.assertEqual(errors, [
            "Codex usage is temporarily unavailable",
            "Codex usage is temporarily unavailable",
        ])

    def test_keeps_last_good_snapshot_when_refresh_is_unavailable(self):
        module = load_module()
        outcomes = iter((snapshot(), None))
        clock = ManualClock()
        service = module.UsageService(
            fetcher=lambda provider: next(outcomes), clock=clock, ttl_seconds=300
        )
        service.get()
        clock.value = 116.0

        with self.assertLogs(module.log, level="WARNING"):
            stale = service.get(force=True)

        self.assertTrue(stale["stale"])
        self.assertEqual(stale["windows"][0]["remaining_percent"], 94)
        self.assertNotIn("cached", stale)
        self.assertNotIn("error", stale)

    def test_failed_refresh_is_shared_during_cooldown(self):
        module = load_module()
        outcomes = iter((snapshot(), None))
        calls = []
        clock = ManualClock()

        def fetcher(provider):
            calls.append(provider)
            return next(outcomes)

        service = module.UsageService(fetcher=fetcher, clock=clock, ttl_seconds=300)
        service.get()
        clock.value = 401.0
        with self.assertLogs(module.log, level="WARNING"):
            first_stale = service.get()
        clock.value = 402.0
        second_stale = service.get()

        self.assertEqual(calls, ["openai-codex", "openai-codex"])
        self.assertTrue(first_stale["stale"])
        self.assertTrue(second_stale["stale"])

    def test_repeated_programming_errors_propagate_without_poisoning_cooldown(self):
        module = load_module()
        calls = []

        def fetcher(provider):
            calls.append(provider)
            raise RuntimeError("programming defect")

        service = module.UsageService(fetcher=fetcher, clock=lambda: 100.0)

        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "programming defect"):
                service.get()
        self.assertEqual(calls, ["openai-codex", "openai-codex"])

    def test_raises_clean_error_without_any_snapshot(self):
        module = load_module()
        service = module.UsageService(fetcher=lambda provider: None, clock=lambda: 100.0)

        with self.assertLogs(module.log, level="WARNING"):
            with self.assertRaisesRegex(module.UsageUnavailable, "temporarily unavailable"):
                service.get()

    def test_default_fetcher_uses_typed_codex_fetch_not_public_wrapper(self):
        module = load_module()
        expected = snapshot()

        with patch(
            "agent.account_usage.fetch_account_usage",
            side_effect=AssertionError("public wrapper must not be used"),
        ), patch(
            "agent.account_usage._fetch_codex_account_usage", return_value=expected
        ) as typed_fetch:
            result = module._default_fetcher("openai-codex")

        self.assertIs(result, expected)
        typed_fetch.assert_called_once_with()

    def test_default_fetcher_translates_only_expected_failures(self):
        module = load_module()
        import httpx
        from hermes_cli.auth import AuthError

        expected_errors = (
            AuthError("not authenticated"),
            httpx.NetworkError("offline"),
            json.JSONDecodeError("bad json", "x", 0),
            RuntimeError("No available openai-codex credential in credential pool"),
        )
        for error in expected_errors:
            with self.subTest(error=type(error).__name__), patch(
                "agent.account_usage._fetch_codex_account_usage", side_effect=error
            ):
                with self.assertRaises(module.UsageUnavailable):
                    module._default_fetcher("openai-codex")

        with patch(
            "agent.account_usage._fetch_codex_account_usage",
            side_effect=KeyError("programming defect"),
        ):
            with self.assertRaises(KeyError):
                module._default_fetcher("openai-codex")


class UsageRouteTests(unittest.TestCase):
    @staticmethod
    def configure_profiles(module, service_type, homes=None):
        homes = homes or {
            "profile-a": Path("/tmp/profile-a"),
            "profile-b": Path("/tmp/profile-b"),
            "default": Path("/tmp/default"),
        }
        module.UsageService = service_type
        module._PROFILE_STATES.clear()
        module._resolve_profile_scope = lambda profile: (
            profile or "default",
            homes[profile or "default"],
        )

    def test_two_profiles_use_isolated_services_credentials_and_request_locks(self):
        module = load_module()
        profile_a_started = threading.Event()
        release_profile_a = threading.Event()
        instances = []

        class ProfileService:
            def __init__(self):
                instances.append(self)

            def get(self, *, force=False):
                from hermes_constants import get_hermes_home

                home = get_hermes_home()
                if home.name == "profile-a":
                    profile_a_started.set()
                    release_profile_a.wait(1)
                return {
                    "plan": home.name,
                    "windows": [],
                    "banked_reset_count": 0,
                    "stale": False,
                }

        self.configure_profiles(module, ProfileService)

        async def exercise():
            task_a = asyncio.create_task(module.usage(force=True, profile="profile-a"))
            await asyncio.to_thread(profile_a_started.wait, 1)
            result_b = await asyncio.wait_for(
                module.usage(force=True, profile="profile-b"), timeout=0.2
            )
            release_profile_a.set()
            result_a = await task_a
            return result_a, result_b

        result_a, result_b = asyncio.run(exercise())

        self.assertEqual(result_a["plan"], "profile-a")
        self.assertEqual(result_b["plan"], "profile-b")
        self.assertEqual(len(instances), 2)
        self.assertIsNot(instances[0], instances[1])

    def test_invalid_and_nonexistent_profiles_fail_before_service_creation(self):
        module = load_module()

        with self.assertRaises(module.HTTPException) as invalid:
            asyncio.run(module.usage(force=False, profile="../bad"))
        self.assertEqual(invalid.exception.status_code, 400)

        with patch("hermes_cli.profiles.profile_exists", return_value=False):
            with self.assertRaises(module.HTTPException) as missing:
                asyncio.run(
                    module.usage(
                        force=False, profile="definitely-missing-codex-limits-profile"
                    )
                )
        self.assertEqual(missing.exception.status_code, 404)

    def test_profile_is_required_and_explicit_default_is_validated(self):
        module = load_module()
        default_home = Path("/tmp/default")

        with self.assertRaises(module.HTTPException) as missing:
            module._resolve_profile_scope(None)
        self.assertEqual(missing.exception.status_code, 400)

        with patch(
            "hermes_cli.profiles.profile_exists", return_value=True
        ), patch(
            "hermes_cli.profiles.get_profile_dir", return_value=default_home
        ):
            explicit_default = module._resolve_profile_scope("default")

        self.assertEqual(explicit_default, ("default", default_home))

    def test_http_route_filters_service_payload_to_exact_privacy_contract(self):
        module = load_module()

        class FakeService:
            def get(self, *, force=False):
                return {
                    "ok": True,
                    "provider": "openai-codex",
                    "plan": "Team",
                    "fetched_at": "secret-timing",
                    "windows": [
                        {
                            "label": "Session",
                            "used_percent": 6,
                            "remaining_percent": 94,
                            "reset_at": "2026-09-01T22:33:00+00:00",
                            "detail": "private",
                        }
                    ],
                    "banked_reset_count": 1,
                    "cached": True,
                    "stale": False,
                    "error": "private",
                    "details": ["private"],
                }

        self.configure_profiles(module, FakeService)

        result = asyncio.run(module.usage(force=True, profile="profile-a"))

        self.assertEqual(
            result,
            {
                "plan": "Team",
                "windows": [
                    {
                        "label": "Session",
                        "remaining_percent": 94,
                        "reset_at": "2026-09-01T22:33:00+00:00",
                    }
                ],
                "banked_reset_count": 1,
                "stale": False,
            },
        )

    def test_http_route_maps_unavailable_to_503(self):
        module = load_module()

        class FailingService:
            def get(self, *, force=False):
                raise module.UsageUnavailable("Codex usage is temporarily unavailable")

        self.configure_profiles(module, FailingService)

        with self.assertRaises(module.HTTPException) as raised:
            asyncio.run(module.usage(force=False, profile="default"))

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "Codex usage is temporarily unavailable")


if __name__ == "__main__":
    unittest.main()
