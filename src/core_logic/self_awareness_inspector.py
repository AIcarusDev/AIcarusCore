# 文件路径: src/core_logic/self_awareness_inspector.py
import asyncio
from typing import TYPE_CHECKING, Any, Dict, Tuple

from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger
from src.database.services.person_storage_service import SELF_PERSON_ID

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.database.services.person_storage_service import PersonStorageService

logger = get_logger(__name__)


async def inspect_and_initialize_self_profile(
    person_service: "PersonStorageService", action_handler: "ActionHandler", platform_id: str
) -> Tuple[bool, Dict[str, Any] | None]:
    """
    检查并初始化机器人自身在特定平台上的档案。
    成功时返回 (True, profile_data)，失败时返回 (False, None)。
    """
    logger.info(f"--- 收到平台 '{platform_id}' 连接信号，开始自我客观信息检查 ---")

    # 1. 检查我是否已在数据库中登记
    persons_collection = await person_service._get_collection("persons")
    if await persons_collection.has(SELF_PERSON_ID):
        logger.info(f"核心档案 '{SELF_PERSON_ID}' 已存在。将直接返回现有档案。")
        # TODO: 应该从 person_service 获取已存在的 profile，而不是返回 None
        return True, {"status": "existing"}

    logger.info("未发现自身核心档案，启动首次检查流程。")

    logger.info(f"试图通过平台 '{platform_id}' 获取自身完整档案...")
    success, profile_data = await action_handler.execute_simple_action(
        platform_id=platform_id,
        action_name="get_bot_profile",
        params={},
        description="安检：获取机器人自身完整档案",
    )

    if not success or not profile_data or not isinstance(profile_data, dict):
        logger.critical(f"检查失败！无法从平台 '{platform_id}' 获取自身基础档案。返回: {profile_data}")
        return False, None

    bot_qq_id = profile_data.get("user_id")
    bot_nickname = profile_data.get("nickname")

    if not bot_qq_id or not bot_nickname:
        logger.critical(f"检查失败！适配器返回的档案不完整。ID: {bot_qq_id}, Nickname: {bot_nickname}")
        return False, None

    logger.success(f"获取到自身ID: {bot_qq_id}, 昵称: {bot_nickname}")

    bot_user_info = ProtocolUserInfo(user_id=str(bot_qq_id), user_nickname=bot_nickname)
    person_id, account_uid = await person_service._create_new_person_with_account(
        user_info=bot_user_info, platform=platform_id, is_self=True
    )

    if not person_id or not account_uid:
        logger.critical("检查失败！在数据库中创建自身 Person 或 Account 节点时失败。")
        return False, None

    group_list_data = profile_data.get("groups", {})
    if not isinstance(group_list_data, dict) or not group_list_data:
        logger.warning("自身档案中未包含任何群聊信息。")
        logger.info(f"--- 平台 '{platform_id}' 的自我检查完成（部分成功） ---")
        return True, profile_data

    logger.info(f"获取到 {len(group_list_data)} 个群聊的档案，开始更新群名片信息...")
    update_tasks = []
    for group_id, group_profile in group_list_data.items():
        if group_id and isinstance(group_profile, dict):
            task = person_service.update_robot_membership_in_conversation(
                account_uid=account_uid,
                conversation_id=str(group_id),
                platform=platform_id,
                conversation_name=group_profile.get("group_name"),
                card_name=group_profile.get("card"),
                role=group_profile.get("role"),
            )
            update_tasks.append(task)

    if update_tasks:
        await asyncio.gather(*update_tasks)

    logger.success("检查完成！所有群聊名片信息已更新。")
    logger.info(f"--- 在平台 '{platform_id}' 的自我客观信息检查圆满完成并记录 ---")
    return True, profile_data
