import hashlib
import base64
import json
import asyncio
from typing import Optional, TYPE_CHECKING

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
    def __init__(self, api_options: Optional[IApiOptions] = None):
        self.api_options = api_options
        self.api = API(self.api_options)
        self.known_captcha_types = ["slider", "checkbox"]

    async def get_captcha_params(self, validation_uri: str) -> tuple[str, str]:
        session_token, remixstlid = await self.api.get_session_data(validation_uri)
        captcha_uri = f"https://id.vk.com/not_robot_captcha?domain=vk.com&session_token={session_token}&variant=popup&blank=1&autofocus=1&origin=https%3A%2F%2Fm.vk.com"
        return captcha_uri, remixstlid

    async def solve(self, redirect_uri: str) -> str:
        initial = await self.api.get_initial_params(redirect_uri)

        captcha_type = initial["data"].get("show_captcha_type")
        if captcha_type not in self.known_captcha_types:
            await self.api.close()
            raise VKCaptchaSolverError(f"Unknown captcha type: {captcha_type}")

        settings = await self.api.get_settings(initial["data"])

        # Use executor for CPU-bound PoW if difficulty is high, but usually it's fast enough
        # To be truly async, we should offload it.
        loop = asyncio.get_running_loop()
        hash_val = await loop.run_in_executor(
            None, self.generate_pow, initial["powInput"], initial["difficulty"]
        )

        check_params: ICaptchaCheckParams = {
            "domain": initial["data"]["domain"],
            "session_token": initial["data"]["session_token"],
            "hash": hash_val,
            "answer": "e30=",  # base64 of "{}"
            "accelerometer": [],
            "cursor": [],
            "gyroscope": [],
            "motion": [],
            "taps": [],
        }

        if captcha_type == "checkbox":
            await self.api.component_done(initial["data"])

            solver = CheckboxCaptchaSolver()
            # Checkbox solver is CPU bound but fast
            params = solver.solve(settings.get("bridge_sensors_list", []))

            check_params.update(params)  # type: ignore
        else:
            content = await self.api.get_content(initial["data"])
            await self.api.component_done(initial["data"])

            slider_solver = SliderCaptchaSolver()
            # Image processing is CPU bound
            params = await loop.run_in_executor(None, slider_solver.solve, content)

            answer_json = json.dumps({"value": params.get("selectedSwaps")})
            answer_b64 = base64.b64encode(answer_json.encode("utf-8")).decode("utf-8")

            check_params["answer"] = answer_b64

        response = await self.api.check(check_params)
        await self.api.end_session(initial["data"])

        return response["success_token"]

    async def close(self):
        if self.api is None or self.api.closed:
            return
        await self.api.close()

    async def __aenter__(self):
        if self.api is None or self.api.closed:
            self.api = API(self.api_options)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def compute_hash_with_nonce(self, input_str: str, nonce: int) -> str:
        data = f"{input_str}{nonce}".encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def generate_pow(self, input_str: str, difficulty: int) -> str:
        nonce = 0
        hash_val = ""
        target_prefix = "0" * difficulty

        while not hash_val.startswith(target_prefix):
            nonce += 1
            hash_val = self.compute_hash_with_nonce(input_str, nonce)

        return hash_val

    async def vkbottle_validation_handler(self, error: "ValidationError") -> str:
        """
        Async handler compatible with vkbottle.
        """
        # replare url query params act=validate to act=captcha
        validation_uri = error.redirect_uri.replace("act=validate", "act=captcha")
        captcha_uri, remixstlid = await self.get_captcha_params(validation_uri)

        try:
            async with self as solver:
                success_token = await solver.solve(captcha_uri)
                await self.api.validate(validation_uri, success_token, remixstlid)
        except Exception as e:
            # Log error?
            print(f"Error solving activation in vkbottle handler: {e}")
            # Re-raise so vkbottle knows it failed?
            raise e

    async def vkbottle_captcha_handler(self, error: "CaptchaError") -> str:
        """
        Async handler compatible with vkbottle.
        """
        redirect_uri = error.redirect_uri

        try:
            async with self as solver:
                return await solver.solve(redirect_uri)
        except Exception as e:
            # Log error?
            print(f"Error solving captcha in vkbottle handler: {e}")
            # Re-raise so vkbottle knows it failed?
            raise e


__all__ = ["CaptchaSolver", "VKCaptchaSolverError", "APIError", "HTTPError"]
