# 文件路径: src/core_logic/self_awareness_inspector.py (修复版)
import asyncio
import time
from typing import TYPE_CHECKING, Any

from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger
from src.database.models import CoreDBCollections
from src.database.services.person_storage_service import SELF_PERSON_ID

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.database.services.person_storage_service import PersonStorageService

logger = get_logger(__name__)


async def inspect_and_initialize_self_profile(
    person_service: "PersonStorageService", action_handler: "ActionHandler", platform_id: str
) -> tuple[bool, dict[str, Any] | None]:
    """检查并初始化祂自身在特定平台上的档案.

    Args:
        person_service (PersonStorageService): 用于与数据库交互的服务实例.
        action_handler (ActionHandler): 用于执行获取自身档案的动作处理器.
        platform_id (str): 需要检查的目标平台ID.

    Returns:
        tuple: 包含两个元素的元组，第一个是布尔值表示检查是否成功，
            第二个是包含自身档案信息的字典或None.
    """
    logger.info(f"--- 收到平台 '{platform_id}' 连接信号，开始自我客观信息检查 ---")

    # 1. 检查我是否已在数据库中登记
    persons_collection = await person_service._get_collection("persons")
    if await persons_collection.has(SELF_PERSON_ID):
        logger.info(f"核心档案 '{SELF_PERSON_ID}' 已存在。将从数据库加载现有档案。")

        all_accounts = await person_service.get_all_self_accounts()
        target_account = next(
            (acc for acc in all_accounts if acc.get("platform") == platform_id), None
        )

        if target_account:
            logger.success(f"成功从数据库为平台 '{platform_id}' 加载到自身账户信息。")
            # 构造一个和首次安检时结构一致的返回字典
            # 注意：这个返回不包含群列表，因为我们假设群信息会通过其他方式（如通知）更新
            # 如果需要，这里也可以加入获取群列表的逻辑
            profile_data = {
                "user_id": target_account.get("platform_id"),
                "nickname": target_account.get("nickname"),
                "platform": platform_id,
                "groups": {},  # 非首次启动，暂时不获取群列表，依赖后续更新
                "status": "existing_and_loaded",
            }
            return True, profile_data
        else:
            # 这种情况比较少见，比如数据库有person但没有这个平台的account
            logger.warning(
                f"数据库中存在核心档案，但未找到平台 '{platform_id}' 的账户信息。将尝试重新获取。"
            )
            # 继续执行下面的首次检查流程

    logger.info("未发现自身核心档案或特定平台档案，启动首次检查流程。")

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

    bot_qq_id = profile_data.get("user_id")
    bot_nickname = profile_data.get("nickname")

    if not bot_qq_id or not bot_nickname:
        logger.critical(
            f"检查失败！适配器返回的档案不完整。ID: {bot_qq_id}, Nickname: {bot_nickname}"
        )
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

    logger.info(f"获取到 {len(group_list_data)} 个群聊的档案，开始更新群名片及会话档案...")
    update_tasks = []
    # 遍历所有群聊，更新群名片和会话档案
    for group_id, group_profile in group_list_data.items():
        if group_id and isinstance(group_profile, dict):
            # 构建一个完整的、将要存入会话文档的机器人档案
            bot_profile_for_conv = {
                "user_id": str(bot_qq_id),
                "nickname": bot_nickname,
                "card": group_profile.get("card"),
                "role": group_profile.get("role"),
                "platform": platform_id,
                "updated_at": int(time.time() * 1000),
            }

            # 创建一个异步任务来处理单个群聊的所有更新
            task = _update_single_group_info(
                person_service,
                account_uid=account_uid,
                conversation_id=str(group_id),
                platform=platform_id,
                group_profile=group_profile,
                bot_profile_for_conv=bot_profile_for_conv,  # 将完整的档案传进去
            )
            update_tasks.append(task)

    if update_tasks:
        await asyncio.gather(*update_tasks)

    logger.success("检查完成！所有群聊名片信息及会话档案已更新。")
    logger.info(f"--- 在平台 '{platform_id}' 的自我客观信息检查圆满完成并记录 ---")
    return True, profile_data


async def _update_single_group_info(
    person_service: "PersonStorageService",
    account_uid: str,
    conversation_id: str,
    platform: str,
    group_profile: dict,
    bot_profile_for_conv: dict,
) -> None:
    """一个辅助函数，用于原子化地更新单个群聊的信息."""
    # 1. 更新成员关系边
    await person_service.update_robot_membership_in_conversation(
        account_uid=account_uid,
        conversation_id=conversation_id,
        platform=platform,
        conversation_name=group_profile.get("group_name"),
        card_name=group_profile.get("card"),
        role=group_profile.get("role"),
    )
    # 2. 将机器人的档案直接更新到会话文档中
    await person_service.conn_manager.db.collection(CoreDBCollections.CONVERSATIONS).update(
        {"_key": conversation_id, "bot_profile_in_this_conversation": bot_profile_for_conv}
    )
