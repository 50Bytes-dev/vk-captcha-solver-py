import asyncio
from vkbottle import API

from vk_captcha_solver import CaptchaSolver


TOKEN = "..."
GROUP_ID = 1


solver = CaptchaSolver()
vk_api = API(token=TOKEN)
vk_api.add_captcha_handler(solver.vkbottle_captcha_handler)
vk_api.add_validation_handler(solver.vkbottle_validation_handler)


async def main():
    response = await vk_api.groups.join(group_id=GROUP_ID)
    print(response)


asyncio.run(main())
