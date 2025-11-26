import asyncio
import os
from vkbottle import API

from vk_captcha_solver import CaptchaSolver
from vkbottle_types.objects import GroupsFields, GroupsGroupFullMemberStatus


TOKEN = os.getenv("VK_API_TOKEN")
GROUP_IDS = os.getenv("VK_GROUP_IDS")

print(TOKEN, GROUP_IDS)

solver = CaptchaSolver()

vk_api = API(
    token=TOKEN,
    proxy=[],
)

vk_api.add_captcha_handler(solver.vkbottle_captcha_handler)

vk_api.add_validation_handler(solver.vkbottle_validation_handler)


async def main():
    for GROUP_ID in GROUP_IDS.split(","):
        try:
            group_info = await vk_api.groups.get_by_id(
                group_id=GROUP_ID,
                fields=[GroupsFields.IS_MEMBER, GroupsFields.MEMBER_STATUS],
            )

            if group_info and len(group_info.groups) > 0:
                group_data = group_info.groups[0]
                member_status = group_data.member_status

                if member_status in [
                    GroupsGroupFullMemberStatus.MEMBER,
                    GroupsGroupFullMemberStatus.HAS_SENT_A_REQUEST,
                ]:  # Already a member or requested to join
                    print(f"INFO    | group {GROUP_ID} is already a member")
                    continue

            response = await vk_api.groups.join(group_id=GROUP_ID)
            print(f"INFO    | response > {response}")
        except Exception as e:
            print(f"INFO    | error > {str(e)[:100]}...")
            continue


asyncio.run(main())
