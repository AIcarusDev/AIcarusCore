# tests/common/unread_info_service/test_unread_info_service.py

from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture
from src.common.unread_info_service.unread_info_service import UnreadInfoService
from src.database.models import ConversationDetails, EntityDocument

# 标记整个模块的所有测试都需要异步环境
pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_entity_graph_service(mocker: MockerFixture) -> MagicMock:
    """模拟 EntityGraphService."""
    mock = mocker.MagicMock()
    # 模拟 get_recently_active_conversation_entities_with_details 方法
    mock.get_recently_active_conversation_entities_with_details = mocker.AsyncMock()
    # 模拟 get_conversations_by_platform 方法
    mock.get_conversations_by_platform = mocker.AsyncMock()
    # --- [修复] ---
    # 将需要被 await 的方法明确声明为 AsyncMock
    mock.get_self_presence_in_conversation = mocker.AsyncMock()
    mock.get_self_entity_by_platform = mocker.AsyncMock()
    # --- [修复结束] ---
    return mock


@pytest.fixture
def unread_info_service(
    mocker: MockerFixture,
    mock_entity_graph_service: MagicMock,
) -> UnreadInfoService:
    """创建一个 UnreadInfoService 实例，并注入模拟依赖."""
    # EventStorageService 在这个测试中不是关键，简单模拟即可
    mock_event_storage = mocker.MagicMock()
    return UnreadInfoService(
        event_storage=mock_event_storage,
        entity_graph_service=mock_entity_graph_service,
    )


class TestUnreadInfoServiceSummaries:
    """专门测试 UnreadInfoService 生成摘要的逻辑."""

    async def test_group_summary_uses_fallback_name_when_none(
        self,
        unread_info_service: UnreadInfoService,
        mock_entity_graph_service: MagicMock,
    ) -> None:
        """测试场景 (复现BUG): 当数据库中的群聊实体没有名称时.

        摘要应使用 '未知群聊(ID)' 作为回退。
        """
        # 1. 准备 (Arrange)
        group_id = "643700843"
        conv_uid = f"qq_group_{group_id}"

        # 模拟一个数据库中没有 name 的群聊实体
        mock_conv_doc = EntityDocument(
            _key=conv_uid,
            entity_uid=conv_uid,
            entity_type="conversation",
            details=ConversationDetails(
                platform="qq",
                conversation_id=group_id,
                type="group",
                name=None,  # <-- 关键：模拟数据库中 name 缺失的情况
            ),
        )

        # 模拟该会话的最新事件和未读信息
        mock_latest_event = {
            "timestamp": 1678886400000,
            "user_info": {"user_nickname": "理塘最強伝說"},
            "content": [{"type": "text", "data": {"text": "因为抖音有哈基米音乐"}}],
        }
        mock_unread_info = {"unread_count": 17, "has_high_priority": False}

        # 配置 mock service 的返回值
        mock_entity_graph_service.get_recently_active_conversation_entities_with_details.return_value = [  # noqa: E501
            {
                "conv_doc": mock_conv_doc,
                "latest_event": mock_latest_event,
                **mock_unread_info,
            }
        ]
        mock_entity_graph_service.get_conversations_by_platform.return_value = {conv_uid: None}

        # 2. 执行 (Act)
        summary = await unread_info_service.get_conversation_list_summary(platform_id="qq")

        # 3. 断言 (Assert)
        # 验证是否正确地回退到了 "未知群聊(ID)" 格式
        assert f"未知群聊({group_id})" in summary
        # 确保摘要中没有出现 None 或者其他意外的字符串
        assert "None" not in summary

        # --- [FIX START] ---
        # 修复点：断言完整的、未被截断的短消息内容
        assert "理塘最強伝說：因为抖音有哈基米音乐" in summary
        # --- [FIX END] ---

        assert "17 条未读信息" in summary

    async def test_group_summary_uses_entity_name_when_present(
        self,
        unread_info_service: UnreadInfoService,
        mock_entity_graph_service: MagicMock,
    ) -> None:
        """测试场景 (正确路径): 当数据库中的群聊实体有名称时，应优先使用该名称.

        摘要应优先使用该名称。
        """
        # 1. 准备 (Arrange)
        group_id = "123456789"
        conv_uid = f"qq_group_{group_id}"
        group_name = "Aicarus 核心测试群"

        # 模拟一个数据库中有 name 的群聊实体
        mock_conv_doc = EntityDocument(
            _key=conv_uid,
            entity_uid=conv_uid,
            entity_type="conversation",
            details=ConversationDetails(
                platform="qq",
                conversation_id=group_id,
                type="group",
                name=group_name,  # <-- 关键：提供一个有效的群名
            ),
        )

        mock_latest_event = {
            "timestamp": 1678886400000,
            "user_info": {"user_nickname": "测试用户"},
            "content": [{"type": "text", "data": {"text": "测试消息"}}],
        }
        mock_unread_info = {"unread_count": 5, "has_high_priority": True}

        # 配置 mock service 的返回值
        mock_entity_graph_service.get_recently_active_conversation_entities_with_details.return_value = [  # noqa: E501
            {
                "conv_doc": mock_conv_doc,
                "latest_event": mock_latest_event,
                **mock_unread_info,
            }
        ]
        mock_entity_graph_service.get_conversations_by_platform.return_value = {
            conv_uid: group_name
        }

        # 2. 执行 (Act)
        summary = await unread_info_service.get_conversation_list_summary(platform_id="qq")

        # 3. 断言 (Assert)
        # 验证是否正确地使用了数据库中提供的群名
        assert f"[群名称]：{group_name}" in summary
        # 确保回退文本没有出现
        assert "未知群聊" not in summary

        # --- [ADDED] ---
        # 新增断言：同样验证短消息不会被截断
        assert "测试用户：测试消息" in summary
        # --- [ADDED] ---

    async def test_self_message_shows_group_cardname_instead_of_self(
        self,
        unread_info_service: UnreadInfoService,
        mock_entity_graph_service: MagicMock,
    ) -> None:
        """测试场景：当机器人自己发送消息时，应该显示群名片而不是"AIcarus (Self)"."""
        # 1. 准备 (Arrange)
        group_id = "123456789"
        conv_uid = f"qq_group_{group_id}"
        group_name = "Aicarus 核心测试群"
        bot_id = "99999"
        group_cardname = "Aicarus-Bot"

        # 更新机器人ID
        unread_info_service.update_self_bot_ids({"qq": bot_id})

        # 模拟群聊实体
        mock_conv_doc = EntityDocument(
            _key=conv_uid,
            entity_uid=conv_uid,
            entity_type="conversation",
            details=ConversationDetails(
                platform="qq",
                conversation_id=group_id,
                type="group",
                name=group_name,
            ),
        )

        # 模拟机器人自己发送的消息
        mock_latest_event = {
            "timestamp": 1678886400000,
            "user_info": {
                "user_id": bot_id,
                "user_nickname": "AIcarus",
                "user_cardname": "AIcarus (Self)",  # 事件中的群名片，但应该被数据库中的覆盖
            },
            "content": [{"type": "text", "data": {"text": "我这就去看看资源站"}}],
        }
        mock_unread_info = {"unread_count": 1, "has_high_priority": False}

        # 配置 mock service 的返回值
        mock_entity_graph_service.get_recently_active_conversation_entities_with_details.return_value = [  # noqa: E501
            {
                "conv_doc": mock_conv_doc,
                "latest_event": mock_latest_event,
                **mock_unread_info,
            }
        ]

        # 模拟数据库查询返回群名片
        mock_entity_graph_service.get_self_presence_in_conversation.return_value = {
            "cardname": group_cardname
        }

        # 2. 执行 (Act)
        summary = await unread_info_service.get_conversation_list_summary(platform_id="qq")

        # 3. 断言 (Assert)
        # 验证显示的是群名片而不是事件中的名称
        assert f"{group_cardname}：我这就去看看资源站" in summary
        assert "AIcarus (Self)" not in summary
        assert "我：我这就去看看资源站" not in summary

    async def test_no_unread_count_when_zero_unread_messages(
        self,
        unread_info_service: UnreadInfoService,
        mock_entity_graph_service: MagicMock,
    ) -> None:
        """测试场景：当未读消息数为0时，不应该显示"共 0 条未读信息"."""
        # 1. 准备 (Arrange)
        group_id = "123456789"
        conv_uid = f"qq_group_{group_id}"
        group_name = "Aicarus 核心测试群"

        # 模拟群聊实体
        mock_conv_doc = EntityDocument(
            _key=conv_uid,
            entity_uid=conv_uid,
            entity_type="conversation",
            details=ConversationDetails(
                platform="qq",
                conversation_id=group_id,
                type="group",
                name=group_name,
            ),
        )

        # 模拟消息
        mock_latest_event = {
            "timestamp": 1678886400000,
            "user_info": {"user_nickname": "测试用户"},
            "content": [{"type": "text", "data": {"text": "测试消息"}}],
        }
        mock_unread_info = {"unread_count": 0, "has_high_priority": False}  # 未读数为0

        # 配置 mock service 的返回值
        mock_entity_graph_service.get_recently_active_conversation_entities_with_details.return_value = [  # noqa: E501
            {
                "conv_doc": mock_conv_doc,
                "latest_event": mock_latest_event,
                **mock_unread_info,
            }
        ]

        # 2. 执行 (Act)
        summary = await unread_info_service.get_conversation_list_summary(platform_id="qq")

        # 3. 断言 (Assert)
        # 验证不显示未读信息部分
        assert "(时间：" in summary
        assert "共 0 条未读信息" not in summary
        assert "条未读信息" not in summary
        # 验证只显示时间
        assert "(时间：2年前)" in summary or "(时间：" in summary

    async def test_self_message_shows_group_cardname_instead_of_placeholder(
        self,
        unread_info_service: UnreadInfoService,
        mock_entity_graph_service: MagicMock,
    ) -> None:
        """
        测试场景 (Bug 1 修复验证): 当最新消息是机器人自己发送时，
        摘要应优先显示其在数据库中记录的【群名片】，而不是事件中缓存的通用昵称。
        """
        # 1. 准备 (Arrange)
        group_id = "123456"
        conv_uid = f"qq_group_{group_id}"
        bot_id = "99999"
        bot_group_cardname = "霜-Bot"  # <-- 这是我们期望看到的正确名称
        bot_platform_nickname = "霜"

        # 告知服务，机器人在qq平台的ID是'99999'
        unread_info_service.update_self_bot_ids({"qq": bot_id})

        # 模拟一个群聊实体
        mock_conv_doc = EntityDocument(
            _key=conv_uid,
            entity_uid=conv_uid,
            entity_type="conversation",
            details=ConversationDetails(
                platform="qq",
                conversation_id=group_id,
                type="group",
                name="测试群",
            ),
        )

        # 模拟一个由机器人自己发送的事件，注意这里的nickname是错误的占位符
        mock_latest_event = {
            "timestamp": 1678886400000,
            "user_info": {"user_id": bot_id, "user_nickname": "AIcarus (Self)"},
            "content": [{"type": "text", "data": {"text": "这是一条机器人自己发的消息"}}],
        }

        # 配置mock：当服务查询活跃会话时，返回我们构造的数据
        mock_entity_graph_service.get_recently_active_conversation_entities_with_details.return_value = [
            {
                "conv_doc": mock_conv_doc,
                "latest_event": mock_latest_event,
                "unread_count": 1,
                "has_high_priority": False,
            }
        ]
        # 配置mock：当服务查询机器人在此群的身份时，返回正确的群名片
        mock_entity_graph_service.get_self_presence_in_conversation.return_value = {
            "cardname": bot_group_cardname
        }
        # 配置mock：当服务查询机器人的平台身份时，返回正确的主昵称
        mock_entity_graph_service.get_self_entity_by_platform.return_value = {
            "details": {"nickname": bot_platform_nickname}
        }

        # 2. 执行 (Act)
        summary = await unread_info_service.get_conversation_list_summary(platform_id="qq")

        # 3. 断言 (Assert)
        # --- [修复] ---
        # 移除多余的 "..."，因为测试消息长度不足20，不会被截断
        assert f"{bot_group_cardname}：这是一条机器人自己发的消息" in summary
        # --- [修复结束] ---
        assert "AIcarus (Self)" not in summary
        assert "我：" not in summary # 也不能是“我”