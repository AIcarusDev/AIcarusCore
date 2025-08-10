# src/core_logic/self_awareness_inspector.py
import asyncio
import time
from typing import TYPE_CHECKING, Any

from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger
from src.database.models import CoreDBCollections

# 导入新的服务和常量
from src.database.services.entity_graph_service import SELF_PROFILE_ID

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.database.services.entity_graph_service import EntityGraphService

logger = get_logger(__name__)


# --- [Refactored Helper 1] 检查现有档案 ---
async def _check_for_existing_profile(
    entity_service: "EntityGraphService", platform_id: str
) -> dict[str, Any] | None:
    """检查数据库中是否已存在该平台的自身档案."""
    profiles_collection = await entity_service._get_collection(CoreDBCollections.ENTITY_PROFILES)
    if not await profiles_collection.has(SELF_PROFILE_ID):
        return None

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
            "status": "existing_and_loaded",
        }
    return None


# --- [Refactored Helper 2] 从适配器获取新档案 ---
async def _fetch_new_profile_from_adapter(
    action_handler: "ActionHandler", platform_id: str
) -> dict[str, Any] | None:
    """通过适配器获取全新的自身档案."""
    logger.info(f"试图通过平台 '{platform_id}' 获取自身完整档案...")

    # [FIX START]
    # 不再尝试解包，而是接收完整的 ActionResult 对象
    action_result = await action_handler.execute_simple_action(
        platform_id=platform_id,
        action_name="get_bot_profile",
        params={},
        bot_id="pending_inspection",
        description="安检：获取祂自身的完整档案",
    )

    # 从 ActionResult 对象的属性中获取成功状态和载荷数据
    success = action_result.is_success
    profile_data = action_result.payload

    if not success or not isinstance(profile_data, dict):
        logger.critical(f"检查失败！无法从平台 '{platform_id}' 获取档案。返回: {profile_data}")
        return None
    # [FIX END]

    # --- [采纳] 使用命名表达式简化赋值和检查 ---
    if not (bot_platform_id := profile_data.get("user_id")) or not (
        bot_nickname := profile_data.get("nickname")
    ):
        logger.critical(
            f"检查失败！适配器返回的档案不完整。ID: {bot_platform_id}, Nickname: {bot_nickname}"
        )
        return None

    logger.success(f"获取到自身ID: {bot_platform_id}, 昵称: {bot_nickname}")
    return profile_data


# --- [Refactored Helper 3] 将新档案持久化到数据库 ---
async def _persist_new_profile(
    entity_service: "EntityGraphService", platform_id: str, profile_data: dict[str, Any]
) -> str | None:
    """将从适配器获取的新档案写入数据库."""
    bot_user_info = ProtocolUserInfo(
        user_id=str(profile_data["user_id"]), user_nickname=profile_data["nickname"]
    )
    _, entity_uid = await entity_service.create_new_profile_with_account_entity(
        user_info=bot_user_info, platform=platform_id, is_self=True
    )
    if not entity_uid:
        logger.critical("检查失败！在数据库中创建自身 Profile 或 Entity 节点时失败。")
        return None
    return entity_uid


# --- [Refactored Helper 4] 更新群聊关系 ---
async def _update_group_memberships(
    entity_service: "EntityGraphService", entity_uid: str, platform_id: str, profile_data: dict
) -> bool:
    """并发地更新所有群聊的存在关系和会话档案."""
    # --- [采纳] 使用命名表达式简化赋值和检查 ---
    if not (group_list_data := profile_data.get("groups")) or not isinstance(group_list_data, dict):
        logger.warning("自身档案中未包含任何群聊信息，跳过群聊更新。")
        return True

    logger.info(f"获取到 {len(group_list_data)} 个群聊档案，开始并发更新...")
    tasks = [
        _update_single_group_info(
            entity_service,
            entity_uid=entity_uid,
            conversation_id=str(group_id),
            platform=platform_id,
            group_profile=group_profile,
            bot_profile_for_conv={
                "user_id": str(profile_data["user_id"]),
                "nickname": profile_data["nickname"],
                "card": group_profile.get("card"),
                "role": group_profile.get("role"),
                "platform": platform_id,
                "updated_at": int(time.time() * 1000),
            },
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


# --- 主函数 (重构后，现在是清晰的编排者) ---
async def inspect_and_initialize_self_profile(
    entity_service: "EntityGraphService",
    action_handler: "ActionHandler",
    platform_id: str,
) -> tuple[bool, dict[str, Any] | None]:
    """(重构后) 编排检查和初始化自身档案的流程."""
    logger.info(f"--- 开始对平台 '{platform_id}' 进行自我客观信息检查 ---")

    # 1. 尝试从数据库加载现有档案
    if existing_profile := await _check_for_existing_profile(entity_service, platform_id):
        return True, existing_profile

    # 2. 如果不存在，则从适配器获取新档案
    logger.info("未发现本地档案，启动首次检查流程。")
    if not (new_profile_data := await _fetch_new_profile_from_adapter(action_handler, platform_id)):
        return False, None

    # 3. 将新档案持久化到数据库
    if not (
        entity_uid := await _persist_new_profile(entity_service, platform_id, new_profile_data)
    ):
        return False, None

    # 4. 更新群聊关系（这是一个可选的增强步骤）
    await _update_group_memberships(entity_service, entity_uid, platform_id, new_profile_data)

    logger.info(f"--- 平台 '{platform_id}' 的自我客观信息检查圆满完成并记录 ---")
    return True, new_profile_data


async def _update_single_group_info(
    entity_service: "EntityGraphService",
    entity_uid: str,
    conversation_id: str,
    platform: str,
    group_profile: dict,
    bot_profile_for_conv: dict,
) -> None:
    """一个辅助函数，用于原子化地更新单个群聊的信息."""
    try:
        conversation_entity = await entity_service.get_or_create_conversation_entity(
            conversation_id=conversation_id,
            platform=platform,
            conv_type="group",
            name=group_profile.get("group_name"),
        )
        conversation_entity_uid = conversation_entity._key

        temp_user_info_for_edge = ProtocolUserInfo(
            user_cardname=group_profile.get("card"),
            permission_level=group_profile.get("role"),
        )
        await entity_service.update_presence_in_conversation(
            account_entity_uid=entity_uid,
            conversation_entity_uid=conversation_entity_uid,
            user_info=temp_user_info_for_edge,
            conversation_name=group_profile.get("group_name"),
        )

        entities_collection = await entity_service._get_collection(CoreDBCollections.ENTITIES)
        await entities_collection.update(
            {
                "_key": conversation_entity_uid,
                "bot_profile_in_this_conversation": bot_profile_for_conv,
            }
        )
    except Exception as e:
        logger.error(f"更新群聊 '{conversation_id}' 的实体信息时在底层失败: {e}", exc_info=True)
        raise e
