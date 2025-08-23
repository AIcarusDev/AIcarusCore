# tests/common/focus_chat_history_builder/test_chat_history_formatter.py

"""测试聊天历史格式化器."""

from unittest.mock import AsyncMock, Mock

import pytest
from aicarus_protocols import Seg
from src.common.focus_chat_history_builder.chat_history_formatter import (
    ParsedContent,
    _ChatHistoryFormatter,
    format_chat_history_for_llm,
)
from src.domain.models import Stimulus


class TestChatHistoryFormatter:
    """测试聊天历史格式化器."""

    # --- [新增测试用例] ---
    def test_bot_motivation_is_not_rendered(self) -> None:
        """测试：验证机器人自身发言的动机(motivation)不再被渲染到聊天记录中."""
        # 准备
        bot_stimulus = Stimulus(
            event_id="bot-msg-1",
            timestamp=1000,
            platform="test",
            bot_id="bot123",
            text_content="这是机器人的一条消息",
            sender_id="bot123",  # 发送者是机器人自己
            motivation="这是一个测试动机", # 带有动机
        )

        formatter = _ChatHistoryFormatter(
            session_stimuli=[bot_stimulus],
            bot_profile={"user_id": "bot123", "nickname": "TestBot"},
            conversation_type="group",
            last_processed_timestamp=0,
            is_first_turn=True,
        )

        # 执行
        result_string = formatter._format_and_track_motivation(bot_stimulus, sender_uid="U0")

        # 断言
        # 核心断言：即使有动机，返回的字符串也必须是 None
        assert result_string is None
        # 验证内部状态被更新（这是一个副作用，可选测试）
        assert formatter.last_displayed_bot_motive == "这是一个测试动机"

    def test_format_quote_segment_with_unknown_user(self) -> None:
        """测试格式化引用段时用户未知的情况."""
        formatter = _ChatHistoryFormatter(
            session_stimuli=[],
            bot_profile={"user_id": "bot123", "nickname": "TestBot"},
            conversation_type="group",
            last_processed_timestamp=0,
            is_first_turn=True,
        )

        # 模拟一个引用段，user_id 为 None
        quote_seg = Seg(type="quote", data={"message_id": "msg123", "user_id": None})
        result = formatter._format_quote_segment(quote_seg)

        # 应该显示 "未知用户" 而不是用户名
        assert result == "引用/回复 未知用户(id:msg123)"

    def test_format_quote_segment_with_user_not_in_current_session(self) -> None:
        """测试格式化引用段时用户不在当前会话的情况."""
        formatter = _ChatHistoryFormatter(
            session_stimuli=[],
            bot_profile={"user_id": "bot123", "nickname": "TestBot"},
            conversation_type="group",
            last_processed_timestamp=0,
            is_first_turn=True,
        )

        # 模拟一个引用段，user_id 存在但不在当前会话的 user_map 中
        quote_seg = Seg(type="quote", data={"message_id": "msg123", "user_id": "user456"})
        result = formatter._format_quote_segment(quote_seg)

        # 应该动态添加到 user_map 并显示 UID
        assert result == "引用/回复 U1(id:msg123)"
        # 检查用户是否被添加到 user_map
        assert "user456" in formatter.user_map
        assert formatter.user_map["user456"]["uid_str"] == "U1"

    def test_format_quote_segment_with_user_in_current_session(self) -> None:
        """测试格式化引用段时用户在当前会话的情况."""
        # 创建一个包含用户的刺激物
        user_stimulus = Mock()
        user_stimulus.sender_id = "user123"
        user_stimulus.sender_nickname = "TestUser"
        user_stimulus.sender_cardname = "TestUserCard"

        formatter = _ChatHistoryFormatter(
            session_stimuli=[user_stimulus],
            bot_profile={"user_id": "bot123", "nickname": "TestBot"},
            conversation_type="group",
            last_processed_timestamp=0,
            is_first_turn=True,
        )

        # 模拟一个引用段，user_id 在当前会话的 user_map 中
        quote_seg = Seg(type="quote", data={"message_id": "msg123", "user_id": "user123"})
        result = formatter._format_quote_segment(quote_seg)

        # 应该显示用户的 UID (U1, U2 等)
        assert result == "引用/回复 U1(id:msg123)"

    def test_assemble_log_line_with_quote(self) -> None:
        """测试组装日志行时包含引用信息."""
        formatter = _ChatHistoryFormatter(
            session_stimuli=[],
            bot_profile={"user_id": "bot123", "nickname": "TestBot"},
            conversation_type="group",
            last_processed_timestamp=0,
            is_first_turn=True,
        )

        # 创建解析后的内容，包含引用信息
        parsed_content = ParsedContent(
            content_parts=["这是一条回复消息"],
            content_type="MSG",
            quote_str="引用/回复 U1(id:msg123)",
        )

        stimulus = Mock()
        stimulus.event_id = "event456"

        result = formatter._assemble_log_line(
            parsed_content=parsed_content,
            stimulus=stimulus,
            sender_uid="U2",
            time_str="12:34:56",
            msg_id="msg789",
        )

        # 应该正确组装包含引用的日志行
        expected = "[12:34:56] U2 [MSG, 引用/回复 U1(id:msg123)]: 这是一条回复消息 (id:msg789)"
        assert result == expected

    def test_parse_message_segments_with_quote(self) -> None:
        """测试解析消息段时包含引用段."""
        formatter = _ChatHistoryFormatter(
            session_stimuli=[],
            bot_profile={"user_id": "bot123", "nickname": "TestBot"},
            conversation_type="group",
            last_processed_timestamp=0,
            is_first_turn=True,
        )

        # 创建包含引用段的消息内容
        content_segs = [
            {"type": "quote", "data": {"message_id": "msg123", "user_id": "user456"}},
            {"type": "text", "data": {"text": "这是一条回复"}},
        ]

        stimulus = Mock()
        stimulus.event_id = "event456"
        stimulus.image_analysis = []

        result = formatter._parse_message_segments(
            content_segs=[Seg.from_dict(seg) for seg in content_segs],
            stimulus=stimulus,
            is_in_viewport=True,
        )

        # 应该正确解析引用段和文本段，用户会被动态添加到 user_map
        assert result.quote_str == "引用/回复 U1(id:msg123)"
        assert result.content_parts == ["这是一条回复"]
        assert result.content_type == "MSG"
        # 检查用户是否被动态添加到 user_map
        assert "user456" in formatter.user_map
        assert formatter.user_map["user456"]["uid_str"] == "U1"

    @pytest.mark.asyncio
    async def test_format_chat_history_for_llm_with_quote(self) -> None:
        """测试完整的聊天历史格式化流程包含引用消息."""
        # 模拟事件存储服务
        mock_event_storage = AsyncMock()

        # 模拟包含引用消息的事件数据 - 使用 ProtocolEvent.from_dict 期望的格式
        event_data = {
            "event_id": "event123",
            "event_type": "message.private",  # ProtocolEvent 期望的字段名
            "time": 1700000000000,
            "bot_id": "bot123",
            "content": [  # ProtocolEvent 期望的字段名
                {"type": "quote", "data": {"message_id": "msg456", "user_id": "user789"}},
                {"type": "text", "data": {"text": "回复消息"}},
            ],
            # Stimulus.from_db_document 需要的额外字段
            "timestamp": 1700000000000,
            "sender_id": "user123",
            "sender_nickname": "TestUser",
            "raw_event_type": "message.private",  # Stimulus 期望的字段
            "raw_content": [  # Stimulus 期望的字段
                {"type": "quote", "data": {"message_id": "msg456", "user_id": "user789"}},
                {"type": "text", "data": {"text": "回复消息"}},
            ],
        }

        mock_event_storage.get_recent_chat_message_documents.return_value = [event_data]

        result = await format_chat_history_for_llm(
            event_storage=mock_event_storage,
            conversation_id="conv123",
            bot_profile={"user_id": "bot123", "nickname": "TestBot"},
            conversation_type="private",
            conversation_name="测试会话",
            last_processed_timestamp=0,
            is_first_turn=True,
        )

        components, processed_stimuli = result

        # 应该包含引用信息，用户会被动态添加到 user_map 并显示 UID
        assert "引用/回复" in components.chat_history_log_block
        assert "U1(id:msg456)" in components.chat_history_log_block
        # 检查用户是否被动态添加到 user_map
        assert "user789" in components.user_map
        assert components.user_map["user789"]["uid_str"] == "U1"

    def test_user_map_building(self) -> None:
        """测试用户映射构建逻辑."""
        # 创建多个用户的刺激物
        user1_stimulus = Mock()
        user1_stimulus.sender_id = "user123"
        user1_stimulus.sender_nickname = "User1"
        user1_stimulus.sender_cardname = "User1Card"

        user2_stimulus = Mock()
        user2_stimulus.sender_id = "user456"
        user2_stimulus.sender_nickname = None  # 测试昵称为空的情况
        user2_stimulus.sender_cardname = None

        formatter = _ChatHistoryFormatter(
            session_stimuli=[user1_stimulus, user2_stimulus],
            bot_profile={"user_id": "bot123", "nickname": "TestBot"},
            conversation_type="group",
            last_processed_timestamp=0,
            is_first_turn=True,
        )

        # 检查用户映射是否正确构建
        assert "bot123" in formatter.user_map
        assert "user123" in formatter.user_map
        assert "user456" in formatter.user_map

        # 检查 UID 分配
        assert formatter.user_map["bot123"]["uid_str"] == "U0"
        assert formatter.user_map["user123"]["uid_str"] == "U1"
        assert formatter.user_map["user456"]["uid_str"] == "U2"

        # 检查昵称回退逻辑
        assert formatter.user_map["user456"]["nick"] == "用户user456"

    def test_format_user_list_block(self) -> None:
        """测试用户列表块格式化."""
        formatter = _ChatHistoryFormatter(
            session_stimuli=[],
            bot_profile={"user_id": "bot123", "nickname": "TestBot", "role": "管理员"},
            conversation_type="group",
            last_processed_timestamp=0,
            is_first_turn=True,
        )

        # 手动添加一些用户到 user_map
        formatter.user_map = {
            "bot123": {
                "uid_str": "U0",
                "nick": "TestBot",
                "card": "TestBot",
                "title": "群主",
                "perm": "管理员",
            },
            "user123": {
                "uid_str": "U1",
                "nick": "User1",
                "card": "User1Card",
                "title": "",
                "perm": "成员",
            },
        }

        result = formatter.format_user_list_block()

        # 应该包含正确的用户信息
        assert "U0: bot123（你）" in result
        assert "nick:TestBot" in result
        assert "U1: user123" in result
        assert "nick:User1" in result
