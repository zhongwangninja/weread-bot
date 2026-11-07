"""独立每周领奖：配置隔离、严格响应处理、无重放及 CLI 边界。"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, List, Tuple
from unittest.mock import AsyncMock, patch

import httpx
import yaml

from tests.helpers import FakeHttpClient, load_weread_bot


def award(
    level_id: int = 1,
    status: int = 1,
    choice_type: int = 2,
    available: int = 1,
) -> dict:
    return {
        "awardLevelId": level_id,
        "awardStatus": status,
        "awardChoices": [
            {"choiceType": choice_type, "awardNum": 1, "canChoice": available}
        ],
    }


def weekly(*awards: dict) -> dict:
    return {"readtimeAwards": list(awards), "readdayAwards": []}


def response(payload: Any, status: int = 200) -> Tuple[httpx.Response, float]:
    request = httpx.Request("POST", "https://i.weread.qq.com/weekly/exchange")
    return httpx.Response(status, json=payload, request=request), 0.01


LOGIN = {"accessToken": "fake-access", "vid": 42}


class GainConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = load_weread_bot()
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.yaml"

    def load(self, content: str, scope: str = "gain") -> Any:
        self.path.write_text(content, encoding="utf-8")
        return self.bot.ConfigManager(str(self.path), load_scope=scope).config

    def test_defaults_are_disabled_and_book_coin(self) -> None:
        config = self.bot.ConfigManager(
            str(self.path), load_scope="gain"
        ).config
        self.assertFalse(config.gain.enabled)
        self.assertEqual(config.gain.gain_type, 2)
        self.assertEqual(config.users, [])

    def test_credentials_do_not_implicitly_enable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GAIN_REFRESH_TOKEN": "fake-refresh",
                "GAIN_DEVICE_ID": "fake-device",
            },
        ):
            config = self.load("gain:\n  gain_type: 1\n")
        self.assertFalse(config.gain.enabled)
        self.assertEqual(config.gain.refresh_token, "")

    def test_disabled_credentials_and_choice_are_not_resolved(self) -> None:
        config = self.load(
            "gain:\n  enabled: false\n  gain_type: invalid\n"
            "  refresh_token: ${MISSING_GAIN_TOKEN}\n"
            "  device_id: ${MISSING_GAIN_DEVICE}\n"
        )
        self.assertFalse(config.gain.enabled)

    def test_reading_ignores_gain_and_gain_environment(self) -> None:
        with patch.dict(os.environ, {"GAIN_ENABLED": "invalid"}):
            config = self.load(
                "gain:\n  enabled: true\n"
                "  refresh_token: ${MISSING_GAIN_TOKEN}\n",
                "reading",
            )
        self.assertFalse(config.gain.enabled)

    def test_gain_ignores_unused_reading_yaml_and_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "WEREAD_CURL_STRING": "${MISSING_CURL}",
                "READING_MODE": "invalid",
                "STARTUP_MODE": "invalid",
                "NOTIFICATION_ENABLED": "invalid",
            },
        ):
            config = self.load(
                "curl_config:\n  content: ${MISSING_CURL}\n"
                "reading:\n  mode: invalid\n"
                "  target_duration: ${MISSING_TIME}\n"
                "notification:\n  enabled: ${MISSING_NOTIFICATION}\n"
                "history:\n  file: ${MISSING_HISTORY}\n"
            )
        self.assertEqual(config.curl_content, "")
        self.assertEqual(config.reading.mode, "smart_random")

    def test_env_precedence_and_empty_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GAIN_ENABLED": "true",
                "GAIN_TYPE": "2",
                "GAIN_REFRESH_TOKEN": "fake-refresh",
                "GAIN_DEVICE_ID": "  ",
            },
        ):
            config = self.load(
                "gain:\n  enabled: false\n  gain_type: 1\n"
                "  device_id: fake-yaml-device\n"
            )
        self.assertTrue(config.gain.enabled)
        self.assertEqual(config.gain.gain_type, 2)
        self.assertEqual(config.gain.device_id, "fake-yaml-device")

    def test_env_credentials_override_unused_yaml_placeholders(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GAIN_REFRESH_TOKEN": "fake-refresh",
                "GAIN_DEVICE_ID": "fake-device",
                "GAIN_TYPE": "2",
            },
        ):
            config = self.load(
                "gain:\n  enabled: true\n"
                "  gain_type: ${MISSING_GAIN_TYPE}\n"
                "  refresh_token: ${MISSING_GAIN_TOKEN}\n"
                "  device_id: ${MISSING_GAIN_DEVICE}\n"
            )
        self.assertEqual(config.gain.refresh_token, "fake-refresh")
        self.assertEqual(config.gain.device_id, "fake-device")
        self.assertEqual(config.gain.gain_type, 2)

    def test_invalid_type_and_credentials_fail_safely(self) -> None:
        for extra, path in (
            ("gain_type: 3", "gain.gain_type"),
            ("gain_type: true", "gain.gain_type"),
            ("refresh_token: 123", "gain.refresh_token"),
            ("device_id: 123", "gain.device_id"),
        ):
            with self.subTest(path=path, extra=extra):
                with self.assertRaises(self.bot.ConfigError) as caught:
                    self.load(
                        "gain:\n  enabled: true\n"
                        "  refresh_token: fake-refresh\n"
                        "  device_id: fake-device\n  " + extra + "\n"
                    )
                self.assertIn(path, str(caught.exception))
                if path != "gain.gain_type":
                    self.assertNotIn("123", str(caught.exception))

    def test_gain_does_not_bypass_common_validation(self) -> None:
        for content, path in (
            ("network:\n  timeout: 0\n", "network.timeout"),
            ("logging:\n  format: invalid\n", "logging.format"),
            ("gain: invalid\n", "gain"),
        ):
            with self.subTest(path=path):
                with self.assertRaisesRegex(self.bot.ConfigError, path):
                    self.load(content)

    def test_reading_still_rejects_missing_reading_placeholder(self) -> None:
        with self.assertRaisesRegex(
            self.bot.ConfigError, "reading.target_duration"
        ):
            self.load(
                "reading:\n  target_duration: ${MISSING_TIME}\n", "reading"
            )

    def test_gain_config_repr_hides_credentials(self) -> None:
        config = self.bot.GainConfig(
            refresh_token="fake-refresh", device_id="fake-device"
        )
        self.assertNotIn("fake-refresh", repr(config))
        self.assertNotIn("fake-device", repr(config))


class GainFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = load_weread_bot()
        self.config = self.bot.WeReadConfig(
            gain=self.bot.GainConfig(
                enabled=True,
                refresh_token="fake-refresh",
                device_id="fake-device",
            ),
            network=self.bot.NetworkConfig(retry_times=5, rate_limit=0),
        )

    def manager(
        self,
        *script: Any,
        is_cancelled: Any = None,
    ) -> Tuple[Any, FakeHttpClient]:
        client = FakeHttpClient(raw_responses=list(script))
        manager = self.bot.GainManager(
            self.config, http_client=client, is_cancelled=is_cancelled
        )
        return manager, client

    @staticmethod
    def claims(client: FakeHttpClient) -> List[dict]:
        return [
            call
            for call in client.raw_calls
            if call["json_data"].get("isExchangeAward") == 1
        ]

    async def test_disabled_creates_no_client_or_requests(self) -> None:
        self.config.gain.enabled = False
        with patch.object(self.bot, "HttpClient") as factory:
            result = await self.bot.GainManager(self.config).run()
        factory.assert_not_called()
        self.assertEqual(result.exit_code, 1)

    async def test_query_only_has_no_claims_or_web_cookies(self) -> None:
        manager, client = self.manager(
            response(LOGIN), response(weekly(award()))
        )
        result = await manager.run(query_only=True)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(self.claims(client)), 0)
        self.assertEqual(client.close_calls, 1)
        self.assertEqual(
            client.raw_calls[1]["headers"]["accesstoken"], "fake-access"
        )
        self.assertTrue(
            all(call["cookies"] is None for call in client.raw_calls)
        )

    async def test_claim_is_confirmed_by_second_query(self) -> None:
        manager, client = self.manager(
            response(LOGIN),
            response(weekly(award(), award(2, 0), award(3, 2))),
            response({"succ": 1}),
            response(weekly(award(status=2))),
        )
        result = await manager.run()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.confirmed, [1])
        self.assertEqual(result.already_claimed, [3])
        self.assertEqual(len(self.claims(client)), 1)
        self.assertEqual(
            self.claims(client)[0]["json_data"],
            {
                "awardLevelId": 1,
                "awardChoiceType": 2,
                "isExchangeAward": 1,
                "isVisitReadGoal": 1,
                "unread": 1,
                "pf": self.bot.GAIN_PF,
            },
        )

    async def test_valid_empty_award_lists_are_success(self) -> None:
        manager, client = self.manager(response(LOGIN), response(weekly()))
        result = await manager.run()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.claims(client), [])

    async def test_card_selection_is_explicit(self) -> None:
        self.config.gain.gain_type = 1
        manager, client = self.manager(
            response(LOGIN),
            response(weekly(award(choice_type=1))),
            response({"succ": 1}),
            response(weekly(award(status=2, choice_type=1))),
        )
        result = await manager.run()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(
            self.claims(client)[0]["json_data"]["awardChoiceType"], 1
        )

    async def test_unavailable_coin_never_falls_back_to_card(self) -> None:
        for item in (
            award(choice_type=1),
            award(available=0),
            award(status=7),
        ):
            with self.subTest(item=item):
                manager, client = self.manager(
                    response(LOGIN), response(weekly(item))
                )
                result = await manager.run()
                self.assertEqual(result.skipped, [1])
                self.assertEqual(result.exit_code, 1)
                self.assertEqual(self.claims(client), [])

    async def test_unknown_choice_schema_is_queryable_but_not_claimed(
        self,
    ) -> None:
        item = award()
        item["awardChoices"] = [{"awardChoiceType": 2, "canChoice": 1}]
        for query_only in (True, False):
            manager, client = self.manager(
                response(LOGIN), response(weekly(item))
            )
            result = await manager.run(query_only=query_only)
            self.assertFalse(result.awards[0].choices_known)
            self.assertEqual(result.exit_code, 0 if query_only else 1)
            self.assertEqual(self.claims(client), [])

    async def test_query_errors_are_not_misreported_as_no_awards(self) -> None:
        for payload in (
            {},
            [],
            {"errcode": -2003},
            {"errcode": "invalid"},
            {"readtimeAwards": []},
            weekly({"awardLevelId": 1}),
            weekly(award(), award()),
        ):
            with self.subTest(payload=payload):
                manager, client = self.manager(
                    response(LOGIN), response(payload)
                )
                result = await manager.run()
                self.assertEqual(result.exit_code, 1)
                self.assertTrue(result.error)
                self.assertEqual(self.claims(client), [])

    async def test_non_json_query_fails(self) -> None:
        request = httpx.Request("POST", self.bot.GAIN_EXCHANGE_URL)
        manager, client = self.manager(
            response(LOGIN),
            (httpx.Response(200, text="not-json", request=request), 0),
        )
        result = await manager.run()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.claims(client), [])

    async def test_empty_claim_response_is_not_alone_success(self) -> None:
        manager, client = self.manager(
            response(LOGIN),
            response(weekly(award())),
            response({}),
            response(weekly(award())),
        )
        result = await manager.run()
        self.assertEqual(result.confirmed, [])
        self.assertEqual(result.unknown, [1])
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(len(self.claims(client)), 1)

    async def test_499_already_claimed_uses_response_and_rechecks(
        self,
    ) -> None:
        raw, _ = response({"errcode": -2664}, 499)
        error = httpx.HTTPStatusError(
            "fake-access", request=raw.request, response=raw
        )
        manager, client = self.manager(
            response(LOGIN),
            response(weekly(award())),
            error,
            response(weekly(award(status=2))),
        )
        with self.assertLogs(level="INFO") as logs:
            result = await manager.run()
        self.assertEqual(result.already_claimed, [1])
        self.assertEqual(result.confirmed, [])
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn("fake-access", "\n".join(logs.output))

    async def test_ordinary_business_failure_does_not_stop_next_award(
        self,
    ) -> None:
        manager, client = self.manager(
            response(LOGIN),
            response(weekly(award(), award(2))),
            response({"errcode": -3002}, 499),
            response(weekly(award(), award(2))),
            response({"succ": 1}),
            response(weekly(award(), award(2, 2))),
        )
        result = await manager.run()
        self.assertEqual(result.failed, [1])
        self.assertEqual(result.confirmed, [2])
        self.assertEqual(result.status, "partial_success")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(len(self.claims(client)), 2)

    async def test_global_auth_and_rate_limit_stop_all_followup_requests(
        self,
    ) -> None:
        for status, payload in (
            (401, {}),
            (429, {}),
            (499, {"errcode": -2013}),
            (200, {"errcode": -9999, "errmsg": "token expired fake-refresh"}),
        ):
            with self.subTest(status=status):
                manager, client = self.manager(
                    response(LOGIN),
                    response(weekly(award(), award(2))),
                    response(payload, status),
                )
                result = await manager.run()
                self.assertEqual(result.exit_code, 1)
                self.assertEqual(len(client.raw_calls), 3)
                self.assertEqual(client.close_calls, 1)
                self.assertNotIn("fake-refresh", result.error)

    async def test_timeout_then_confirmation_does_not_replay_claim(
        self,
    ) -> None:
        manager, client = self.manager(
            response(LOGIN),
            response(weekly(award())),
            httpx.ReadTimeout("fake-refresh"),
            response(weekly(award(status=2))),
        )
        result = await manager.run()
        self.assertEqual(result.confirmed, [1])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(self.claims(client)), 1)

    async def test_unconfirmed_timeout_or_missing_award_is_unknown(
        self,
    ) -> None:
        for recheck in (response(weekly()), httpx.ReadTimeout("fake-access")):
            manager, client = self.manager(
                response(LOGIN),
                response(weekly(award())),
                httpx.ReadTimeout("fake-refresh"),
                recheck,
            )
            result = await manager.run()
            self.assertEqual(result.unknown, [1])
            self.assertEqual(result.exit_code, 1)
            self.assertNotIn("fake-access", result.error)
            self.assertEqual(len(self.claims(client)), 1)

    async def test_wrong_confirmed_choice_is_not_success(self) -> None:
        claimed = award(status=2)
        claimed["awardChooseType"] = 1
        manager, _ = self.manager(
            response(LOGIN),
            response(weekly(award())),
            response({"succ": 1}),
            response(weekly(claimed)),
        )
        result = await manager.run()
        self.assertEqual(result.unknown, [1])
        self.assertEqual(result.exit_code, 1)

    async def test_cross_week_stops_before_claim(self) -> None:
        manager, client = self.manager(
            response(LOGIN), response(weekly(award()))
        )
        with patch.object(
            manager, "_week_key", side_effect=["2026-45"] * 2 + ["2026-46"]
        ):
            result = await manager.run()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.claims(client), [])

    async def test_cross_week_during_recheck_is_unknown(self) -> None:
        manager, client = self.manager(
            response(LOGIN),
            response(weekly(award())),
            response({"succ": 1}),
            response(weekly(award(status=2))),
        )
        with patch.object(
            manager, "_week_key", side_effect=["2026-45"] * 5 + ["2026-46"]
        ):
            result = await manager.run()
        self.assertEqual(result.unknown, [1])
        self.assertEqual(len(self.claims(client)), 1)

    async def test_cancelled_before_start_has_zero_requests(self) -> None:
        manager, client = self.manager(is_cancelled=lambda: True)
        result = await manager.run()
        self.assertEqual(result.exit_code, 130)
        self.assertEqual(client.raw_calls, [])
        self.assertEqual(client.close_calls, 1)

    async def test_cancel_after_claim_stops_recheck_and_closes(self) -> None:
        cancelled = False
        manager, client = self.manager(
            response(LOGIN),
            response(weekly(award())),
            response({"succ": 1}),
            is_cancelled=lambda: cancelled,
        )
        original = client.post_raw

        async def post(*args: Any, **kwargs: Any) -> Any:
            nonlocal cancelled
            value = await original(*args, **kwargs)
            if kwargs["json_data"].get("isExchangeAward") == 1:
                cancelled = True
            return value

        client.post_raw = post
        result = await manager.run()
        self.assertEqual(result.exit_code, 130)
        self.assertEqual(len(client.raw_calls), 3)
        self.assertEqual(client.close_calls, 1)

    async def test_rotated_refresh_token_is_memory_only_and_not_logged(
        self,
    ) -> None:
        manager, _ = self.manager(
            response({**LOGIN, "refreshToken": "fake-renewed"}),
            response(weekly()),
        )
        with self.assertLogs(level="INFO") as logs:
            result = await manager.run()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(manager._refresh_token, "fake-renewed")
        self.assertEqual(self.config.gain.refresh_token, "fake-refresh")
        for secret in (
            "fake-access",
            "fake-refresh",
            "fake-renewed",
            "fake-device",
        ):
            self.assertNotIn(secret, "\n".join(logs.output))

    async def test_http_wrapper_does_not_replay_or_mutate_network_config(
        self,
    ) -> None:
        calls = []

        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            if request.url.path == "/login":
                return httpx.Response(200, json=LOGIN)
            if body["isExchangeAward"] == 1:
                raise httpx.ReadTimeout("fake-refresh", request=request)
            claimed = len(calls) > 2
            return httpx.Response(
                200, json=weekly(award(status=2 if claimed else 1))
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with patch.object(self.bot.httpx, "AsyncClient", return_value=client):
            result = await self.bot.GainManager(self.config).run()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(
            sum(item.get("isExchangeAward") == 1 for item in calls), 1
        )
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.config.network.retry_times, 5)
        self.assertTrue(client.is_closed)


class GainSendGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = load_weread_bot()
        self.config = self.bot.WeReadConfig(
            gain=self.bot.GainConfig(
                enabled=True,
                refresh_token="fake-refresh",
                device_id="fake-device",
            )
        )
        self.calls = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        data = LOGIN if request.url.path == "/login" else weekly(award())
        return httpx.Response(200, json=data)

    async def test_cancel_flag_during_rate_wait_prevents_claim(self) -> None:
        cancelled = False
        attempts = 0

        async def acquire() -> None:
            nonlocal cancelled, attempts
            attempts += 1
            if attempts == 3:
                cancelled = True

        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        with (
            patch.object(self.bot.httpx, "AsyncClient", return_value=client),
            patch.object(self.bot.RateLimiter, "acquire", side_effect=acquire),
        ):
            result = await self.bot.GainManager(
                self.config, is_cancelled=lambda: cancelled
            ).run()
        self.assertEqual(result.exit_code, 130)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(client.is_closed)

    async def test_cross_week_during_rate_wait_prevents_claim(self) -> None:
        week = "2026-45"
        attempts = 0

        async def acquire() -> None:
            nonlocal week, attempts
            attempts += 1
            if attempts == 3:
                week = "2026-46"

        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        manager = self.bot.GainManager(self.config)
        with (
            patch.object(self.bot.httpx, "AsyncClient", return_value=client),
            patch.object(self.bot.RateLimiter, "acquire", side_effect=acquire),
            patch.object(manager, "_week_key", side_effect=lambda: week),
        ):
            result = await manager.run()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.skipped, [1])
        self.assertEqual(result.unknown, [])
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(client.is_closed)

    def test_claim_window_reserves_network_timeout_budget(self) -> None:
        now = self.bot.datetime(
            2026, 11, 8, 23, 59, tzinfo=self.bot.ZoneInfo("Asia/Shanghai")
        )
        manager = self.bot.GainManager(self.config)
        with (
            patch.object(self.bot, "datetime") as clock,
            patch.object(manager, "_week_key", return_value="2026-45"),
        ):
            clock.now.return_value = now
            with self.assertRaises(self.bot.GainError) as caught:
                manager._check_claim_window("2026-45")
        self.assertEqual(caught.exception.kind, "not_sent")

    async def test_signals_interrupt_rate_wait_without_sending_claim(
        self,
    ) -> None:
        for signum in (self.bot.signal.SIGINT, self.bot.signal.SIGTERM):
            with self.subTest(signum=signum):
                self.calls.clear()
                handlers = {}
                attempts = 0

                def install(sig: int, handler: Any) -> str:
                    handlers[sig] = handler
                    return "previous"

                async def acquire() -> None:
                    nonlocal attempts
                    attempts += 1
                    if attempts == 3:
                        handlers[signum](signum, None)
                        await asyncio.sleep(0)

                client = httpx.AsyncClient(
                    transport=httpx.MockTransport(self.handle)
                )
                with (
                    patch.object(
                        self.bot.httpx, "AsyncClient", return_value=client
                    ),
                    patch.object(
                        self.bot.RateLimiter, "acquire", side_effect=acquire
                    ),
                    patch.object(
                        self.bot.signal, "signal", side_effect=install
                    ),
                ):
                    task = asyncio.create_task(self.bot.run_gain(self.config))
                    self.assertEqual(await task, 130)
                self.assertEqual(len(self.calls), 2)
                self.assertTrue(client.is_closed)


class GainCliTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = load_weread_bot()

    def test_conflicting_cli_modes_are_rejected(self) -> None:
        for flags in (
            ["--gain-only", "--gain-query"],
            ["--gain-only", "--dry-run"],
            ["--gain-query", "--validate-config"],
            ["--gain-only", "--show-last-run"],
            ["--gain-only", "--mode", "immediate"],
        ):
            with self.subTest(flags=flags), patch("sys.stderr"):
                with self.assertRaises(SystemExit) as caught:
                    self.bot.parse_arguments(flags)
                self.assertEqual(caught.exception.code, 2)

    async def test_main_gain_bypasses_web_and_notification_dependencies(
        self,
    ) -> None:
        for flag in ("--gain-query", "--gain-only"):
            args = self.bot.parse_arguments([flag, "--config", "unused.yaml"])
            config = self.bot.WeReadConfig()
            with (
                patch.object(self.bot, "parse_arguments", return_value=args),
                patch.object(self.bot, "ConfigManager") as factory,
                patch.object(self.bot, "setup_logging"),
                patch.object(
                    self.bot, "run_gain", new=AsyncMock(return_value=1)
                ) as run,
                patch.object(self.bot, "_validate_runtime_config") as validate,
                patch.object(
                    self.bot, "_validate_curl_configs", new=AsyncMock()
                ) as curl,
                patch.object(self.bot, "WeReadApplication") as web,
                patch.object(self.bot, "NotificationService") as notify,
                patch.object(self.bot, "persist_run_history") as history,
                patch.object(self.bot, "requests", None),
                patch.object(self.bot, "croniter", None),
            ):
                factory.return_value.config = config
                self.assertEqual(await self.bot.main(), 1)
            factory.assert_called_once_with("unused.yaml", load_scope="gain")
            run.assert_awaited_once_with(
                config, query_only=flag == "--gain-query"
            )
            validate.assert_not_called()
            curl.assert_not_awaited()
            web.assert_not_called()
            notify.assert_not_called()
            history.assert_not_called()

    async def test_runner_restores_signal_handlers_and_maps_exit_code(
        self,
    ) -> None:
        config = self.bot.WeReadConfig()
        result = self.bot.GainResult(status="cancelled")
        with (
            patch.object(self.bot, "GainManager") as factory,
            patch.object(
                self.bot.signal, "signal", return_value="previous"
            ) as signals,
        ):
            factory.return_value.run = AsyncMock(return_value=result)
            self.assertEqual(
                await self.bot.run_gain(config, query_only=True), 130
            )
        self.assertEqual(signals.call_count, 4)
        self.assertEqual(
            [call.args[1] for call in signals.call_args_list[2:]],
            ["previous", "previous"],
        )

    def test_new_sensitive_fields_are_redacted_in_text_and_structured_logs(
        self,
    ) -> None:
        for key in (
            "refreshToken",
            "refresh_token",
            "accessToken",
            "accesstoken",
            "deviceId",
            "device_id",
            "vid",
        ):
            with self.subTest(key=key):
                self.assertNotIn(
                    "fake-sensitive",
                    str(self.bot.redact_for_log({key: "fake-sensitive"})),
                )
                self.assertNotIn(
                    "fake-sensitive",
                    self.bot.redact_for_log(f'{key}="fake-sensitive"'),
                )


class GainWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/weekly-gain.yml"
        )
        # BaseLoader 保留 GitHub 的 on 键，不按 YAML 1.1 解析成布尔值。
        self.workflow = yaml.load(
            path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
        )

    def test_workflow_is_manual_and_default_disabled(self) -> None:
        self.assertEqual(set(self.workflow["on"]), {"workflow_dispatch"})
        inputs = self.workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["enable_once"]["type"], "boolean")
        self.assertEqual(inputs["enable_once"]["default"], "false")
        self.assertEqual(inputs["operation"]["default"], "query")
        self.assertEqual(inputs["reward_type"]["default"], "default")
        job = self.workflow["jobs"]["gain"]
        enabled = (
            "${{ vars.GAIN_ENABLED == 'true' || "
            "(github.event_name == 'workflow_dispatch' "
            "&& inputs.enable_once == true) }}"
        )
        self.assertEqual(job["if"], enabled)
        runtime = next(
            step
            for step in job["steps"]
            if "GAIN_ENABLED" in step.get("env", {})
        )
        self.assertEqual(runtime["env"]["GAIN_ENABLED"], enabled)
        self.assertEqual(job["timeout-minutes"], "10")

    def test_same_account_is_serialized_without_cancelling_running_job(
        self,
    ) -> None:
        self.assertEqual(
            self.workflow["concurrency"],
            {"group": "weread-weekly-gain", "cancel-in-progress": "false"},
        )
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})

    def test_secrets_are_only_in_runtime_step_and_only_safe_logs_uploaded(
        self,
    ) -> None:
        steps = self.workflow["jobs"]["gain"]["steps"]
        runtime = next(
            step
            for step in steps
            if "GAIN_REFRESH_TOKEN" in step.get("env", {})
        )
        self.assertEqual(
            runtime["env"]["GAIN_REFRESH_TOKEN"],
            "${{ secrets.GAIN_REFRESH_TOKEN }}",
        )
        self.assertEqual(
            runtime["env"]["GAIN_DEVICE_ID"], "${{ secrets.GAIN_DEVICE_ID }}"
        )
        self.assertIn("--gain-query", runtime["run"])
        self.assertIn("--gain-only", runtime["run"])
        self.assertNotIn("secrets.", runtime["run"])
        self.assertNotIn("WEREAD_CURL", str(self.workflow))
        self.assertNotIn("continue-on-error", runtime)
        for step in steps:
            if step is not runtime:
                self.assertNotIn("GAIN_REFRESH_TOKEN", step.get("env", {}))
        artifact = next(
            step for step in steps if "upload-artifact" in step.get("uses", "")
        )
        self.assertEqual(artifact["if"], "always()")
        self.assertEqual(artifact["with"]["path"], "logs/weekly-gain.log")


if __name__ == "__main__":
    unittest.main()
