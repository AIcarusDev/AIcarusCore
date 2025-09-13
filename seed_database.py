# 文件路径: seed_database.py

import asyncio
import random
import time
from typing import Any

from aicarus_protocols import Event as ProtocolEvent
from aicarus_protocols import SegBuilder
from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.bootstrap.builder import ServiceBuilder
from src.common.utils import build_conversation_entity_uid
from src.services.database import EntityGraphService, EventStorageService
from typedb.driver import TransactionType

# --- [新增] 可配置的数据生成参数 ---
NUM_FRIENDS = 35  # 确保超过一页
NUM_GROUPS = 25  # 确保超过一页
LONG_CHAT_MESSAGES = 50  # 确保超过一页
BOT_USER_ID = "10001"
BOT_NICKNAME = "霜"
# ------------------------------------

ENTITY_TYPES_TO_DELETE = [
    "aic_self",
    "external_person",
    "account",
    "conversation",
    "platform",
    "sticker",
    "event",
    "thought-chain-node",
    "action-log",
    "system-pointer",
    "intrusive-thought",
    "image-cache",
    "goal",
    "friendship",
    "membership",
]


async def clear_database(container: Any) -> bool | None:
    """清空数据库中的所有测试数据."""
    print("--- 正在清空数据库 ---")
    driver = container.conn_manager.get_driver()
    db_name = container.conn_manager.database_name

    def db_write() -> None:
        with driver.transaction(db_name, TransactionType.WRITE) as tx:
            for entity_type in ENTITY_TYPES_TO_DELETE:
                print(f"  - 正在删除所有 '{entity_type}' 实体及关系...")
                delete_query = f"match $e isa {entity_type}; delete $e;"
                tx.query(delete_query).resolve()
            tx.commit()

    try:
        await asyncio.to_thread(db_write)
        print("--- 数据库清空完毕 ---")
        return True
    except Exception as e:
        print(f"!!! 数据库清空失败: {e} !!!")
        return False


async def seed_data(entity_service: EntityGraphService, event_service: EventStorageService) -> None:
    """注入丰富且复杂的模拟数据."""
    print("--- 开始注入模拟数据 ---")
    base_timestamp = int(time.time() * 1000)

    # --- Step 1: 创建核心实体 (机器人自己 和 QQ平台) ---
    print("Step 1: 创建核心实体...")
    await entity_service.get_or_create_platform_entity("qq", "QQ")
    _, self_account_uid = await entity_service.create_new_profile_with_account_entity(
        ProtocolUserInfo(user_id=BOT_USER_ID, user_nickname=BOT_NICKNAME), "qq", is_self=True
    )
    if not self_account_uid:
        print("!!! 致命错误: 无法创建机器人自身实体，注入终止。")
        return
    print(f"  - 核心实体创建完毕 (UID: {self_account_uid})")

    # --- Step 2: 批量创建好友并建立关系 ---
    print(f"Step 2: 批量创建 {NUM_FRIENDS} 位好友...")
    friend_tasks = [
        entity_service.find_or_create_profile_and_account_entity(
            ProtocolUserInfo(user_id=f"friend_{i:03d}", user_nickname=f"好友{i:03d}"), "qq"
        )
        for i in range(NUM_FRIENDS)
    ]
    friend_results = await asyncio.gather(*friend_tasks)
    friend_uids = [res[1] for res in friend_results if res and res[1]]
    if friend_uids:
        await entity_service.establish_friendships(self_account_uid, friend_uids)
        print(f"  - 已成功创建 {len(friend_uids)} 位好友并建立关系。")

    # --- Step 3: 批量创建群聊, 并注入少量消息以产生会话记录 ---
    print(f"Step 3: 批量创建 {NUM_GROUPS} 个群聊并注入最新消息...")
    for i in range(NUM_GROUPS):
        group_id = f"group_{i:03d}"
        group_name = f"测试群聊 {i:03d}"
        group_conv_uid = build_conversation_entity_uid("qq", "group", group_id)

        # 为每个群创建一个模拟成员
        _, member_uid = await entity_service.create_new_profile_with_account_entity(
            ProtocolUserInfo(user_id=f"member_in_group_{i:03d}", user_nickname=f"群友{i:03d}"), "qq"
        )

        # 创建群聊实体
        await entity_service.get_or_create_conversation_entity(
            conversation_id=group_id, platform="qq", conv_type="group", name=group_name
        )
        # 将机器人和模拟成员加入群聊
        await entity_service.update_presence_in_conversation(
            self_account_uid, group_conv_uid, ProtocolUserInfo(user_cardname=f"BotCard-{i:03d}")
        )
        if member_uid:
            await entity_service.update_presence_in_conversation(
                member_uid, group_conv_uid, ProtocolUserInfo()
            )

        # 注入一条最新消息，时间戳递减，确保列表排序正确
        msg_timestamp = base_timestamp - (i * 60000)  # 每隔1分钟
        event = ProtocolEvent.from_dict(
            {
                "event_id": f"group_seed_{i}",
                "event_type": "message.qq.group",
                "time": msg_timestamp,
                "bot_id": BOT_USER_ID,
                "user_info": {
                    "user_id": f"member_in_group_{i:03d}",
                    "user_nickname": f"群友{i:03d}",
                },
                "conversation_info": {"conversation_id": group_id, "type": "group"},
                "content": [SegBuilder.text(f"这是群 {i:03d} 的最新消息。").to_dict()],
            }
        )
        event_dict = event.to_dict()
        event_dict["platform"] = "qq"
        await event_service.save_event_document(event_dict)
    print(f"  - 已成功创建 {NUM_GROUPS} 个群聊会话。")

    # --- Step 4: 创建一个包含大量聊天记录的私聊，用于测试分页 ---
    print(f"Step 4: 创建一个包含 {LONG_CHAT_MESSAGES} 条消息的私聊...")
    if friend_uids:
        long_chat_friend_uid = friend_uids[0]
        long_chat_friend_id = long_chat_friend_uid.split("_", 1)[1]
        long_chat_conv_uid = build_conversation_entity_uid("qq", "private", long_chat_friend_id)

        await entity_service.get_or_create_conversation_entity(
            conversation_id=long_chat_friend_id, platform="qq", conv_type="private", name="好友000"
        )

        for i in range(LONG_CHAT_MESSAGES):
            # 随机决定发言人
            sender_id = random.choice([BOT_USER_ID, long_chat_friend_id])
            sender_name = BOT_NICKNAME if sender_id == BOT_USER_ID else "好友000"

            # 注入一条最新消息，时间戳递增，让这个会话排在最前面
            msg_timestamp = base_timestamp + (i * 1000)  # 每隔1秒

            content = [
                SegBuilder.text(f"这是长对话的第 {i + 1}/{LONG_CHAT_MESSAGES} 条消息。").to_dict()
            ]
            # 在倒数第二条消息中@机器人，测试高优提醒
            if i == LONG_CHAT_MESSAGES - 2:
                sender_id = long_chat_friend_id
                sender_name = "好友000"
                content = [
                    SegBuilder.text("最后提醒一下，记得看这个！").to_dict(),
                    SegBuilder.at(BOT_USER_ID, BOT_NICKNAME).to_dict(),
                ]

            event = ProtocolEvent.from_dict(
                {
                    "event_id": f"long_chat_seed_{i}",
                    "event_type": "message.qq.private",
                    "time": msg_timestamp,
                    "bot_id": BOT_USER_ID,
                    "user_info": {"user_id": sender_id, "user_nickname": sender_name},
                    "conversation_info": {
                        "conversation_id": long_chat_friend_id,
                        "type": "private",
                    },
                    "content": content,
                }
            )
            event_dict = event.to_dict()
            event_dict["platform"] = "qq"
            await event_service.save_event_document(event_dict)
        print(f"  - 长对话注入完成，会话UID: {long_chat_conv_uid}")

    # 注意：我们不再手动设置 last_read_timestamp，让系统根据默认值 (0) 和最新消息时间戳
    # 自动计算出正确的未读状态。这更接近真实情况。
    print("--- 模拟数据注入完成 ---")


async def main() -> None:
    """主函数，构建服务并执行注入."""
    builder = ServiceBuilder()
    container = await builder.build_container()

    if not container.entity_graph_service or not container.event_storage_service:
        raise RuntimeError("数据库服务初始化失败！")

    if not await clear_database(container):
        print("数据库清空失败，注入终止。")
        return

    await seed_data(container.entity_graph_service, container.event_storage_service)
    await container.conn_manager.close_client()


if __name__ == "__main__":
    asyncio.run(main())
