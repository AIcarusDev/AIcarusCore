# src/core_logic/self_awareness_inspector.py (重构后)
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

        # 如果存在，就获取所有自身实体，然后从中筛选出当前平台的实体
        all_self_entities = await entity_service.get_all_self_entities()

        existing_entity = next(
            (
                entity
                for entity in all_self_entities
                if entity.get("details", {}).get("platform")
                == platform_id  # 修复：从 details 中获取平台信息
            ),
            None,
        )
        if existing_entity:
            logger.success(f"成功从数据库为平台 '{platform_id}' 加载到自身客观实体信息。")
            # 基于已存在的实体信息构建返回数据
            entity_details = existing_entity.get("details", {})
            profile_data = {
                "user_id": entity_details.get("platform_id"),
                "nickname": entity_details.get("nickname"),
                "platform": platform_id,
                "groups": {},
                "status": "existing_and_loaded",
            }
            return True, profile_data

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

    # 获取到档案后，调用 _create_new_profile_with_account_entity 创建客观实体
    bot_user_info = ProtocolUserInfo(user_id=str(bot_platform_id_str), user_nickname=bot_nickname)

    profile_id, entity_uid = await entity_service._create_new_profile_with_account_entity(
        user_info=bot_user_info,
        platform=platform_id,
        is_self=True,
    )

    if not profile_id or not entity_uid:
        logger.critical("检查失败！在数据库中创建自身 Profile 或 Entity 节点时失败。")
        return False, None

    if not (group_list_data := profile_data.get("groups", {})) or not isinstance(
        group_list_data, dict
    ):
        logger.warning("自身档案中未包含任何群聊信息。")
        logger.info(f"--- 平台 '{platform_id}' 的自我检查完成（无群聊信息） ---")
        return True, profile_data

    logger.info(f"获取到 {len(group_list_data)} 个群聊的档案，开始更新存在关系及会话档案...")
    update_tasks = []

    await entity_service.get_or_create_platform_entity(platform_id)
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
                entity_service,
                entity_uid=entity_uid,
                conversation_id=str(group_id),
                platform=platform_id,
                group_profile=group_profile,
                bot_profile_for_conv=bot_profile_for_conv,
            )
            update_tasks.append(task)

    all_updates_successful = True
    if update_tasks:
        # 使用 return_exceptions=True 来捕获所有任务的结果
        results = await asyncio.gather(*update_tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                all_updates_successful = False
                # 从原始任务列表中找到对应的任务以获取上下文信息
                failed_task_coro = update_tasks[i]
                # 这是一种获取协程参数的方式，虽然有点 hack，但在这里很有效
                # 我们从协程的 frame 中查找局部变量
                try:
                    failed_group_id = failed_task_coro.cr_frame.f_locals.get(
                        "conversation_id", "未知"
                    )
                    logger.error(f"更新群聊 '{failed_group_id}' 的实体信息时失败: {result}")
                except AttributeError:
                    logger.error(f"更新一个群聊信息时失败: {result}")

    # 根据最终的成功状态来决定日志内容和返回值
    if all_updates_successful:
        logger.success("检查完成！所有群聊存在关系及会话档案已成功更新。")
        logger.info(f"--- 在平台 '{platform_id}' 的自我客观信息检查圆满完成并记录 ---")
        return True, profile_data
    else:
        logger.error("检查失败！在更新部分群聊信息时发生错误，请检查上面的日志。")
        logger.warning(f"--- 在平台 '{platform_id}' 的自我客观信息检查完成，但存在错误 ---")
        # 即使部分失败，基础档案还是获取到了，所以返回 True 和 profile_data，
        # 但日志会明确指出问题。
        return False, profile_data


async def _update_single_group_info(
    entity_service: "EntityGraphService",
    entity_uid: str,  # 这是“祂”自己的账户实体UID, e.g., "qq_123456"
    conversation_id: str,  # 这是群号, e.g., "98765"
    platform: str,
    group_profile: dict,
    bot_profile_for_conv: dict,
) -> None:
    """[重构后] 一个辅助函数，用于原子化地更新单个群聊的信息."""
    try:
        # 1. 获取或创建这个群聊的客观实体 (Entity)。
        #    这一步现在是健壮的，因为它内部已经解决了并发和事务问题。
        conversation_entity = await entity_service.get_or_create_conversation_entity(
            conversation_id=conversation_id,
            platform=platform,
            conv_type="group",
            name=group_profile.get("group_name"),
        )
        # 从返回的强类型对象中获取 _key
        conversation_entity_uid = conversation_entity._key

        # 2. 更新“祂”在这个会话实体中的存在关系 (is_present_in 边)。
        from aicarus_protocols import UserInfo as ProtocolUserInfo

        temp_user_info_for_edge = ProtocolUserInfo(
            user_cardname=group_profile.get("card"),
            permission_level=group_profile.get("role"),
        )
        await entity_service.update_presence_in_conversation(
            account_entity_uid=entity_uid,  # “祂”的账户实体
            conversation_entity_uid=conversation_entity_uid,  # 群聊的实体
            user_info=temp_user_info_for_edge,
            conversation_name=group_profile.get("group_name"),
        )

        # 3. 将“祂”在该群的具体档案，更新到“群聊实体”的文档中。
        entities_collection = await entity_service._get_collection(CoreDBCollections.ENTITIES)
        await entities_collection.update(
            {
                "_key": conversation_entity_uid,
                "bot_profile_in_this_conversation": bot_profile_for_conv,
            }
        )

    except Exception as e:
        # 向上抛出异常，让顶层的循环知道此任务失败
        logger.error(f"更新群聊 '{conversation_id}' 的实体信息时在底层失败: {e}", exc_info=True)
        raise e
