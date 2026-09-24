"""Tests for the CAPSolver Turnstile solver integration (#2623)."""

from __future__ import annotations

import httpx
import pytest
import respx

from framework.turnstile_solver import (
    CAPSOLVER_API_BASE,
    get_balance,
    solve_turnstile,
)

SITEKEY = "0x4AAAAAAAhseTmUbrk0U5ab"
PAGE_URL = "https://webapps.sftc.org/captcha/captcha.dll"
API_KEY = "test-api-key-1234"


class TestSolveTurnstileMissingKey:
    """solve_turnstile short-circuits when no API key is available."""

    @pytest.mark.asyncio
    async def test_returns_none_when_api_key_is_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty api_key AND empty env var -> returns None immediately."""
        monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
        token = await solve_turnstile(sitekey=SITEKEY, page_url=PAGE_URL)
        assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_when_api_key_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """api_key=None + missing env var -> returns None immediately."""
        monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
        token = await solve_turnstile(sitekey=SITEKEY, page_url=PAGE_URL, api_key=None)
        assert token is None

    @pytest.mark.asyncio
    async def test_does_not_call_api_without_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No API call should be made when no key is present."""
        monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
        with respx.mock(base_url=CAPSOLVER_API_BASE, assert_all_called=False) as router:
            create_route = router.post("/createTask").mock(
                return_value=httpx.Response(200, json={"taskId": "x"})
            )
            token = await solve_turnstile(sitekey=SITEKEY, page_url=PAGE_URL)
            assert token is None
            assert not create_route.called


class TestSolveTurnstileEnvFallback:
    """solve_turnstile picks up the API key from CAPSOLVER_API_KEY env var."""

    @pytest.mark.asyncio
    async def test_uses_env_var_when_no_explicit_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Setting CAPSOLVER_API_KEY env var enables the solver."""
        monkeypatch.setenv("CAPSOLVER_API_KEY", API_KEY)

        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/createTask").mock(
                return_value=httpx.Response(
                    200, json={"errorId": 0, "taskId": "task-123", "status": "idle"}
                )
            )
            router.post("/getTaskResult").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "errorId": 0,
                        "status": "ready",
                        "solution": {"token": "TOKEN_FROM_ENV"},
                    },
                )
            )
            token = await solve_turnstile(sitekey=SITEKEY, page_url=PAGE_URL)

        assert token == "TOKEN_FROM_ENV"


class TestSolveTurnstileHappyPath:
    """solve_turnstile returns the token when CAPSolver succeeds."""

    @pytest.mark.asyncio
    async def test_returns_token_on_immediate_ready(self) -> None:
        """getTaskResult returns ready on first poll -> token is returned."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            create = router.post("/createTask").mock(
                return_value=httpx.Response(
                    200, json={"errorId": 0, "taskId": "abc", "status": "idle"}
                )
            )
            poll = router.post("/getTaskResult").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "errorId": 0,
                        "status": "ready",
                        "solution": {"token": "cf.turnstile.token.value"},
                    },
                )
            )
            token = await solve_turnstile(sitekey=SITEKEY, page_url=PAGE_URL, api_key=API_KEY)

        assert token == "cf.turnstile.token.value"
        assert create.called
        assert poll.called

    @pytest.mark.asyncio
    async def test_create_task_payload_shape(self) -> None:
        """createTask request must carry clientKey and the Turnstile task type."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            create = router.post("/createTask").mock(
                return_value=httpx.Response(
                    200, json={"errorId": 0, "taskId": "abc", "status": "idle"}
                )
            )
            router.post("/getTaskResult").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "errorId": 0,
                        "status": "ready",
                        "solution": {"token": "T"},
                    },
                )
            )
            await solve_turnstile(sitekey=SITEKEY, page_url=PAGE_URL, api_key=API_KEY)

        assert create.called
        request = create.calls[0].request
        import json as _json

        body = _json.loads(request.content)
        assert body["clientKey"] == API_KEY
        assert body["task"]["type"] == "AntiTurnstileTaskProxyLess"
        assert body["task"]["websiteURL"] == PAGE_URL
        assert body["task"]["websiteKey"] == SITEKEY

    @pytest.mark.asyncio
    async def test_polls_until_ready(self) -> None:
        """Solver waits for 'processing' responses then returns the token."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/createTask").mock(
                return_value=httpx.Response(
                    200, json={"errorId": 0, "taskId": "abc", "status": "idle"}
                )
            )
            poll_route = router.post("/getTaskResult").mock(
                side_effect=[
                    httpx.Response(200, json={"errorId": 0, "status": "processing"}),
                    httpx.Response(200, json={"errorId": 0, "status": "processing"}),
                    httpx.Response(
                        200,
                        json={
                            "errorId": 0,
                            "status": "ready",
                            "solution": {"token": "FINAL_TOKEN"},
                        },
                    ),
                ]
            )
            token = await solve_turnstile(
                sitekey=SITEKEY,
                page_url=PAGE_URL,
                api_key=API_KEY,
                poll_interval=0.0,
            )

        assert token == "FINAL_TOKEN"
        assert poll_route.call_count == 3


class TestSolveTurnstileErrors:
    """solve_turnstile returns None on various error conditions."""

    @pytest.mark.asyncio
    async def test_returns_none_on_create_task_http_error(self) -> None:
        """HTTP 500 on createTask -> None."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/createTask").mock(return_value=httpx.Response(500, text="boom"))
            token = await solve_turnstile(sitekey=SITEKEY, page_url=PAGE_URL, api_key=API_KEY)
        assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_on_create_task_api_error(self) -> None:
        """errorId != 0 on createTask -> None."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/createTask").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "errorId": 1,
                        "errorCode": "ERROR_KEY_DOES_NOT_EXIST",
                        "errorDescription": "Invalid key",
                    },
                )
            )
            token = await solve_turnstile(sitekey=SITEKEY, page_url=PAGE_URL, api_key=API_KEY)
        assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_on_create_task_invalid_json(self) -> None:
        """Non-JSON response on createTask -> None."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/createTask").mock(
                return_value=httpx.Response(200, text="not json at all")
            )
            token = await solve_turnstile(sitekey=SITEKEY, page_url=PAGE_URL, api_key=API_KEY)
        assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_on_missing_task_id(self) -> None:
        """createTask response missing taskId -> None."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/createTask").mock(
                return_value=httpx.Response(200, json={"errorId": 0, "status": "idle"})
            )
            token = await solve_turnstile(sitekey=SITEKEY, page_url=PAGE_URL, api_key=API_KEY)
        assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_on_poll_http_error(self) -> None:
        """HTTP 500 on getTaskResult -> None."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/createTask").mock(
                return_value=httpx.Response(
                    200, json={"errorId": 0, "taskId": "abc", "status": "idle"}
                )
            )
            router.post("/getTaskResult").mock(return_value=httpx.Response(500, text="boom"))
            token = await solve_turnstile(
                sitekey=SITEKEY,
                page_url=PAGE_URL,
                api_key=API_KEY,
                poll_interval=0.0,
            )
        assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_on_poll_api_error(self) -> None:
        """errorId != 0 on getTaskResult -> None."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/createTask").mock(
                return_value=httpx.Response(
                    200, json={"errorId": 0, "taskId": "abc", "status": "idle"}
                )
            )
            router.post("/getTaskResult").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "errorId": 22,
                        "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                        "errorDescription": "Solver could not solve it.",
                    },
                )
            )
            token = await solve_turnstile(
                sitekey=SITEKEY,
                page_url=PAGE_URL,
                api_key=API_KEY,
                poll_interval=0.0,
            )
        assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_on_poll_invalid_json(self) -> None:
        """Non-JSON response on getTaskResult -> None."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/createTask").mock(
                return_value=httpx.Response(
                    200, json={"errorId": 0, "taskId": "abc", "status": "idle"}
                )
            )
            router.post("/getTaskResult").mock(return_value=httpx.Response(200, text="not json"))
            token = await solve_turnstile(
                sitekey=SITEKEY,
                page_url=PAGE_URL,
                api_key=API_KEY,
                poll_interval=0.0,
            )
        assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_on_ready_with_empty_token(self) -> None:
        """status=ready but solution.token is missing/empty -> None."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/createTask").mock(
                return_value=httpx.Response(
                    200, json={"errorId": 0, "taskId": "abc", "status": "idle"}
                )
            )
            router.post("/getTaskResult").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "errorId": 0,
                        "status": "ready",
                        "solution": {"token": ""},
                    },
                )
            )
            token = await solve_turnstile(
                sitekey=SITEKEY,
                page_url=PAGE_URL,
                api_key=API_KEY,
                poll_interval=0.0,
            )
        assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_on_ready_without_solution(self) -> None:
        """status=ready but no solution key at all -> None."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/createTask").mock(
                return_value=httpx.Response(
                    200, json={"errorId": 0, "taskId": "abc", "status": "idle"}
                )
            )
            router.post("/getTaskResult").mock(
                return_value=httpx.Response(200, json={"errorId": 0, "status": "ready"})
            )
            token = await solve_turnstile(
                sitekey=SITEKEY,
                page_url=PAGE_URL,
                api_key=API_KEY,
                poll_interval=0.0,
            )
        assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_on_poll_timeout(self) -> None:
        """Solver gives up when total elapsed exceeds timeout."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/createTask").mock(
                return_value=httpx.Response(
                    200, json={"errorId": 0, "taskId": "abc", "status": "idle"}
                )
            )
            router.post("/getTaskResult").mock(
                return_value=httpx.Response(200, json={"errorId": 0, "status": "processing"})
            )
            token = await solve_turnstile(
                sitekey=SITEKEY,
                page_url=PAGE_URL,
                api_key=API_KEY,
                timeout=0.001,
                poll_interval=0.0,
            )
        assert token is None


class TestSolveTurnstileExplicitKeyBeatsEnv:
    """Explicit api_key argument takes precedence over env var."""

    @pytest.mark.asyncio
    async def test_explicit_key_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """api_key argument is sent to CAPSolver instead of the env var."""
        monkeypatch.setenv("CAPSOLVER_API_KEY", "env-key-should-not-be-used")

        import json as _json

        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            create = router.post("/createTask").mock(
                return_value=httpx.Response(
                    200, json={"errorId": 0, "taskId": "t", "status": "idle"}
                )
            )
            router.post("/getTaskResult").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "errorId": 0,
                        "status": "ready",
                        "solution": {"token": "TOK"},
                    },
                )
            )
            token = await solve_turnstile(
                sitekey=SITEKEY,
                page_url=PAGE_URL,
                api_key="explicit-key",
            )

        assert token == "TOK"
        body = _json.loads(create.calls[0].request.content)
        assert body["clientKey"] == "explicit-key"


class TestGetBalance:
    """get_balance reports CAPSolver key validity + remaining credit (#4638)."""

    @pytest.mark.asyncio
    async def test_valid_response_returns_balance(self) -> None:
        """errorId 0 with a balance -> valid=True, balance parsed."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/getBalance").mock(
                return_value=httpx.Response(200, json={"errorId": 0, "balance": 12.5})
            )
            result = await get_balance(API_KEY)
        assert result.valid is True
        assert result.balance == 12.5
        assert result.error_code is None

    @pytest.mark.asyncio
    async def test_invalid_key_400_body_reports_error_code(self) -> None:
        """The #4633 invalid-key response — HTTP 400 with an errorId body."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/getBalance").mock(
                return_value=httpx.Response(
                    400,
                    json={
                        "errorId": 1,
                        "errorCode": "ERROR_KEY_DOES_NOT_EXIST",
                        "errorDescription": "clientKey is invalid",
                    },
                )
            )
            result = await get_balance(API_KEY)
        assert result.valid is False
        assert result.error_code == "ERROR_KEY_DOES_NOT_EXIST"
        assert result.error_description == "clientKey is invalid"

    @pytest.mark.asyncio
    async def test_empty_key_makes_no_api_call(self) -> None:
        """Empty key -> valid=False without hitting the API."""
        with respx.mock(base_url=CAPSOLVER_API_BASE, assert_all_called=False) as router:
            route = router.post("/getBalance").mock(
                return_value=httpx.Response(200, json={"errorId": 0, "balance": 1.0})
            )
            result = await get_balance("")
        assert result.valid is False
        assert result.balance is None
        assert not route.called

    @pytest.mark.asyncio
    async def test_none_key_makes_no_api_call(self) -> None:
        """None key -> valid=False without hitting the API."""
        result = await get_balance(None)
        assert result.valid is False

    @pytest.mark.asyncio
    async def test_http_error_returns_invalid(self) -> None:
        """A transport-level error -> valid=False with a detail string."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/getBalance").mock(side_effect=httpx.ConnectError("boom"))
            result = await get_balance(API_KEY)
        assert result.valid is False
        assert result.error_description is not None

    @pytest.mark.asyncio
    async def test_invalid_json_returns_invalid(self) -> None:
        """A non-JSON body -> valid=False."""
        with respx.mock(base_url=CAPSOLVER_API_BASE) as router:
            router.post("/getBalance").mock(return_value=httpx.Response(200, text="not json"))
            result = await get_balance(API_KEY)
        assert result.valid is False
