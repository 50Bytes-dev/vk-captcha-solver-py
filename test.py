import asyncio
import os
from vkbottle import API

from vk_captcha_solver import CaptchaSolver


TOKEN = os.getenv("VK_API_TOKEN")
GROUP_IDS = os.getenv("VK_GROUP_IDS")

print(TOKEN, GROUP_IDS)

solver = CaptchaSolver(
    api_options={"delays": {"between_requests": 0, "after_solve": 0}}
)
vk_api = API(token=TOKEN)
vk_api.add_captcha_handler(solver.vkbottle_captcha_handler)
vk_api.add_validation_handler(solver.vkbottle_validation_handler)


async def main():
    for GROUP_ID in GROUP_IDS.split(","):
        response = await vk_api.groups.join(group_id=GROUP_ID)
        print(response)


asyncio.run(main())
