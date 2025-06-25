# tests/focus_chat_mode/test_focus_chat_cycler.py

import asyncio
import os

# 同样，需要把 src 目录加到路径里
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# 导入一大堆我们要测试和模拟的东西，别头晕
from src.focus_chat_mode.chat_session import ChatSession
from src.focus_chat_mode.focus_chat_cycler import FocusChatCycler


class TestFocusChatCycler(unittest.TestCase):
    def setUp(self) -> None:
        """
        准备一个极其复杂的模拟世界，哼，看好了！
        """
        # 1. 模拟所有 ChatSession 的依赖
        self.mock_llm_client = MagicMock()
        self.mock_event_storage = MagicMock()
        self.mock_action_handler = MagicMock()
        self.mock_core_logic = MagicMock()
        self.mock_chat_session_manager = MagicMock()
        self.mock_conversation_service = MagicMock()

        # 【关键】我们要重点监视这个假的服务员！
        self.mock_summarization_service = MagicMock()
        # 它的 consolidate_summary 方法是个异步方法，所以用 AsyncMock
        self.mock_summarization_service.consolidate_summary = AsyncMock()

        # 【关键】这个是我们要检查的最终目标！
        self.mock_summary_storage_service = MagicMock()
        self.mock_summary_storage_service.save_summary = AsyncMock()

        # 2. 创建一个假的 ChatSession 实例，把所有假依赖都喂给它
        self.mock_session = ChatSession(
            conversation_id="test_conv_123",
            llm_client=self.mock_llm_client,
            event_storage=self.mock_event_storage,
            action_handler=self.mock_action_handler,
            bot_id="test_bot_456",
            platform="test_platform",
            conversation_type="group",
            core_logic=self.mock_core_logic,
            chat_session_manager=self.mock_chat_session_manager,
            conversation_service=self.mock_conversation_service,
            summarization_service=self.mock_summarization_service,
            summary_storage_service=self.mock_summary_storage_service,
        )

        # 3. 创建我们要测试的 FocusChatCycler 实例
        # 注意！它的大部分依赖其实是从 session 里拿的
        self.cycler = FocusChatCycler(session=self.mock_session)

        # 4. 确保 cycler 内部的依赖也被正确地替换成了我们的 mock 对象
        # 这一步是双重保险，确保我们的测试环境是完全受控的
        self.cycler.summary_storage_service = self.mock_summary_storage_service

    def test_final_summary_is_saved_on_shutdown(self) -> None:
        """
        测试核心场景：当 shutdown 被调用时，最终的总结是否被正确保存了。
        """
        # --- 准备 (Arrange) ---

        # 1. 伪造一个最终的总结文本，存到 session 的变量里
        # 这模拟了在关闭前，session 已经通过多次循环生成了一份总结
        final_summary_text = "这是我们最终聊天的完美总结，必须被保存下来！"
        self.mock_session.current_handover_summary = final_summary_text

        # 2. 伪造一些待总结的事件ID，用来检查 save_summary 的参数
        self.mock_session.events_since_last_summary = [{"event_id": "event_final_1"}, {"event_id": "event_final_2"}]
        self.cycler._loop_active = True

        # --- 执行 (Act) ---

        # 调用我们要测试的 shutdown 方法
        asyncio.run(self.cycler.shutdown())

        # --- 断言 (Assert) ---

        # 1. 【最重要的检查】确认 summary_storage_service 的 save_summary 方法被调用了，而且只调用了一次！
        self.mock_summary_storage_service.save_summary.assert_called_once()

        # 2. 【更深入的检查】我们不仅要确认它被调用了，还要检查它被调用时的“姿势”对不对！
        #    也就是检查传递给它的参数是不是我们期望的。
        call_args, call_kwargs = self.mock_summary_storage_service.save_summary.call_args

        # 检查关键字参数
        self.assertEqual(call_kwargs.get("conversation_id"), "test_conv_123")
        self.assertEqual(call_kwargs.get("summary_text"), final_summary_text)
        self.assertEqual(call_kwargs.get("platform"), "test_platform")
        self.assertEqual(call_kwargs.get("bot_id"), "test_bot_456")

        # 检查 event_ids_covered 参数
        # 注意：这里的逻辑根据你代码实现，如果 shutdown 时会清空 events_since_last_summary，那这里可能为空
        # 根据我们 _save_final_summary 的逻辑，它会使用当前的 events_since_last_summary
        self.assertIn("event_final_1", call_kwargs.get("event_ids_covered", []))
        self.assertIn("event_final_2", call_kwargs.get("event_ids_covered", []))

    def test_shutdown_does_not_save_empty_summary(self) -> None:
        """
        测试边界场景：如果总结文本是空的，不应该调用保存方法。
        """
        # --- 准备 ---

        # 故意让总结文本为空
        self.mock_session.current_handover_summary = None

        # --- 执行 ---
        asyncio.run(self.cycler.shutdown())

        # --- 断言 ---

        # 确认 save_summary 方法【没有】被调用！
        self.mock_summary_storage_service.save_summary.assert_not_called()


# 同样，加上这个方便直接运行
if __name__ == "__main__":
    unittest.main()
