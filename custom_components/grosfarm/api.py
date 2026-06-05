"""HTTP+WebSocket клиент к облаку Gros.farm (или к локальному моку).

Контракт описан в `mock-cloud/PROTOCOL.md`. Зависит только от aiohttp — никаких
homeassistant-имплантов, чтобы файл можно было импортить из dev-скриптов.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from uuid import uuid4

import aiohttp

_LOGGER = logging.getLogger(__name__)


class GrosfarmAPIError(Exception):
    """Базовая ошибка API."""


class GrosfarmAuthError(GrosfarmAPIError):
    """Невалидные креды или просроченный токен."""


@dataclass(slots=True)
class RegistrationResult:
    """Что мы получили от облака в ответ на register_control_unit."""

    control_unit_id: str
    section_ids: dict[str, str]
    stream_url: str


class GrosfarmCloudClient:
    """Клиент облака. HTTP — для bootstrap, WS — для двусторонней live-связи."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        login: str,
        password: str,
        api_key: str,
    ) -> None:
        """Хранит креды, не выполняет сетевых вызовов до authenticate()."""
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._login = login
        self._password = password
        self._api_key = api_key
        self._token: str | None = None

    @property
    def base_url(self) -> str:
        """Базовый URL облака без хвостового слэша."""
        return self._base_url

    @property
    def access_token(self) -> str | None:
        """Текущий bearer-токен либо None, если ещё не authenticated."""
        return self._token

    @contextlib.asynccontextmanager
    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """HTTP к облаку с переводом транспортных ошибок в GrosfarmAPIError.

        Connection refused / host unreachable / timeout — это «облако недоступно»,
        а не баг: оборачиваем в GrosfarmAPIError, чтобы coordinator мог уйти в
        автономный режим, а не валить setup сырым aiohttp-исключением (см.
        coordinator.async_start). HTTP-статусы (401/4xx/5xx) — на совести
        вызывающих методов: исключения, поднятые внутри `async with`, проходят
        наружу как есть.
        """
        try:
            async with self._session.request(
                method, f"{self._base_url}{path}", **kwargs
            ) as resp:
                yield resp
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise GrosfarmAPIError(f"cloud unreachable: {exc}") from exc

    async def authenticate(self) -> str:
        """Выполнить POST /auth/login, запомнить и вернуть access_token."""
        body = {
            "login": self._login,
            "password": self._password,
            "api_key": self._api_key,
        }
        async with self._request("POST", "/api/v1/auth/login", json=body) as resp:
            if resp.status == HTTPStatus.UNAUTHORIZED:
                raise GrosfarmAuthError("неверные креды или api_key")
            if resp.status != HTTPStatus.OK:
                raise GrosfarmAPIError(f"login failed: {resp.status}")
            payload = await resp.json()
        self._token = payload["access_token"]
        return self._token

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            raise GrosfarmAuthError("not authenticated")
        return {"Authorization": f"Bearer {self._token}"}

    async def register_control_unit(
        self,
        *,
        mac_address: str,
        name: str,
        firmware_version: str,
        sections: list[dict[str, str]],
        sensors: list[dict[str, Any]] | None = None,
        devices: list[dict[str, Any]] | None = None,
    ) -> RegistrationResult:
        """Зарегистрировать (или обновить) этот HAOS как ControlUnit в облаке."""
        body: dict[str, Any] = {
            "mac_address": mac_address,
            "name": name,
            "firmware_version": firmware_version,
            "sections": sections,
            "sensors": sensors or [],
            "devices": devices or [],
        }
        async with self._request(
            "POST",
            "/api/v1/control-units/register",
            json=body,
            headers=self._headers(),
        ) as resp:
            if resp.status == HTTPStatus.UNAUTHORIZED:
                raise GrosfarmAuthError("token expired")
            if resp.status >= HTTPStatus.BAD_REQUEST:
                detail = await resp.text()
                raise GrosfarmAPIError(f"register failed {resp.status}: {detail}")
            data = await resp.json()
        return RegistrationResult(
            control_unit_id=data["control_unit_id"],
            section_ids=data["section_ids"],
            stream_url=data["stream_url"],
        )

    async def get_setpoints(self, cu_id: str) -> dict[str, Any]:
        """Снять snapshot уставок (HTTP-фоллбэк к WS push'у)."""
        async with self._request(
            "GET",
            f"/api/v1/control-units/{cu_id}/setpoints",
            headers=self._headers(),
        ) as resp:
            if resp.status == HTTPStatus.UNAUTHORIZED:
                raise GrosfarmAuthError("token expired")
            if resp.status >= HTTPStatus.BAD_REQUEST:
                detail = await resp.text()
                raise GrosfarmAPIError(f"get_setpoints failed {resp.status}: {detail}")
            data: dict[str, Any] = await resp.json()
            return data

    async def post_zone_status(
        self,
        cu_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Pусь runtime-статуса зоны (lighting controller — DLI, lamp, status)."""
        async with self._request(
            "POST",
            f"/api/v1/control-units/{cu_id}/status",
            json=body,
            headers=self._headers(),
        ) as resp:
            if resp.status == HTTPStatus.UNAUTHORIZED:
                raise GrosfarmAuthError("token expired")
            if resp.status >= HTTPStatus.BAD_REQUEST:
                detail = await resp.text()
                raise GrosfarmAPIError(
                    f"post_zone_status failed {resp.status}: {detail}"
                )
            data: dict[str, Any] = await resp.json()
            return data

    async def post_measurements(
        self,
        cu_id: str,
        measurements: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Отправить батч измерений HTTP'ом (фоллбэк когда WS лежит)."""
        body = {
            "control_unit_id": cu_id,
            "client_batch_id": uuid4().hex,
            "measurements": measurements,
        }
        async with self._request(
            "POST",
            "/api/v1/measurements",
            json=body,
            headers=self._headers(),
        ) as resp:
            if resp.status == HTTPStatus.UNAUTHORIZED:
                raise GrosfarmAuthError("token expired")
            if resp.status >= HTTPStatus.BAD_REQUEST:
                detail = await resp.text()
                raise GrosfarmAPIError(
                    f"post_measurements failed {resp.status}: {detail}"
                )
            data: dict[str, Any] = await resp.json()
            return data


class GrosfarmStream:
    """Долгоживущий WS-канал. Reconnect с backoff, hello-handshake, рассылка событий."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        access_token_provider: Callable[[], Awaitable[str]],
        current_version: int,
        on_setpoints: Callable[[dict[str, Any]], Awaitable[None]],
        on_command: Callable[[dict[str, Any]], Awaitable[None]],
        backoff_seconds: tuple[int, ...] = (1, 2, 5, 10, 30),
    ) -> None:
        """Сохранить колбэки/URL; ничего не подключать до start()."""
        self._session = session
        self._url = url
        self._token_provider = access_token_provider
        self._on_setpoints = on_setpoints
        self._on_command = on_command
        self._backoff = backoff_seconds
        self._current_version = current_version
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._send_lock = asyncio.Lock()

    def start(self) -> None:
        """Запустить фоновую coroutine, поддерживающую WS-соединение."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="grosfarm_ws")

    async def stop(self) -> None:
        """Корректно остановить фоновую coroutine и закрыть сокет."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    def update_known_version(self, version: int) -> None:
        """Запомнить максимальную известную версию уставок (для hello-handshake'а)."""
        self._current_version = max(version, self._current_version)

    async def send_telemetry(self, measurements: list[dict[str, Any]]) -> str | None:
        """Отправить батч измерений в WS, вернуть message_id (None если WS лежит)."""
        if self._ws is None or self._ws.closed:
            return None
        client_message_id = uuid4().hex
        async with self._send_lock:
            await self._ws.send_json(
                {
                    "type": "telemetry",
                    "client_message_id": client_message_id,
                    "measurements": measurements,
                }
            )
        return client_message_id

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            try:
                token = await self._token_provider()
                async with self._session.ws_connect(self._url, heartbeat=30) as ws:
                    self._ws = ws
                    await ws.send_json(
                        {
                            "type": "hello",
                            "access_token": token,
                            "current_setpoints_version": self._current_version,
                        }
                    )
                    attempt = 0
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._dispatch(msg.json())
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                if self._ws is not None and not self._ws.closed:
                    await self._ws.close()
                raise
            except Exception as exc:
                _LOGGER.warning("WS error: %s", exc)
            finally:
                self._ws = None
            if self._stopping:
                return  # type: ignore[unreachable]
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            _LOGGER.info("WS reconnect через %s сек", delay)
            await asyncio.sleep(delay)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "hello.ack":
            self.update_known_version(int(msg.get("setpoints_version", 0)))
        elif msg_type == "setpoints.update":
            await self._on_setpoints(msg)
            self.update_known_version(int(msg.get("version", 0)))
            if self._ws is not None and not self._ws.closed:
                async with self._send_lock:
                    await self._ws.send_json(
                        {"type": "setpoints.ack", "version": msg.get("version")}
                    )
        elif msg_type and msg_type.startswith("command"):
            await self._on_command(msg)
        elif msg_type == "telemetry.ack":
            _LOGGER.debug("telemetry ack: %s", msg)
        elif msg_type == "pong":
            pass
        elif msg_type == "ping":
            if self._ws is not None and not self._ws.closed:
                async with self._send_lock:
                    await self._ws.send_json(
                        {"type": "pong", "ts": datetime.now(UTC).isoformat()}
                    )
        elif msg_type == "error":
            _LOGGER.error("WS error from server: %s", msg)
        else:
            _LOGGER.debug("WS unknown msg: %s", msg)
