import re
import json
import aiohttp
from typing import Optional, Dict, Any
from urllib.parse import urljoin

from .types import (
    IApiOptions,
    ICaptchaInitialParams,
    ICaptchaInitialParamsResponse,
    ICaptchaSettings,
    ICaptchaContent,
    ICaptchaCheckParams,
    ICaptchaCheck,
)
from .utils import safe_json_parse
from .exceptions import APIError, HTTPError, VKCaptchaSolverError


class API:
    def __init__(self, options: Optional[IApiOptions] = None):
        options = options or {}
        self.version = options.get("version", "5.199")
        self.base_url = options.get("baseUrl", "https://api.vk.ru")
        self.headers = options.get("headers", {})
        self.cookies = options.get("cookies", {})
        self.cookies.update(
            {
                "remixmdevice": "1440/900/2/!!-!!!!!!!!/158",
            }
        )
        self.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
            }
        )
        # NOTE: Session is not created in __init__ because it needs to be async managed
        # We will use a per-request session or let the user manage it if this class was persistent
        # For simplicity in this port, we'll create a session in methods or require one passed
        # But since the original class had a persistent session, let's use one.
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self.headers,
                cookies=self.cookies,
            )
        return self._session

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def get_session_data(self, url: str) -> tuple[str, str]:
        session = await self._get_session()
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                html = await response.text()
        except aiohttp.ClientResponseError as e:
            raise HTTPError(e.status, e.message, url) from e
        except Exception as e:
            raise VKCaptchaSolverError(f"Failed to get session token: {e}") from e

        session_token_match = re.search(r"session_token\s*=\s*([^&]+)&", html)
        if not session_token_match:
            raise VKCaptchaSolverError(
                'Missing required value: "session_token" not found in page content.'
            )

        session_token = session_token_match.group(1)
        remixstlid = response.cookies.get("remixstlid").value

        return session_token, remixstlid

    async def get_initial_params(self, url: str) -> ICaptchaInitialParams:
        """Gets the captcha page (iframe) and parses parameters."""
        session = await self._get_session()
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                html = await response.text()
        except aiohttp.ClientResponseError as e:
            raise HTTPError(e.status, e.message, url) from e
        except Exception as e:
            raise VKCaptchaSolverError(f"Failed to fetch captcha page: {e}") from e

        pow_input_match = re.search(
            r'const powInput\s*=\s*"([^"]+)";', html, re.IGNORECASE
        )
        if not pow_input_match:
            pow_input_match = re.search(
                r'const powInput\s*=\s*"([^"]+)";', html, re.IGNORECASE
            )

        if not pow_input_match:
            # try finding it with single quotes
            pow_input_match = re.search(
                r"const powInput\s*=\s*'([^']+)';", html, re.IGNORECASE
            )

        if not pow_input_match:
            raise VKCaptchaSolverError(
                'Missing required value: "powInput" not found in page content.'
            )

        pow_input = pow_input_match.group(1)

        difficulty_match = re.search(r"const difficulty\s*=\s*(\d+);", html)
        if not difficulty_match:
            raise VKCaptchaSolverError(
                'Missing required value: "difficulty" not found in page content.'
            )

        difficulty = int(difficulty_match.group(1))

        # Window init match
        init_match = re.search(
            r"window\.init\s*=\s*({[\s\S]*?});\s*(?=\s*window\.lang|</script>|$)", html
        )

        if not init_match:
            raise VKCaptchaSolverError(
                'Missing required value: "window.init" not found in page content.'
            )

        initial_params_json = init_match.group(1)
        initial_params: Optional[ICaptchaInitialParamsResponse] = safe_json_parse(
            initial_params_json
        )

        if not initial_params:
            raise VKCaptchaSolverError("Invalid window.init data")

        return {
            "data": initial_params["data"],
            "difficulty": difficulty,
            "powInput": pow_input,
        }

    async def get_settings(self, params: Dict[str, str]) -> ICaptchaSettings:
        """Gets captcha settings."""
        return await self.call(
            "captchaNotRobot.settings",
            {"session_token": params["session_token"], "domain": params["domain"]},
        )

    async def get_content(self, params: Dict[str, Any]) -> ICaptchaContent:
        """Gets captcha content (Only for "slider" captcha)."""
        captcha_settings_list = params.get("captcha_settings", [])
        show_captcha_type = params.get("show_captcha_type")

        settings_str = next(
            (
                s["settings"]
                for s in captcha_settings_list
                if s["type"] == show_captcha_type
            ),
            None,
        )

        return await self.call(
            "captchaNotRobot.getContent",
            {
                "session_token": params["session_token"],
                "domain": params["domain"],
                "captcha_settings": settings_str,
            },
        )

    async def component_done(self, params: Dict[str, str]) -> None:
        """Sends "Component ready/loaded" event."""
        await self.call(
            "captchaNotRobot.componentDone",
            {"session_token": params["session_token"], "domain": params["domain"]},
        )

    async def check(self, params: ICaptchaCheckParams) -> ICaptchaCheck:
        """Sends solved captcha data for verification."""
        return await self.call("captchaNotRobot.check", params)

    async def end_session(self, params: Dict[str, str]) -> None:
        """Sends "Session ended" event."""
        await self.call(
            "captchaNotRobot.endSession",
            {"session_token": params["session_token"], "domain": params["domain"]},
        )

    async def call(self, method: str, params: Dict[str, Any]) -> Any:
        request_params = {**params, "v": self.version}

        # Serialize params like in TS
        serialized_params = {}
        for key, value in request_params.items():
            if value is None:
                serialized_params[key] = ""
            elif isinstance(value, (list, dict)):
                serialized_params[key] = json.dumps(value)
            else:
                serialized_params[key] = str(value)

        url = urljoin(self.base_url, f"/method/{method}")
        session = await self._get_session()

        try:
            async with session.post(url, data=serialized_params) as response:
                response.raise_for_status()
                data = await response.json()
        except aiohttp.ClientResponseError as e:
            raise HTTPError(e.status, e.message, url) from e
        except Exception as e:
            # Handle other network errors
            raise VKCaptchaSolverError(f"Network error during API call: {e}") from e

        if "error" in data:
            raise APIError(data["error"], method)

        if "response" in data:
            resp = data["response"]
            if isinstance(resp, dict) and resp.get("status") and resp["status"] != "OK":
                # Construct error object for APIError
                error_payload = {
                    "status": resp["status"],
                    "error_msg": "Bad method status",
                    "request_params": [
                        {"key": k, "value": v} for k, v in serialized_params.items()
                    ],
                }
                raise APIError(error_payload, method)
            return resp

        # If neither error nor response?
        return data

    async def validate(self, url: str, success_token: str, remixstlid: str) -> None:
        async with self._session.post(
            url,
            cookies={
                "remixstlid": remixstlid,
            },
            data={
                "success_token": success_token,
            },
        ) as response:
            try:
                response.raise_for_status()
            except aiohttp.ClientResponseError as e:
                raise HTTPError(e.status, e.message, url) from e
            except Exception as e:
                raise VKCaptchaSolverError(f"Failed to validate captcha: {e}") from e
