# src/os/apps/qq/qq_inspection_service.py

import asyncio
from typing import TYPE_CHECKING, Any

from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger

if TYPE_CHECKING:
    from src.services.action.action_handler import ActionHandler
    from src.services.database.services.entity_graph_service import EntityGraphService

logger = get_logger(__name__)

# --- 辅助函数 1: 检查现有档案 ---
async def _check_for_existing_profile(
    entity_service: "EntityGraphService", platform_id: str
) -> dict[str, Any] | None:
    all_self_entities = await entity_service.get_all_self_entities()
    if existing_entity := next(
        (e for e in all_self_entities if e.get("details", {}).get("platform") == platform_id),
        None,
    ):
        logger.success(f"成功从数据库为平台 '{platform_id}' 加载到自身客观实体信息。")
        details = existing_entity.get("details", {})
        return {
            "user_id": details.get("platform_id"),
            "nickname": details.get("nickname"),
            "platform": platform_id,
            "groups": {},
            "friends": [],
            "status": "existing_and_loaded",
        }
    return None

# --- 辅助函数 2: 从适配器获取新档案 ---
async def _fetch_new_profile_from_adapter(
    action_handler: "ActionHandler", platform_id: str
) -> dict[str, Any] | None:
    logger.info(f"试图通过平台 '{platform_id}' 获取自身完整档案...")
    action_result = await action_handler.execute_simple_action(
        platform_id=platform_id,
        action_name="get_bot_profile",
        params={},
        bot_id="pending_inspection",
        description="安检：获取祂自身的完整档案",
    )
    if not action_result.is_success or not isinstance(action_result.payload, dict):
        logger.critical(
            f"检查失败！无法从平台 '{platform_id}' 获取档案。返回: {action_result.payload}"
            )
        return None

    profile_data = action_result.payload
    if not profile_data.get("user_id") or not profile_data.get("nickname"):
        logger.critical("检查失败！适配器返回的档案不完整。")
        return None

    logger.success(f"获取到自身ID: {profile_data['user_id']}, 昵称: {profile_data['nickname']}")
    return profile_data

# --- 辅助函数 3: 持久化新档案 ---
async def _persist_new_profile(
    entity_service: "EntityGraphService", platform_id: str, profile_data: dict[str, Any]
) -> str | None:
    bot_user_info = ProtocolUserInfo(
        user_id=str(profile_data["user_id"]), user_nickname=profile_data["nickname"]
    )
    _, entity_uid = await entity_service.create_new_profile_with_account_entity(
        user_info=bot_user_info, platform=platform_id, is_self=True
    )
    if not entity_uid:
        logger.critical("检查失败！在数据库中创建自身 Profile 或 Entity 节点时失败。")
    return entity_uid

# --- 辅助函数 4: 更新群聊关系 ---
async def _update_group_memberships(
    entity_service: "EntityGraphService",
    self_account_uid: str,
    platform_id: str,
    profile_data: dict
) -> bool:
    if not (group_list_data := profile_data.get("groups")) or not isinstance(group_list_data, dict):
        logger.info("自身档案中未包含任何群聊信息，跳过群聊更新。")
        return True

    logger.info(f"获取到 {len(group_list_data)} 个群聊档案，开始并发更新...")
    tasks = [
        _update_single_group_info(
            entity_service,
            self_account_uid,
            str(group_id),
            platform_id,
            group_profile
        )
        for group_id, group_profile in group_list_data.items()
        if group_id and isinstance(group_profile, dict)
    ]
    if not tasks:
        return True

    results = await asyncio.gather(*tasks, return_exceptions=True)
    if any(isinstance(res, Exception) for res in results):
        logger.error("在更新部分群聊信息时发生错误，请检查日志。")
        return False

    logger.success("所有群聊存在关系及会话档案已成功更新。")
    return True

async def _update_single_group_info(
    entity_service: "EntityGraphService",
    entity_uid: str,
    conversation_id: str,
    platform: str,
    group_profile: dict
) -> None:
    try:
        conversation_entity = await entity_service.get_or_create_conversation_entity(
            conversation_id=conversation_id,
            platform=platform,
            conv_type="group",
            name=group_profile.get("group_name"),
        )

        if not conversation_entity or not conversation_entity._key:
            logger.error(f"为群聊 {conversation_id} 获取或创建实体失败，跳过更新。")
            return

        temp_user_info = ProtocolUserInfo(
            user_cardname=group_profile.get("card"),
            permission_level=group_profile.get("role"),
        )
        await entity_service.update_presence_in_conversation(
            account_entity_uid=entity_uid,
            conversation_entity_uid=conversation_entity._key,
            user_info=temp_user_info,
        )

    except Exception as e:
        logger.error(f"更新群聊 '{conversation_id}' 的实体信息时在底层失败: {e}", exc_info=True)
        raise e

# --- 辅助函数 5: 持久化好友列表 ---
async def _persist_friends(
    entity_service: "EntityGraphService",
    self_account_uid: str,
    platform_id: str,
    profile_data: dict
) -> bool:
    if not (friend_list := profile_data.get("friends")) or not isinstance(friend_list, list):
        logger.info("自身档案中未包含好友列表信息，跳过好友关系更新。")
        return True

    logger.info(f"获取到 {len(friend_list)} 位好友，开始并发更新好友关系...")
    tasks = []
    for friend_data in friend_list:
        if not isinstance(friend_data, dict) or not friend_data.get("user_id"):
            continue

        friend_user_info = ProtocolUserInfo(
            user_id=str(friend_data["user_id"]),
            user_nickname=friend_data.get("nickname"),
            friend_remark=friend_data.get("remark"),
        )
        # 这会并发地创建所有好友的实体
        task = entity_service.find_or_create_profile_and_account_entity(
            user_info=friend_user_info, platform=platform_id
        )
        tasks.append(task)

    if not tasks:
        return True

    friend_entity_results = await asyncio.gather(*tasks, return_exceptions=True)

    friend_account_uids = [
        res[1] for res in friend_entity_results
        if isinstance(res, tuple) and len(res) > 1 and res[1]
    ]

    if friend_account_uids:
        success = await entity_service.establish_friendships(self_account_uid, friend_account_uids)
        if success:
            logger.success(f"已成功建立或确认了与 {len(friend_account_uids)} 位好友的关系。")
        else:
            logger.error("在批量建立好友关系时发生错误。")
        return success

    return True

# --- 主函数 (编排者) ---
async def inspect_and_initialize_self_profile(
    entity_service: "EntityGraphService",
    action_handler: "ActionHandler",
    platform_id: str,
) -> tuple[bool, dict[str, Any] | None]:
    """编排检查和初始化QQ平台自身档案的流程."""
    logger.info(f"--- [QQ App] 开始对平台 '{platform_id}' 进行自我客观信息检查 ---")

    if existing_profile := await _check_for_existing_profile(entity_service, platform_id):
        return True, existing_profile

    logger.info("未发现本地档案，启动首次检查流程。")
    if not (new_profile_data := await _fetch_new_profile_from_adapter(action_handler, platform_id)):
        return False, None

    if not (self_account_uid := await _persist_new_profile(
        entity_service, platform_id, new_profile_data
        )):
        return False, None

    group_task = _update_group_memberships(
        entity_service, self_account_uid, platform_id, new_profile_data
    )
    friend_task = _persist_friends(entity_service, self_account_uid, platform_id, new_profile_data)
    await asyncio.gather(group_task, friend_task)

    logger.info(f"--- 平台 '{platform_id}' 的自我客观信息检查圆满完成并记录 ---")
    return True, new_profile_data
