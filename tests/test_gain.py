"""独立每周领奖：配置隔离、严格响应处理、无重放及 CLI 边界。"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple
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


def signed_login_fields() -> dict:
    return {
        "random": 7,
        "signature": "0" * 64,
        "timestamp": 1_700_000_000_000,
    }


def signed_login_environment() -> dict:
    return {"GAIN_LOGIN_DATA": json.dumps(signed_login_fields())}


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
        self.assertEqual(config.gain.login_data, {})
        self.assertEqual(config.users, [])

    def test_credentials_do_not_implicitly_enable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GAIN_REFRESH_TOKEN": "fake-refresh",
                "GAIN_DEVICE_ID": "fake-device",
                **signed_login_environment(),
            },
        ):
            config = self.load("gain:\n  gain_type: 1\n")
        self.assertFalse(config.gain.enabled)
        self.assertEqual(config.gain.refresh_token, "")
        self.assertEqual(config.gain.login_data, {})

    def test_disabled_credentials_and_choice_are_not_resolved(self) -> None:
        config = self.load(
            "gain:\n  enabled: false\n  gain_type: invalid\n"
            "  refresh_token: ${MISSING_GAIN_TOKEN}\n"
            "  device_id: ${MISSING_GAIN_DEVICE}\n"
            "  login_data: ${MISSING_GAIN_LOGIN_DATA}\n"
        )
        self.assertFalse(config.gain.enabled)

    def test_reading_ignores_gain_and_gain_environment(self) -> None:
        with patch.dict(os.environ, {
            "GAIN_ENABLED": "invalid", "GAIN_LOGIN_DATA": "invalid"
        }):
            config = self.load(
                "gain:\n  enabled: true\n"
                "  refresh_token: ${MISSING_GAIN_TOKEN}\n"
                "  login_data: ${MISSING_GAIN_LOGIN_DATA}\n",
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
                **signed_login_environment(),
            },
        ):
            config = self.load(
                "gain:\n  enabled: true\n"
                "  gain_type: ${MISSING_GAIN_TYPE}\n"
                "  refresh_token: ${MISSING_GAIN_TOKEN}\n"
                "  device_id: ${MISSING_GAIN_DEVICE}\n"
                "  login_data: ${MISSING_GAIN_LOGIN_DATA}\n"
            )
        self.assertEqual(config.gain.refresh_token, "fake-refresh")
        self.assertEqual(config.gain.device_id, "fake-device")
        self.assertEqual(config.gain.gain_type, 2)
        self.assertEqual(config.gain.login_data, signed_login_fields())

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

    def test_login_data_loads_yaml_and_normalizes_integers(self) -> None:
        data = signed_login_fields()
        data["random"] = str(data["random"])
        data["timestamp"] = str(data["timestamp"])
        with patch.dict(os.environ, {"GAIN_LOGIN_DATA": "  "}):
            config = self.load(yaml.safe_dump({
                "gain": {
                    "enabled": True,
                    "refresh_token": "fake-refresh",
                    "device_id": "fake-device",
                    "login_data": data,
                },
            }))
        self.assertEqual(config.gain.login_data, signed_login_fields())

    def test_multiline_json_login_data_is_accepted(self) -> None:
        with patch.dict(os.environ, {
            "GAIN_ENABLED": "true",
            "GAIN_REFRESH_TOKEN": "fake-refresh",
            "GAIN_DEVICE_ID": "fake-device",
            "GAIN_LOGIN_DATA": json.dumps(signed_login_fields(), indent=2),
        }):
            config = self.bot.ConfigManager(
                str(self.path), load_scope="gain"
            ).config
        self.assertEqual(config.gain.login_data, signed_login_fields())

    def test_invalid_login_data_fails_without_exposing_values(self) -> None:
        valid = signed_login_fields()
        cases = (
            '{"signature": "fake-private-signature"',
            [],
            None,
            {"signature": valid["signature"]},
            {"random": 7, "signature": valid["signature"]},
            {**valid, "deviceId": "fake-private-device"},
            {**valid, "signature": "fake-private-signature"},
            {**valid, "random": True},
            {**valid, "random": -1},
            {**valid, "timestamp": True},
            {**valid, "timestamp": 0},
        )
        for index, data in enumerate(cases):
            with self.subTest(case=index), patch.dict(os.environ, {
                "GAIN_ENABLED": "true",
                "GAIN_REFRESH_TOKEN": "fake-refresh",
                "GAIN_DEVICE_ID": "fake-device",
                "GAIN_LOGIN_DATA": (
                    data if isinstance(data, str) else json.dumps(data)
                ),
            }):
                with self.assertRaises(self.bot.ConfigError) as caught:
                    self.bot.ConfigManager(str(self.path), load_scope="gain")
                message = str(caught.exception)
                self.assertIn("gain.login_data", message)
                self.assertNotIn(valid["signature"], message)
                self.assertNotIn("fake-private-signature", message)
                self.assertNotIn("fake-private-device", message)

    def test_gain_config_repr_hides_credentials(self) -> None:
        config = self.bot.GainConfig(
            refresh_token="fake-refresh", device_id="fake-device",
            login_data=signed_login_fields(),
        )
        self.assertNotIn("fake-refresh", repr(config))
        self.assertNotIn("fake-device", repr(config))
        self.assertNotIn(signed_login_fields()["signature"], repr(config))


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

    async def test_unsigned_login_uses_static_fields_and_current_timestamp(
        self,
    ) -> None:
        manager, client = self.manager(response(LOGIN), response(weekly()))
        with patch.object(self.bot.time, "time", return_value=1_700_000_000.125):
            with self.assertLogs(level="WARNING") as captured:
                result = await manager.run(query_only=True)
        self.assertEqual(result.exit_code, 0)
        body = client.raw_calls[0]["json_data"]
        self.assertEqual(body["deviceId"], "fake-device")
        self.assertEqual(body["refreshToken"], "fake-refresh")
        self.assertEqual(body["wxToken"], 0)
        self.assertEqual(body["timestamp"], 1_700_000_000_125)
        for name in ("refCgi", "trackId", "virtualChannelId"):
            self.assertEqual(body[name], "")
        self.assertEqual(len(body), 10)
        self.assertNotIn("random", body)
        self.assertNotIn("signature", body)
        self.assertIn("未配置手机端签名数据", "\n".join(captured.output))

    async def test_signed_login_fields_are_preserved_and_only_sent_to_login(
        self,
    ) -> None:
        fields = signed_login_fields()
        self.config.gain.login_data = fields
        item = award(level_id=13)
        item["awardChoices"] = [
            {"choiceType": 1, "awardNum": 2, "canChoice": 1},
            {"choiceType": 2, "awardNum": 6, "canChoice": 1},
        ]
        claimed = {**item, "awardStatus": 2, "awardChooseType": 2}
        manager, client = self.manager(
            response(LOGIN), response(weekly(item)),
            response({"succ": 1}), response(weekly(claimed)),
        )
        result = await manager.run()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.confirmed, [13])
        body = client.raw_calls[0]["json_data"]
        self.assertEqual(len(body), 12)
        for name, value in fields.items():
            self.assertEqual(body[name], value)
            self.assertEqual(self.config.gain.login_data[name], value)
        for call in client.raw_calls[1:]:
            self.assertTrue(
                set(signed_login_fields()).isdisjoint(call["json_data"])
            )
        self.assertEqual(
            self.claims(client)[0]["json_data"]["awardChoiceType"], 2
        )

    async def test_external_signature_requires_original_timestamp(self) -> None:
        self.config.gain.login_data = {
            "random": 7, "signature": signed_login_fields()["signature"]
        }
        manager, client = self.manager()
        result = await manager.run(query_only=True)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("gain.login_data", result.error)
        self.assertIn("timestamp", result.error)
        self.assertNotIn(signed_login_fields()["signature"], result.error)
        self.assertEqual(client.raw_calls, [])

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
        self.root = Path(__file__).resolve().parents[1]
        path = self.root / ".github/workflows/weekly-gain.yml"
        # BaseLoader 保留 GitHub 的 on 键，不按 YAML 1.1 解析成布尔值。
        self.workflow = yaml.load(
            path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
        )
        self.steps = self.workflow["jobs"]["gain"]["steps"]
        self.runtime = next(
            step for step in self.steps if step.get("id") == "claim"
        )
        self.notification = next(
            step
            for step in self.steps
            if step["name"] == "Send Apprise notification"
        )

    def test_workflow_defaults_to_enabled_claim_without_debug_inputs(
        self,
    ) -> None:
        self.assertEqual(
            set(self.workflow["on"]), {"workflow_dispatch", "schedule"}
        )
        self.assertEqual(self.workflow["on"]["workflow_dispatch"], {})
        job = self.workflow["jobs"]["gain"]
        self.assertEqual(job["if"], "${{ vars.GAIN_ENABLED != 'false' }}")
        self.assertEqual(
            self.runtime["env"]["GAIN_ENABLED"],
            "${{ vars.GAIN_ENABLED || 'true' }}",
        )
        self.assertEqual(
            self.runtime["env"]["GAIN_TYPE"], "${{ vars.GAIN_TYPE || '2' }}"
        )
        self.assertIn("--gain-only", self.runtime["run"])
        self.assertNotIn("--gain-query", self.runtime["run"])
        self.assertNotIn("inputs.", str(self.workflow))
        self.assertEqual(job["timeout-minutes"], "10")

    def test_schedule_is_sunday_three_in_beijing(self) -> None:
        crons = [entry["cron"] for entry in self.workflow["on"]["schedule"]]
        self.assertEqual(crons, ["0 19 * * 6"])
        minute, utc_hour, _, _, weekday = crons[0].split()
        utc = datetime(
            2026,
            11,
            7,
            int(utc_hour),
            int(minute),
            tzinfo=timezone.utc,
        )
        self.assertEqual(utc.isoweekday() % 7, int(weekday))
        local = utc.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(local.isoweekday(), 7)
        self.assertEqual((local.hour, local.minute), (3, 0))

    def test_reading_schedule_is_daily_one_in_beijing(self) -> None:
        path = self.root / ".github/workflows/auto-reading-scheduled.yml"
        reading = yaml.load(
            path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
        )
        self.assertEqual(reading["on"]["schedule"], [{"cron": "0 17 * * *"}])
        utc = datetime(2026, 11, 7, 17, tzinfo=timezone.utc)
        local = utc.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual((local.day, local.hour, local.minute), (8, 1, 0))

    def test_checkout_tracks_latest_development_branch(self) -> None:
        checkout = next(
            step for step in self.steps if "checkout" in step.get("uses", "")
        )
        self.assertEqual(
            checkout["with"]["ref"], "feat/standalone-weekly-gain"
        )
        self.assertEqual(checkout["with"]["persist-credentials"], "false")

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
        self.assertEqual(
            self.runtime["env"]["GAIN_REFRESH_TOKEN"],
            "${{ secrets.GAIN_REFRESH_TOKEN }}",
        )
        self.assertEqual(
            self.runtime["env"]["GAIN_DEVICE_ID"],
            "${{ secrets.GAIN_DEVICE_ID }}",
        )
        self.assertEqual(
            self.runtime["env"]["GAIN_LOGIN_DATA"],
            "${{ secrets.GAIN_LOGIN_DATA }}",
        )
        self.assertNotIn("secrets.", self.runtime["run"])
        self.assertNotIn("WEREAD_CURL", str(self.workflow))
        self.assertNotIn("continue-on-error", self.runtime)
        for step in self.steps:
            if step is not self.runtime:
                self.assertNotIn("GAIN_REFRESH_TOKEN", step.get("env", {}))
                self.assertNotIn("GAIN_DEVICE_ID", step.get("env", {}))
                self.assertNotIn("GAIN_LOGIN_DATA", step.get("env", {}))
        artifact = next(
            step
            for step in self.steps
            if "upload-artifact" in step.get("uses", "")
        )
        self.assertEqual(artifact["if"], "always()")
        self.assertEqual(artifact["with"]["path"], "logs/weekly-gain.log")

    def test_notification_reuses_secrets_and_does_not_change_claim_outcome(
        self,
    ) -> None:
        self.assertEqual(self.notification["if"], "always()")
        self.assertEqual(self.notification["continue-on-error"], "true")
        self.assertEqual(self.notification["timeout-minutes"], "2")
        self.assertEqual(
            self.notification["env"],
            {
                "APPRISE_URL": "${{ secrets.APPRISE_URL }}",
                "APPRISE_URL_BAK": "${{ secrets.APPRISE_URL_BAK }}",
                "NOTIFICATION_ONLY_ON_FAILURE": (
                    "${{ secrets.NOTIFICATION_ONLY_ON_FAILURE || 'false' }}"
                ),
                "GAIN_OUTCOME": "${{ steps.claim.outcome }}",
                "GAIN_LOG_FILE": "logs/weekly-gain.log",
            },
        )
        self.assertNotIn("secrets.", self.notification["run"])
        self.assertLess(
            self.steps.index(self.runtime), self.steps.index(self.notification)
        )

    def run_notification(
        self,
        outcome: str = "success",
        only_failure: str = "false",
        url: str = "test://notification-placeholder",
        backup_url: str = "",
        cli_exit: int = 0,
        log_records: Optional[List[dict]] = None,
    ) -> Tuple[subprocess.CompletedProcess, List[dict]]:
        if log_records is None:
            log_records = [
                {
                    "level": "INFO",
                    "message": "🎁 档位 1: status=1, 可选奖励={2: 1}",
                },
                {
                    "level": "INFO",
                    "message": (
                        "🎁 领奖结果: status=success confirmed=[1] "
                        "already_claimed=[] failed=[] skipped=[] unknown=[]"
                    ),
                },
            ]
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "weekly-gain.log"
            log_path.write_text(
                "\n".join(json.dumps(record) for record in log_records) + "\n",
                encoding="utf-8",
            )
            capture = Path(directory) / "capture.jsonl"
            cli = Path(directory) / "apprise"
            # 假 CLI 逐次记录调用，并故意输出 URL 来验证 workflow 的脱敏。
            cli.write_text(
                f"#!{sys.executable}\n"
                "import json\n"
                "import os\n"
                "import sys\n"
                "from pathlib import Path\n"
                "with Path(os.environ['APPRISE_CAPTURE']).open(\n"
                "    'a', encoding='utf-8'\n"
                ") as capture:\n"
                "    capture.write(json.dumps({\n"
                "        'argv': sys.argv[1:],\n"
                "        'urls': os.environ.get('APPRISE_URLS'),\n"
                "    }) + '\\n')\n"
                "sys.stdout.write(os.environ['APPRISE_URLS'])\n"
                "sys.stderr.write(os.environ['APPRISE_URLS'])\n"
                "sys.exit(int(os.environ['APPRISE_EXIT']))\n",
                encoding="utf-8",
            )
            cli.chmod(0o700)
            result = subprocess.run(
                [
                    "/bin/bash",
                    "--noprofile",
                    "--norc",
                    "-e",
                    "-o",
                    "pipefail",
                    "-c",
                    self.notification["run"],
                ],
                env={
                    "PATH": (
                        f"{directory}:{Path(sys.executable).parent}:{os.defpath}"
                    ),
                    "APPRISE_CAPTURE": str(capture),
                    "APPRISE_EXIT": str(cli_exit),
                    "APPRISE_URL": url,
                    "APPRISE_URL_BAK": backup_url,
                    "NOTIFICATION_ONLY_ON_FAILURE": only_failure,
                    "GAIN_OUTCOME": outcome,
                    "GAIN_LOG_FILE": str(log_path),
                    "GITHUB_REPOSITORY": "test-owner/test-repo",
                    "GITHUB_SERVER_URL": "https://github.com",
                    "GITHUB_RUN_ID": "123",
                    "GAIN_REFRESH_TOKEN": "fake-refresh-for-redaction-test",
                    "GAIN_DEVICE_ID": "fake-device-for-redaction-test",
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            calls = (
                [
                    json.loads(line)
                    for line in capture.read_text(encoding="utf-8").splitlines()
                ]
                if capture.exists()
                else []
            )
            self.assertNotIn("notification-placeholder", result.stdout)
            self.assertNotIn("notification-placeholder", result.stderr)
            self.assertNotIn("backup-placeholder", result.stdout)
            self.assertNotIn("backup-placeholder", result.stderr)
            return result, calls

    def test_notification_without_url_skips_cli(self) -> None:
        result, calls = self.run_notification(url="")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertIn("未配置 APPRISE_URL/APPRISE_URL_BAK", result.stdout)

    def test_success_notification_uses_log_summary_and_safe_run_link(
        self,
    ) -> None:
        result, calls = self.run_notification()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["urls"], "test://notification-placeholder")
        self.assertEqual(call["argv"][0], "--title")
        self.assertTrue(
            call["argv"][1].startswith("✅ 微信读书每周领奖报告")
        )
        self.assertEqual(call["argv"][2], "--body")
        self.assertEqual(len(call["argv"]), 4)
        self.assertIn("结果：✅ 全部正常", call["argv"][3])
        self.assertIn("状态：success", call["argv"][3])
        self.assertIn("本次新领取：1 个（档位 1）", call["argv"][3])
        self.assertIn("档位 1  本次已领取", call["argv"][3])
        self.assertIn(
            "https://github.com/test-owner/test-repo/actions/runs/123",
            call["argv"][3],
        )
        self.assertNotIn("notification-placeholder", str(call["argv"]))
        self.assertNotIn("fake-refresh", str(call["argv"]))
        self.assertNotIn("fake-device", str(call["argv"]))
        self.assertIn("Apprise 通知已发送", result.stdout)

    def test_backup_channel_receives_additional_notification(self) -> None:
        result, calls = self.run_notification(
            backup_url="test://backup-placeholder"
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            [call["urls"] for call in calls],
            [
                "test://notification-placeholder",
                "test://backup-placeholder",
            ],
        )
        self.assertEqual(calls[0]["argv"], calls[1]["argv"])
        self.assertIn("APPRISE_URL", result.stdout)
        self.assertIn("APPRISE_URL_BAK", result.stdout)

    def test_backup_channel_is_optional(self) -> None:
        result, calls = self.run_notification(backup_url="")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("APPRISE_URL_BAK", result.stdout)

    def test_backup_channel_works_without_primary_url(self) -> None:
        result, calls = self.run_notification(
            url="", backup_url="test://backup-placeholder"
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            [call["urls"] for call in calls], ["test://backup-placeholder"]
        )

    def test_backup_channel_is_attempted_after_primary_failure(self) -> None:
        result, calls = self.run_notification(
            backup_url="test://backup-placeholder", cli_exit=1
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            [call["urls"] for call in calls],
            [
                "test://notification-placeholder",
                "test://backup-placeholder",
            ],
        )
        self.assertIn("Apprise 通知失败（APPRISE_URL", result.stdout)
        self.assertIn("Apprise 通知失败（APPRISE_URL_BAK", result.stdout)

    def test_serverchan_channel_uses_markdown_paragraphs(self) -> None:
        result, calls = self.run_notification(
            backup_url="schan://schan-placeholder-token"
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(calls), 2)
        plain_body = calls[0]["argv"][3]
        markdown_body = calls[1]["argv"][3]
        self.assertIn("状态：success\n本次新领取", plain_body)
        self.assertIn("状态：success\n\n本次新领取", markdown_body)
        self.assertIn("异常：无\n\n—— 档位明细 ——", markdown_body)
        self.assertNotIn("schan-placeholder-token", result.stdout)
        self.assertNotIn("schan-placeholder-token", result.stderr)

    def test_only_failure_skips_success_notification(self) -> None:
        for value in ("true", "True", "TRUE", "1", "yes", "YES", "on", "On"):
            with self.subTest(value=value):
                result, calls = self.run_notification(
                    only_failure=value,
                    backup_url="test://backup-placeholder",
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_failure_notifies_even_when_only_failure_is_enabled(self) -> None:
        result, calls = self.run_notification(
            outcome="failure",
            only_failure="true",
            log_records=[
                {
                    "level": "INFO",
                    "message": "🎁 档位 1: status=1, 可选奖励={2: 1}",
                },
                {
                    "level": "INFO",
                    "message": (
                        "🎁 领奖结果: status=partial_success confirmed=[] "
                        "already_claimed=[] failed=[1] skipped=[] unknown=[]"
                    ),
                },
            ],
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("结果：⚠️ 部分成功", calls[0]["argv"][3])
        self.assertIn("领取失败 档位 1", calls[0]["argv"][3])
        self.assertIn("需人工核实", calls[0]["argv"][3])

    def test_cancelled_and_skipped_steps_do_not_report_success(self) -> None:
        cancelled_log = [
            {
                "level": "INFO",
                "message": (
                    "🎁 领奖结果: status=cancelled confirmed=[] "
                    "already_claimed=[] failed=[] skipped=[] unknown=[]"
                ),
            },
        ]
        for outcome, records, title_text, body_text in (
            ("cancelled", cancelled_log, "已中断", "结果：⏹️ 已中断"),
            (
                "skipped",
                [],
                "未找到领奖结果，退出状态 skipped",
                "结果：❌ 结果未知",
            ),
        ):
            with self.subTest(outcome=outcome):
                result, calls = self.run_notification(
                    outcome=outcome,
                    only_failure="true",
                    log_records=records,
                )
                self.assertEqual(result.returncode, 0)
                self.assertIn(title_text, calls[0]["argv"][1])
                self.assertIn(body_text, calls[0]["argv"][3])
                self.assertNotIn("全部正常", calls[0]["argv"][3])

    def test_notification_failure_hides_raw_cli_output(self) -> None:
        result, calls = self.run_notification(cli_exit=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        self.assertIn("Apprise 通知失败", result.stdout)
        self.assertEqual(self.notification["continue-on-error"], "true")
        self.assertNotIn("continue-on-error", self.runtime)


if __name__ == "__main__":
    unittest.main()
