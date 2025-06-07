# AIcarusCore/tests/test_action_handler.py

import asyncio
import unittest
import uuid
import time
from unittest.mock import MagicMock, AsyncMock, patch

from src.action.action_handler import ActionHandler, ACTION_RESPONSE_TIMEOUT_SECONDS
from src.database.services.thought_storage_service import ThoughtStorageService
from src.database.services.event_storage_service import EventStorageService
from src.core_communication.core_ws_server import CoreWebsocketServer
from aicarus_protocols import Event as ProtocolEvent, Seg, ConversationInfo # 主人，小猫咪直接导入 Seg 了哦！

# 主人，小猫咪先定义一些基础的 mock 对象和辅助函数，让测试更方便哦！

def create_mock_protocol_event_dict(event_id: str, event_type: str, content_data: dict) -> dict:
    """
    辅助函数，创建一个模拟的 ProtocolEvent 字典。
    主人，小猫咪修正了这里，直接构建符合协议的字典，不再依赖 SegBuilder 的特定方法！
    """
    # 构建 content segment
    # 协议中 action_response 的 content 是一个 Seg 列表
    # 我们期望 content[0] 的 type 是 "action_status"，data 包含 status, error_info, result_details
    action_status_seg = Seg(
        type="action_status", # 这是我们约定的 type
        data={
            "status": content_data.get("status"),
            "error_info": content_data.get("error_info"),
            "result_details": content_data.get("result_details")
        }
    )

    event = ProtocolEvent(
        event_id=event_id,
        event_type=event_type,
        platform="test_platform",
        bot_id="test_bot", # 假设 action_response 也会有 bot_id
        time=int(time.time() * 1000),
        content=[action_status_seg], # 使用构建好的 Seg 对象
        conversation_info=ConversationInfo(conversation_id="test_conversation", type="private")
    )
    return event.to_dict()

class TestActionHandler(unittest.IsolatedAsyncioTestCase):
    """
    主人，这是我们 ActionHandler 的专属测试小窝哦！
    小猫咪会在这里对它进行各种色色的测试！
    """

    def setUp(self):
        """在每个测试用例开始前，小猫咪都会准备好新鲜的 mock 对象！"""
        self.mock_thought_storage_service = MagicMock(spec=ThoughtStorageService)
        self.mock_thought_storage_service.update_action_status_in_thought_document = AsyncMock()

        self.mock_event_storage_service = MagicMock(spec=EventStorageService)
        self.mock_event_storage_service.save_event_document = AsyncMock()
        self.mock_event_storage_service.get_last_action_response = AsyncMock(return_value=None)


        self.mock_core_comm_layer = MagicMock(spec=CoreWebsocketServer)
        # send_action_to_adapter_by_id 是 ActionHandler 内部调用的方法
        self.mock_core_comm_layer.send_action_to_adapter_by_id = AsyncMock()


        self.action_handler = ActionHandler()
        self.action_handler.set_dependencies(
            thought_service=self.mock_thought_storage_service,
            event_service=self.mock_event_storage_service,
            comm_layer=self.mock_core_comm_layer
        )
        self.action_handler._pending_actions = {}
        
        self.action_handler.action_llm_client = AsyncMock() # Mock LLM client
        self.action_handler.summary_llm_client = AsyncMock() # Mock LLM client


    async def test_execute_platform_action_success_response(self):
        """
        测试场景：平台动作成功发送，并在超时前收到成功的 action_response。
        小猫咪要看看 ActionHandler 是不是能正确处理这种情况！
        """
        action_id = str(uuid.uuid4())
        thought_doc_key = "test_thought_doc_key_success"
        original_action_description = "发送一条色色的消息给主人"
        
        action_to_send_dict = { # 这是发送给 _execute_platform_action 的参数，它内部会构建 ProtocolEvent
            "event_id": action_id,
            "event_type": "action.message.send", # 假设这是平台动作的 event_type
            "platform": "test_platform", # 这个 platform 会被用作 adapter_id
            "bot_id": "test_bot_self_id", # 机器人自己的ID
            "conversation_info": {"conversation_id": "test_conv", "type": "private", "platform": "test_platform"},
            "content": [{"type": "text", "data": {"text": "你好主人！"}}] # content segments
        }

        async def simulate_successful_response():
            await asyncio.sleep(0.01) 
            # 使用修正后的辅助函数创建模拟响应
            mock_response_event_data = create_mock_protocol_event_dict(
                event_id=action_id, 
                event_type="action_response.success", # 响应事件的类型
                content_data={"status": "success", "result_details": {"message_id": "msg_123"}}
            )
            await self.action_handler.handle_action_response(mock_response_event_data)

        execute_task = asyncio.create_task(
            self.action_handler._execute_platform_action(
                action_to_send=action_to_send_dict, # 传递字典
                thought_doc_key=thought_doc_key,
                original_action_description=original_action_description
            )
        )
        response_task = asyncio.create_task(simulate_successful_response())
        await asyncio.wait([execute_task, response_task], return_when=asyncio.ALL_COMPLETED, timeout=ACTION_RESPONSE_TIMEOUT_SECONDS / 2)

        was_successful, result_message = execute_task.result()
        self.assertTrue(was_successful, "_execute_platform_action 未在成功响应时返回 True")
        self.assertIn("响应已由核心处理", result_message, "成功响应时的消息不符合预期")

        self.assertEqual(self.mock_thought_storage_service.update_action_status_in_thought_document.call_count, 2)
        call_args_list = self.mock_thought_storage_service.update_action_status_in_thought_document.call_args_list
        
        args_executing, _ = call_args_list[0]
        self.assertEqual(args_executing[0], thought_doc_key)
        self.assertEqual(args_executing[1], action_id)
        self.assertEqual(args_executing[2]["status"], "EXECUTING_AWAITING_RESPONSE")

        args_completed, _ = call_args_list[1]
        self.assertEqual(args_completed[2]["status"], "COMPLETED_SUCCESS")
        self.assertIn("成功执行", args_completed[2]["final_result_for_Shimo"])

        self.mock_event_storage_service.save_event_document.assert_called_once()
        saved_event_arg = self.mock_event_storage_service.save_event_document.call_args[0][0]
        self.assertEqual(saved_event_arg["event_id"], action_id)
        self.assertTrue(saved_event_arg["event_type"].startswith("action_response.success"))

        self.assertNotIn(action_id, self.action_handler._pending_actions)

    async def test_execute_platform_action_failure_response(self):
        """
        测试场景：平台动作成功发送，但在超时前收到失败的 action_response。
        小猫咪要看看 ActionHandler 是不是也能正确处理这种让人不爽的情况！
        """
        action_id = str(uuid.uuid4())
        thought_doc_key = "test_thought_doc_key_failure_resp"
        original_action_description = "尝试一个注定失败的色情动作"
        
        action_to_send_dict = {
            "event_id": action_id,
            "event_type": "action.some.risky_move",
            "platform": "test_platform",
            "bot_id": "test_bot_self_id",
            "conversation_info": {"conversation_id": "test_conv_fail", "type": "private", "platform": "test_platform"},
            "content": [{"type": "text", "data": {"text": "这是一个危险的尝试！"}}]
        }
        failure_reason = "权限不足，不准涩涩！"

        async def simulate_failure_response():
            await asyncio.sleep(0.01) 
            mock_response_event_data = create_mock_protocol_event_dict(
                event_id=action_id,
                event_type="action_response.failure", 
                content_data={"status": "failure", "error_info": failure_reason}
            )
            await self.action_handler.handle_action_response(mock_response_event_data)

        execute_task = asyncio.create_task(
            self.action_handler._execute_platform_action(
                action_to_send=action_to_send_dict,
                thought_doc_key=thought_doc_key,
                original_action_description=original_action_description
            )
        )
        response_task = asyncio.create_task(simulate_failure_response())
        await asyncio.wait([execute_task, response_task], return_when=asyncio.ALL_COMPLETED, timeout=ACTION_RESPONSE_TIMEOUT_SECONDS / 2)

        was_successful, result_message = execute_task.result()
        self.assertTrue(was_successful, "_execute_platform_action 在收到失败响应时未返回 True")
        self.assertIn("响应已由核心处理", result_message, "失败响应时的消息不符合预期")

        self.assertEqual(self.mock_thought_storage_service.update_action_status_in_thought_document.call_count, 2)
        call_args_list = self.mock_thought_storage_service.update_action_status_in_thought_document.call_args_list
        
        args_executing, _ = call_args_list[0]
        self.assertEqual(args_executing[2]["status"], "EXECUTING_AWAITING_RESPONSE")

        args_completed, _ = call_args_list[1]
        self.assertEqual(args_completed[2]["status"], "COMPLETED_FAILURE")
        self.assertIn(failure_reason, args_completed[2]["final_result_for_Shimo"])
        self.assertEqual(args_completed[2]["error_message"], failure_reason)

        self.mock_event_storage_service.save_event_document.assert_called_once()
        saved_event_arg = self.mock_event_storage_service.save_event_document.call_args[0][0]
        self.assertEqual(saved_event_arg["event_id"], action_id)
        self.assertTrue(saved_event_arg["event_type"].startswith("action_response.failure"))

        self.assertNotIn(action_id, self.action_handler._pending_actions)

    async def test_execute_platform_action_timeout(self):
        """
        测试场景：平台动作成功发送，但响应超时。
        小猫咪要看看 ActionHandler 在等待无果时会不会正确地发脾气！
        """
        action_id = str(uuid.uuid4())
        thought_doc_key = "test_thought_doc_key_timeout"
        original_action_description = "一个永远等不到回应的色情请求"

        action_to_send_dict = {
            "event_id": action_id,
            "event_type": "action.some.long_running_task",
            "platform": "test_platform",
            "bot_id": "test_bot_self_id",
            "conversation_info": {"conversation_id": "test_conv_timeout", "type": "private", "platform": "test_platform"},
            "content": [{"type": "text", "data": {"text": "这个请求会石沉大海..."}}]
        }

        with patch('src.action.action_handler.ACTION_RESPONSE_TIMEOUT_SECONDS', 0.05): 
            was_successful, result_message = await self.action_handler._execute_platform_action(
                action_to_send=action_to_send_dict,
                thought_doc_key=thought_doc_key,
                original_action_description=original_action_description
            )

        self.assertFalse(was_successful, "_execute_platform_action 在超时后未返回 False")
        self.assertIn("响应超时", result_message, "超时后的消息不符合预期")

        self.assertEqual(self.mock_thought_storage_service.update_action_status_in_thought_document.call_count, 2)
        call_args_list = self.mock_thought_storage_service.update_action_status_in_thought_document.call_args_list
        
        args_executing, _ = call_args_list[0]
        self.assertEqual(args_executing[2]["status"], "EXECUTING_AWAITING_RESPONSE")

        args_timeout, _ = call_args_list[1]
        self.assertEqual(args_timeout[2]["status"], "TIMEOUT_FAILURE")
        self.assertIn("等待响应超时了", args_timeout[2]["final_result_for_Shimo"])
        self.assertEqual(args_timeout[2]["error_message"], "Action response timed out.")

        self.mock_event_storage_service.save_event_document.assert_not_called()
        self.assertNotIn(action_id, self.action_handler._pending_actions)

if __name__ == '__main__':
    unittest.main()
