# 文件路径: src/core_logic/self_awareness_inspector.py (重构后)
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

    # 引用新的服务类
    from src.database.services.entity_graph_service import EntityGraphService

logger = get_logger(__name__)


async def inspect_and_initialize_self_profile(
    entity_service: "EntityGraphService",  # <--- 参数名和类型已更改
    action_handler: "ActionHandler",
    platform_id: str,
) -> tuple[bool, dict[str, Any] | None]:
    """检查并初始化祂自身在特定平台上的客观实体与主观侧写.

    Args:
        entity_service (EntityGraphService): 用于与数据库实体图谱交互的服务实例.
        action_handler (ActionHandler): 用于执行获取自身档案的动作处理器.
        platform_id (str): 需要检查的目标平台ID.

    Returns:
        tuple: 包含两个元素的元组，第一个是布尔值表示检查是否成功，
            第二个是包含自身档案信息的字典或None.
    """
    logger.info(f"--- 收到平台 '{platform_id}' 连接信号，开始自我客观信息检查 ---")

    # 1. 检查我们自己的核心Profile (`aic_person_0`) 是否已存在
    profiles_collection = await entity_service._get_collection(CoreDBCollections.ENTITY_PROFILES)
    if await profiles_collection.has(SELF_PROFILE_ID):
        logger.info(f"核心 Profile '{SELF_PROFILE_ID}' 已存在。将从数据库加载现有档案。")

        # 如果存在，就检查是否已经有关联的该平台的客观实体
        existing_entity = await entity_service.get_self_entity_for_platform(platform_id)
        if existing_entity:
            logger.success(f"成功从数据库为平台 '{platform_id}' 加载到自身客观实体信息。")
            # 基于已存在的实体信息构建返回数据
            profile_data = {
                "user_id": existing_entity.get("platform_id"),
                "nickname": existing_entity.get("nickname"),
                "platform": platform_id,
                "groups": {},  # 注意：此处未加载群组信息，可根据需要扩展
                "status": "existing_and_loaded",
            }
            return True, profile_data
        else:
            logger.warning(
                f"数据库中存在核心Profile，但未找到平台 '{platform_id}' 的实体信息。"
                f"将尝试重新获取。"
            )

    logger.info("未发现自身核心Profile或特定平台实体，启动首次检查流程。")

    logger.info(f"试图通过平台 '{platform_id}' 获取自身完整档案...")
    success, profile_data = await action_handler.execute_simple_action(
        platform_id=platform_id,
        action_name="get_bot_profile",
        params={},
        bot_id="pending_inspection",
        description="安检：获取祂自身的完整档案",
    )

    if not success or not profile_data or not isinstance(profile_data, dict):
        logger.critical(
            f"检查失败！无法从平台 '{platform_id}' 获取自身基础档案。返回: {profile_data}"
        )
        return False, None

    bot_platform_id_str = profile_data.get("user_id")
    bot_nickname = profile_data.get("nickname")

    if not bot_platform_id_str or not bot_nickname:
        logger.critical(
            f"检查失败！适配器返回的档案不完整。ID: {bot_platform_id_str}, Nickname: {bot_nickname}"
        )
        return False, None

    logger.success(f"获取到自身ID: {bot_platform_id_str}, 昵称: {bot_nickname}")

    # [核心修改]
    # 获取到档案后，调用 _create_new_profile_with_entity 创建客观实体，并将其与 SELF_PROFILE_ID 关联
    bot_user_info = ProtocolUserInfo(user_id=str(bot_platform_id_str), user_nickname=bot_nickname)
    profile_id, entity_uid = await entity_service._create_new_profile_with_entity(
        user_info=bot_user_info,
        platform=platform_id,
        is_self=True,  # <--- 关键！这会使用 SELF_PROFILE_ID
    )

    if not profile_id or not entity_uid:
        logger.critical("检查失败！在数据库中创建自身 Profile 或 Entity 节点时失败。")
        return False, None

    if not (group_list_data := profile_data.get("groups", {})) or not isinstance(
        group_list_data, dict
    ):
        logger.warning("自身档案中未包含任何群聊信息。")
        logger.info(f"--- 平台 '{platform_id}' 的自我检查完成（部分成功） ---")
        return True, profile_data

    logger.info(f"获取到 {len(group_list_data)} 个群聊的档案，开始更新存在关系及会话档案...")
    update_tasks = []
    for group_id, group_profile in group_list_data.items():
        if group_id and isinstance(group_profile, dict):
            bot_profile_for_conv = {
                "user_id": str(bot_platform_id_str),
                "nickname": bot_nickname,
                "card": group_profile.get("card"),
                "role": group_profile.get("role"),
                "platform": platform_id,
                "updated_at": int(time.time() * 1000),
            }
            task = _update_single_group_info(
                entity_service,  # <--- 传递新的服务实例
                entity_uid=entity_uid,  # <--- 传递 entity_uid
                conversation_id=str(group_id),
                platform=platform_id,
                group_profile=group_profile,
                bot_profile_for_conv=bot_profile_for_conv,
            )
            update_tasks.append(task)

    if update_tasks:
        await asyncio.gather(*update_tasks)

    logger.success("检查完成！所有群聊存在关系及会话档案已更新。")
    logger.info(f"--- 在平台 '{platform_id}' 的自我客观信息检查圆满完成并记录 ---")
    return True, profile_data


async def _update_single_group_info(
    entity_service: "EntityGraphService",  # <--- 参数名和类型已更改
    entity_uid: str,  # <--- 参数名已更改
    conversation_id: str,
    platform: str,
    group_profile: dict,
    bot_profile_for_conv: dict,
) -> None:
    """一个辅助函数，用于原子化地更新单个群聊的信息."""
    # 1. 更新 'is_present_in' 关系边
    # 调用新的方法 update_robot_presence_in_conversation
    await entity_service.update_robot_presence_in_conversation(
        entity_uid=entity_uid,  # <--- 传递 entity_uid
        conversation_id=conversation_id,
        platform=platform,
        conversation_name=group_profile.get("group_name"),
        card_name=group_profile.get("card"),
        role=group_profile.get("role"),
    )
    # 2. 将机器人的档案直接更新到会话文档中
    await entity_service.conn_manager.db.collection(CoreDBCollections.CONVERSATIONS).update(
        {"_key": conversation_id, "bot_profile_in_this_conversation": bot_profile_for_conv}
    )
