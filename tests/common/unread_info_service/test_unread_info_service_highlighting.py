# tests/common/unread_info_service/test_unread_info_service_highlighting.py

import pytest
from pytest_mock import MockerFixture
from src.common.unread_info_service.unread_info_service import UnreadInfoService
from src.database.models import AccountDetails, ConversationDetails, EntityDocument

# 标记整个模块的所有测试都需要异步环境
pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_entity_graph_service(mocker: MockerFixture) -> MockerFixture:
    """模拟 EntityGraphService，这是测试的核心。"""
    mock = mocker.AsyncMock()
    mock.get_recently_active_conversation_entities_with_details.return_value = []
    mock.get_self_entity_by_platform.return_value = None
    mock.get_self_presence_in_conversation.return_value = None
    mock.get_entity_by_key.return_value = None
    return mock


@pytest.fixture
def unread_info_service(
    mocker: MockerFixture,
    mock_entity_graph_service: MockerFixture,
) -> UnreadInfoService:
    """创建一个 UnreadInfoService 实例，并注入模拟依赖。"""
    service = UnreadInfoService(
        event_storage=mocker.AsyncMock(),
        entity_graph_service=mock_entity_graph_service,
    )
    service.update_self_bot_ids({"qq": "99999"})
    return service


class TestAtMentionHighlighting:
    """专门测试 UnreadInfoService 中关于 @ 和回复的高亮及身份识别逻辑。
    """

    PLATFORM = "qq"
    BOT_ID = "99999"
    BOT_NICKNAME = "AIcarus-QQ"
    BOT_CARD_NAME = "Aicarus-Bot"
    OTHER_USER_ID = "12345"
    OTHER_USER_NICKNAME = "路人甲"
    OTHER_USER_REMARK = "测试大佬"
    GROUP_ID = "654321"
    CONV_UID = f"{PLATFORM}_group_{GROUP_ID}"

    def _get_expected_truncated_line(
        self, sender: str, raw_content: str, is_priority: bool = False
    ) -> str:
        """[最终修复版] 辅助函数，精确模拟生产代码的“先截断，后拼接”逻辑。"""
        # 1. 先对原始消息内容进行截断 (模拟 _format_and_truncate_preview)
        processed_content = raw_content.replace("\n", " ").strip()
        if len(processed_content) > 20:
            truncated_content = f"{processed_content[:20]}..."
        else:
            truncated_content = processed_content

        # 2. 然后再拼接发送者 (模拟 _create_message_preview)
        final_preview = f"{sender}：{truncated_content}"

        # 3. 最后再添加高亮标签 (模拟 _create_message_preview)
        if is_priority:
            return f"<b>[有人@你]</b> {final_preview}"
        return final_preview

    async def test_at_self_shows_highlight_and_correct_card_name(
        self,
        unread_info_service: UnreadInfoService,
        mock_entity_graph_service: MockerFixture,
    ):
        """测试场景 [核心]：当机器人在群聊中被 @ 时
        """
        # 1. 准备 (Arrange)
        mock_entity_graph_service.get_self_presence_in_conversation.return_value = {
            "cardname": self.BOT_CARD_NAME
        }
        mock_entity_graph_service.get_self_entity_by_platform.return_value = {
            "details": {"nickname": self.BOT_NICKNAME}
        }
        # 原始消息内容
        raw_message_content = f"你好啊 @{self.BOT_CARD_NAME} 有个非常非常紧急的情况需要你处理"
        mock_entity_graph_service.get_recently_active_conversation_entities_with_details.return_value = [
            self._create_mock_active_conversation(
                at_target_id=self.BOT_ID,
                message_text_parts=["你好啊 ", " 有个非常非常紧急的情况需要你处理"],
            )
        ]

        # 2. 执行 (Act)
        summary = await unread_info_service.get_conversation_list_summary(platform_id=self.PLATFORM)

        # 3. 断言 (Assert)
        expected_line = self._get_expected_truncated_line(
            sender=self.OTHER_USER_NICKNAME, raw_content=raw_message_content, is_priority=True
        )
        assert expected_line in summary

    async def test_at_other_user_resolves_name_from_db_without_truncation(
        self,
        unread_info_service: UnreadInfoService,
        mock_entity_graph_service: MockerFixture,
    ):
        """测试场景 [核心修复验证]：当 @ 其他用户且消息很短时，不应截断。
        """
        # 1. 准备 (Arrange)
        other_user_entity = EntityDocument(
            _key=f"{self.PLATFORM}_private_{self.OTHER_USER_ID}",
            entity_uid=f"{self.PLATFORM}_private_{self.OTHER_USER_ID}",
            entity_type="account",
            details=AccountDetails(
                platform=self.PLATFORM,
                platform_id=self.OTHER_USER_ID,
                nickname=self.OTHER_USER_NICKNAME,
                friend_remark=self.OTHER_USER_REMARK,
            ),
        )
        mock_entity_graph_service.get_entity_by_key.return_value = other_user_entity

        # 文本较短，确保不会触发截断
        raw_message_content_short = f"@{self.OTHER_USER_REMARK} 快出来"
        mock_entity_graph_service.get_recently_active_conversation_entities_with_details.return_value = [
            self._create_mock_active_conversation(
                at_target_id=self.OTHER_USER_ID, message_text_parts=["", " 快出来"]
            )
        ]

        # 2. 执行 (Act)
        summary = await unread_info_service.get_conversation_list_summary(platform_id=self.PLATFORM)

        # 3. 断言 (Assert)
        expected_line_short = self._get_expected_truncated_line(
            sender=self.OTHER_USER_NICKNAME,
            raw_content=raw_message_content_short,
            is_priority=False,
        )
        assert expected_line_short in summary

    # --- 辅助方法 (保持不变) ---
    def _create_mock_active_conversation(
        self,
        at_target_id: str | None = None,
        reply_target_id: str | None = None,
        message_text_parts: list[str] | None = None,
    ) -> dict:
        content = []
        if message_text_parts:
            if message_text_parts[0]:
                content.append({"type": "text", "data": {"text": message_text_parts[0]}})
        if reply_target_id:
            content.append(
                {
                    "type": "quote",
                    "data": {"user_id": reply_target_id, "message_id": "msg-to-reply"},
                }
            )
        if at_target_id:
            content.append(
                {"type": "at", "data": {"user_id": at_target_id, "display_name": "Unreliable Name"}}
            )
        if message_text_parts and len(message_text_parts) > 1:
            if message_text_parts[1]:
                content.append({"type": "text", "data": {"text": message_text_parts[1]}})

        return {
            "conv_doc": EntityDocument(
                _key=self.CONV_UID,
                entity_uid=self.CONV_UID,
                entity_type="conversation",
                details=ConversationDetails(
                    platform=self.PLATFORM,
                    conversation_id=self.GROUP_ID,
                    type="group",
                    name="测试群组",
                ),
            ),
            "latest_event": {
                "timestamp": 1678886400000,
                "user_info": {"user_nickname": self.OTHER_USER_NICKNAME},
                "content": content,
            },
            "unread_count": 1,
            "has_high_priority": True,
        }
