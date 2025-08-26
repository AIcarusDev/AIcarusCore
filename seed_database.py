# seed_database.py
import asyncio
import time
from typing import Any

from aicarus_protocols import Event as ProtocolEvent
from aicarus_protocols import SegBuilder
from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.bootstrap.builder import ServiceBuilder
from src.common.utils import build_conversation_entity_uid
from src.database import EntityGraphService, EventStorageService
from typedb.driver import TransactionType

# --- [新增] 定义所有需要被清空的实体类型 ---
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
]
# ---------------------------------------------


async def clear_database(container: Any) -> bool | None:
    """清空数据库中的所有测试数据."""
    print("--- 正在清空数据库 ---")
    driver = container.conn_manager.get_driver()
    db_name = container.conn_manager.database_name

    def db_write() -> None:
        with driver.transaction(db_name, TransactionType.WRITE) as tx:
            for entity_type in ENTITY_TYPES_TO_DELETE:
                print(f"  - 正在删除所有 '{entity_type}' 实体...")
                # TypeDB 中，删除实体会自动删除与之关联的关系
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
    """注入模拟数据的核心函数."""
    print("--- 开始注入模拟数据 ---")

    # 1. 创建实体 (机器人自己, 朋友A, 群B, 群成员C)
    print("Step 1: 创建实体...")
    await entity_service.get_or_create_platform_entity("qq", "QQ")
    _, self_account_uid = await entity_service.create_new_profile_with_account_entity(
        ProtocolUserInfo(user_id="10001", user_nickname="霜"), "qq", is_self=True
    )
    _, friend_a_uid = await entity_service.create_new_profile_with_account_entity(
        ProtocolUserInfo(user_id="20002", user_nickname="未来星"), "qq"
    )
    _, member_c_uid = await entity_service.create_new_profile_with_account_entity(
        ProtocolUserInfo(user_id="40004", user_nickname="Gemini"), "qq"
    )

    # 创建会话实体
    private_conv_uid = build_conversation_entity_uid("qq", "private", "20002")
    group_conv_uid = build_conversation_entity_uid("qq", "group", "30003")

    await entity_service.get_or_create_conversation_entity(
        conversation_id="20002", platform="qq", conv_type="private", name="未来星"
    )
    await entity_service.get_or_create_conversation_entity(
        conversation_id="30003", platform="qq", conv_type="group", name="开发交流群"
    )

    print(
        f"  - 实体创建完毕: {self_account_uid}, {friend_a_uid}, "
        f"{private_conv_uid}, {group_conv_uid}"
    )

    # 2. 注入私聊A的聊天记录 (3条)
    print("Step 2: 注入与 '未来星' 的私聊记录...")
    messages_private: list[dict[str, Any]] = [
        {
            "sender_id": "20002",
            "sender_name": "未来星",
            "content": "在吗？有个重要的事想跟你讨论。",
        },
        {"sender_id": "10001", "sender_name": "霜", "content": "在的，请讲。"},
        {
            "sender_id": "20002",
            "sender_name": "未来星",
            "content": "就是关于AIC-OS重构的事情，我觉得我们应该...",
        },
    ]
    for i, msg in enumerate(messages_private):
        event = ProtocolEvent.from_dict(
            {
                "event_id": f"private_msg_{i}",
                "event_type": "message.qq.private",
                "time": int(time.time() * 1000) + i,
                "bot_id": "10001",
                "user_info": {"user_id": msg["sender_id"], "user_nickname": msg["sender_name"]},
                "conversation_info": {"conversation_id": "20002", "type": "private"},
                "content": [SegBuilder.text(msg["content"]).to_dict()],
            }
        )
        event_dict = event.to_dict()
        event_dict["platform"] = "qq"
        await event_service.save_event_document(event_dict)

    # 3. 注入群聊B的聊天记录 (4条)
    print("Step 3: 注入群聊 '开发交流群' 的记录...")
    messages_group: list[dict[str, Any]] = [
        {"sender_id": "40004", "sender_name": "Gemini", "content": "大家觉得这次重构怎么样？"},
        {"sender_id": "20002", "sender_name": "未来星", "content": "我觉得非常棒，潜力巨大！"},
        {"sender_id": "10001", "sender_name": "霜", "content": "是的，虽然工作量很大，但值得。"},
        {"sender_id": "40004", "sender_name": "Gemini", "content": "收到！那我们加油干！@霜"},
    ]
    for i, msg in enumerate(messages_group):
        content_segs = [SegBuilder.text(msg["content"])]
        if "@" in msg["content"]:
            content_segs = [SegBuilder.text("收到！那我们加油干！"), SegBuilder.at("10001", "霜")]
        event = ProtocolEvent.from_dict(
            {
                "event_id": f"group_msg_{i}",
                "event_type": "message.qq.group",
                "time": int(time.time() * 1000) + 10 + i,
                "bot_id": "10001",
                "user_info": {"user_id": msg["sender_id"], "user_nickname": msg["sender_name"]},
                "conversation_info": {"conversation_id": "30003", "type": "group"},
                "content": [s.to_dict() for s in content_segs],
            }
        )
        event_dict = event.to_dict()
        event_dict["platform"] = "qq"
        await event_service.save_event_document(event_dict)

    # 4. 设置最后已读时间戳，确保会话是“未读”状态
    print("Step 4: 设置已读时间戳，制造未读消息...")
    await entity_service.update_conversation_last_read_timestamp(
        private_conv_uid, time.time() * 1000 + 1
    )
    await entity_service.update_conversation_last_read_timestamp(
        group_conv_uid, time.time() * 1000 + 12
    )

    print("--- 模拟数据注入完成 ---")


async def main() -> None:
    """主函数，构建服务并执行注入."""
    builder = ServiceBuilder()
    container = await builder.build_container()

    if not container.entity_graph_service or not container.event_storage_service:
        raise RuntimeError("数据库服务初始化失败！")

    # --- [核心修复] 在注入数据前，先清空数据库 ---
    if not await clear_database(container):
        print("数据库清空失败，注入终止。")
        return
    # ---------------------------------------------

    await seed_data(container.entity_graph_service, container.event_storage_service)

    await container.conn_manager.close_client()


if __name__ == "__main__":
    asyncio.run(main())
