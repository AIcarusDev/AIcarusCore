# tests/observation/test_summarization_service.py

import asyncio
import os

# 哎，为了测试，我们得把要测试的东西给引进来，真麻烦
# 我们需要告诉 Python 去哪里找 src 目录
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# 把项目根目录（AIcarusCore）加到 Python 的路径里
# 这样 "from src.observation..." 这样的导入才能成功
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# 现在可以安全地导入了
from src.observation.summarization_service import SummarizationService


class TestSummarizationService(unittest.TestCase):
    def setUp(self) -> None:
        """
        这个方法在每个测试用例开始前都会被调用，用来做一些准备工作。
        比如创建我们要测试的对象，和一些假的依赖。
        """
        # 1. 创建一个假的 LLM 客户端
        # AsyncMock 是专门用来模拟异步方法的假对象
        self.mock_llm_client = MagicMock()
        self.mock_llm_client.make_llm_request = AsyncMock()

        # 2. 创建我们要测试的 SummarizationService 实例，并把假客户端喂给它
        self.service = SummarizationService(llm_client=self.mock_llm_client)

        # 3. 准备一些假的测试数据，这样就不用真的去连数据库了
        self.mock_bot_profile = {"user_id": "12345", "nickname": "小懒猫测试版", "card": "摸鱼中"}
        self.mock_conversation_info = {"name": "小懒猫的测试群", "type": "group", "id": "group_1001"}
        self.mock_user_map = {
            "12345": {"uid_str": "U0", "nick": "小懒猫测试版", "card": "摸鱼中", "title": "", "perm": "owner"},
            "67890": {"uid_str": "U1", "nick": "用户A", "card": "用户A", "title": "", "perm": "member"},
        }

        # 4. 模拟一下 config，因为 prompt 构建需要它
        # 使用 patch 来临时替换掉 config 模块里的 persona 对象
        self.persona_patcher = patch("src.observation.summarization_service.config.persona")
        self.mock_persona = self.persona_patcher.start()
        self.mock_persona.bot_name = "小懒猫测试版"
        self.mock_persona.description = "一只爱睡觉的猫"
        self.mock_persona.profile = "最讨厌麻烦事"

    def tearDown(self) -> None:
        """
        这个方法在每个测试用例结束后调用，用来清理现场。
        """
        self.persona_patcher.stop()

    def test_first_summary_generation(self) -> None:
        """
        测试场景1：首次总结，没有 previous_summary
        """
        # --- 准备阶段 (Arrange) ---

        # 模拟 LLM 会返回一个成功的、包含总结文本的响应
        expected_summary = "我们开始讨论如何写测试，用户A觉得很难，但我感觉还行。"
        self.mock_llm_client.make_llm_request.return_value = {"text": expected_summary, "error": None}

        # 准备新的聊天记录事件
        new_events = [
            {
                "_key": "event1",
                "timestamp": 1700000000000,
                "event_type": "message.group.normal",
                "user_info": {"user_id": "67890", "user_nickname": "用户A"},
                "content": [{"type": "text", "data": {"text": "这个单元测试好难啊！"}}],
            },
            {
                "_key": "event2",
                "timestamp": 1700000001000,
                "event_type": "action.message.send",
                "user_info": {"user_id": "12345", "user_nickname": "小懒猫测试版"},
                "content": [{"type": "text", "data": {"text": "还好吧，我教你。"}}],
                "motivation": "安抚一下这个小白用户",
            },
        ]

        # --- 执行阶段 (Act) ---

        # 调用我们要测试的方法
        # 因为它是异步的，我们需要用 asyncio.run 来运行它
        result_summary = asyncio.run(
            self.service.consolidate_summary(
                previous_summary=None,  # 首次总结，所以是 None
                recent_events=new_events,
                bot_profile=self.mock_bot_profile,
                conversation_info=self.mock_conversation_info,
                user_map=self.mock_user_map,
            )
        )

        # --- 断言阶段 (Assert) ---

        # 检查我们的方法返回的结果是不是和我们预期的一样
        self.assertEqual(result_summary, expected_summary)

        # 检查我们的假 LLM 客户端是不是被正确地调用了
        self.mock_llm_client.make_llm_request.assert_called_once()

        # (可选) 检查传递给 LLM 的 prompt 是否包含了关键信息
        call_args = self.mock_llm_client.make_llm_request.call_args
        user_prompt = call_args.kwargs.get("prompt", "")
        self.assertIn("暂时无总结，这是你专注于该群聊的首次总结", user_prompt)
        self.assertIn("这个单元测试好难啊！", user_prompt)
        self.assertIn("安抚一下这个小白用户", user_prompt)

    def test_progressive_summary_generation(self) -> None:
        """
        测试场景2：渐进式总结，有旧摘要和新事件
        """
        # --- 准备 ---
        previous_summary = "我们开始讨论如何写测试，用户A觉得很难，但我感觉还行。"
        expected_new_summary = "我们讨论了写测试的难点，用户A提到了mock，我解释了它的作用，他好像懂了。"
        self.mock_llm_client.make_llm_request.return_value = {"text": expected_new_summary, "error": None}

        new_events = [
            {
                "_key": "event3",
                "timestamp": 1700000002000,
                "event_type": "message.group.normal",
                "user_info": {"user_id": "67890", "user_nickname": "用户A"},
                "content": [{"type": "text", "data": {"text": "主要是那个 mock 不会用。"}}],
            }
        ]

        # --- 执行 ---
        result_summary = asyncio.run(
            self.service.consolidate_summary(
                previous_summary=previous_summary,
                recent_events=new_events,
                bot_profile=self.mock_bot_profile,
                conversation_info=self.mock_conversation_info,
                user_map=self.mock_user_map,
            )
        )

        # --- 断言 ---
        self.assertEqual(result_summary, expected_new_summary)
        self.mock_llm_client.make_llm_request.assert_called_once()

        call_args = self.mock_llm_client.make_llm_request.call_args
        user_prompt = call_args.kwargs.get("prompt", "")
        self.assertIn(previous_summary, user_prompt)  # 检查旧摘要是否在 prompt 里
        self.assertIn("主要是那个 mock 不会用。", user_prompt)  # 检查新消息是否在 prompt 里

    def test_no_new_events(self) -> None:
        """
        测试场景3：没有新事件，应该直接返回旧摘要
        """
        previous_summary = "旧的回忆录，不应该被改变。"

        # --- 执行 ---
        result_summary = asyncio.run(
            self.service.consolidate_summary(
                previous_summary=previous_summary,
                recent_events=[],  # 没有新事件！
                bot_profile=self.mock_bot_profile,
                conversation_info=self.mock_conversation_info,
                user_map=self.mock_user_map,
            )
        )

        # --- 断言 ---
        self.assertEqual(result_summary, previous_summary)
        # 确认 LLM 根本没有被调用，因为没必要
        self.mock_llm_client.make_llm_request.assert_not_called()

    def test_llm_call_failure(self) -> None:
        """
        测试场景4：LLM 调用失败了，应该返回旧摘要和一个错误提示
        """
        # --- 准备 ---
        previous_summary = "旧的回忆录。"
        error_message = "API key is invalid"
        self.mock_llm_client.make_llm_request.return_value = {"text": None, "error": True, "message": error_message}

        new_events = [
            {
                "_key": "event4",
                "timestamp": 1700000003000,
                "event_type": "message.group.normal",
                "user_info": {"user_id": "67890", "user_nickname": "用户A"},
                "content": [{"type": "text", "data": {"text": "你还在吗？"}}],
            }
        ]

        # --- 执行 ---
        result_summary = asyncio.run(
            self.service.consolidate_summary(
                previous_summary=previous_summary,
                recent_events=new_events,
                bot_profile=self.mock_bot_profile,
                conversation_info=self.mock_conversation_info,
                user_map=self.mock_user_map,
            )
        )

        # --- 断言 ---
        # 确认返回的内容是旧摘要加上错误信息
        self.assertIn(previous_summary, result_summary)
        self.assertIn(error_message, result_summary)
        self.assertIn("系统提示", result_summary)  # 检查我们新加的标记
        self.mock_llm_client.make_llm_request.assert_called_once()


# 这段代码让你可以直接运行这个测试文件
if __name__ == "__main__":
    unittest.main()
