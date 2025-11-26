"""
VK Captcha Solver - автоматическое решение капчи VK.

Пример использования:
    solver = CaptchaSolver()

    # Простой вызов
    async with solver.session(proxy="http://proxy:8080") as api:
        token = await solver.solve(redirect_uri)

    # Интеграция с vkbottle
    await solver.vkbottle_captcha_handler(error, proxy="http://proxy:8080")
"""

import hashlib
import base64
import json
import asyncio
from contextlib import asynccontextmanager
from typing import Optional, AsyncIterator, TYPE_CHECKING

from .api import API
from .checkbox_solver import CheckboxCaptchaSolver
from .slider_solver import SliderCaptchaSolver
from .exceptions import VKCaptchaSolverError, APIError, HTTPError
from .types import IApiOptions, ICaptchaCheckParams

if TYPE_CHECKING:
    try:
        from vkbottle import CaptchaError, ValidationError
    except ImportError:
        pass


class CaptchaSolver:
    """
    Решатель капчи VK с поддержкой прокси.

    Attributes:
        api_options: Базовые настройки API (headers, cookies).

    Example:
        >>> solver = CaptchaSolver()
        >>> async with solver.session(proxy="http://proxy:8080") as api:
        ...     token = await solver.solve(redirect_uri)
    """

    def __init__(self, api_options: Optional[IApiOptions] = None):
        self.api_options = api_options or {}
        self._api: Optional[API] = None

    # =========================================================================
    # Public API
    # =========================================================================

    @asynccontextmanager
    async def session(
        self, proxy: Optional[str] = None
    ) -> AsyncIterator["CaptchaSolver"]:
        """
        Контекстный менеджер для создания сессии с прокси.

        Args:
            proxy: URL прокси-сервера (например, "http://user:pass@host:port").

        Yields:
            self: Экземпляр CaptchaSolver с активной сессией.

        Example:
            >>> async with solver.session(proxy="http://proxy:8080") as s:
            ...     token = await s.solve(redirect_uri)
        """
        self._api = self._create_api(proxy)
        try:
            yield self
        finally:
            await self._close_api()

    async def solve(self, redirect_uri: str) -> str:
        """
        Решает капчу по redirect_uri.

        Args:
            redirect_uri: URL страницы капчи.

        Returns:
            success_token для подтверждения решения.

        Raises:
            VKCaptchaSolverError: Если тип капчи неизвестен или произошла ошибка.
        """
        if self._api is None or self._api.closed:
            raise VKCaptchaSolverError(
                "No active session. Use 'async with solver.session():'"
            )

        initial = await self._api.get_initial_params(redirect_uri)

        captcha_type = initial["data"].get("show_captcha_type")
        if captcha_type not in ("slider", "checkbox"):
            raise VKCaptchaSolverError(f"Unknown captcha type: {captcha_type}")

        settings = await self._api.get_settings(initial["data"])

        # Compute proof-of-work in executor (CPU-bound)
        loop = asyncio.get_running_loop()
        pow_hash = await loop.run_in_executor(
            None, self._generate_pow, initial["powInput"], initial["difficulty"]
        )

        check_params = self._build_check_params(initial, pow_hash)

        if captcha_type == "checkbox":
            check_params = await self._solve_checkbox(initial, settings, check_params)
        else:
            check_params = await self._solve_slider(
                initial, settings, check_params, loop
            )

        response = await self._api.check(check_params)

        await self._api.end_session(initial["data"])

        return response["success_token"]

    # =========================================================================
    # vkbottle Integration
    # =========================================================================

    async def vkbottle_captcha_handler(
        self,
        error: "CaptchaError",
        proxy: Optional[str] = None,
    ) -> str:
        """
        Хэндлер капчи для vkbottle.

        Args:
            error: CaptchaError от vkbottle.
            proxy: URL прокси-сервера.

        Returns:
            success_token для vkbottle.
        """
        async with self.session(proxy):
            return await self.solve(error.redirect_uri)

    async def vkbottle_validation_handler(
        self,
        error: "ValidationError",
        proxy: Optional[str] = None,
    ) -> None:
        """
        Хэндлер валидации для vkbottle.

        Args:
            error: ValidationError от vkbottle.
            proxy: URL прокси-сервера.
        """
        validation_uri = error.redirect_uri.replace("act=validate", "act=captcha")

        async with self.session(proxy):
            session_token, cookies = await self._api.get_session_data(validation_uri)
            captcha_uri = f"https://id.vk.com/not_robot_captcha?domain=vk.com&session_token={session_token}&variant=popup&blank=1"

            success_token = await self.solve(captcha_uri)
            await self._api.validate(validation_uri, success_token, cookies)

    # =========================================================================
    # Legacy Context Manager (для обратной совместимости)
    # =========================================================================

    async def __aenter__(self):
        """Deprecated: используйте solver.session(proxy) вместо этого."""
        self._api = self._create_api()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._close_api()

    # =========================================================================
    # Private Methods
    # =========================================================================

    def _create_api(self, proxy: Optional[str] = None) -> API:
        """Создаёт экземпляр API с опциональным прокси."""
        options = dict(self.api_options)
        if proxy:
            options["proxy"] = proxy
        return API(options)  # type: ignore

    async def _close_api(self) -> None:
        """Закрывает текущую API сессию."""
        if self._api and not self._api.closed:
            await self._api.close()
        self._api = None

    def _build_check_params(self, initial: dict, pow_hash: str) -> ICaptchaCheckParams:
        """Формирует базовые параметры для проверки капчи."""
        return {
            "domain": initial["data"]["domain"],
            "session_token": initial["data"]["session_token"],
            "hash": pow_hash,
            "answer": "e30=",  # base64("{}")
            "accelerometer": [],
            "cursor": [],
            "gyroscope": [],
            "motion": [],
            "taps": [],
        }

    async def _solve_checkbox(
        self, initial: dict, settings: dict, check_params: ICaptchaCheckParams
    ) -> ICaptchaCheckParams:
        """Решает checkbox-капчу."""
        await self._api.component_done(initial["data"])

        solver = CheckboxCaptchaSolver()
        params = solver.solve(settings.get("bridge_sensors_list", []))
        check_params.update(params)  # type: ignore

        return check_params

    async def _solve_slider(
        self,
        initial: dict,
        settings: dict,
        check_params: ICaptchaCheckParams,
        loop: asyncio.AbstractEventLoop,
    ) -> ICaptchaCheckParams:
        """Решает slider-капчу."""
        content = await self._api.get_content(initial["data"])

        await self._api.component_done(initial["data"])

        solver = SliderCaptchaSolver()
        params = await loop.run_in_executor(None, solver.solve, content)

        answer = json.dumps({"value": params.get("selectedSwaps")})
        check_params["answer"] = base64.b64encode(answer.encode()).decode()

        return check_params

    def _generate_pow(self, input_str: str, difficulty: int) -> str:
        """Генерирует proof-of-work хэш."""
        target_prefix = "0" * difficulty
        nonce = 0

        while True:
            nonce += 1
            data = f"{input_str}{nonce}".encode()
            hash_val = hashlib.sha256(data).hexdigest()
            if hash_val.startswith(target_prefix):
                return hash_val


__all__ = [
    "CaptchaSolver",
    "VKCaptchaSolverError",
    "APIError",
    "HTTPError",
    "IApiOptions",
]
