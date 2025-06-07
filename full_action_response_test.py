# AIcarusCore/full_action_response_test.py

import asyncio
import unittest
import uuid
import time
from unittest.mock import MagicMock, AsyncMock, patch

# 核心组件导入
from src.action.action_handler import ActionHandler, ACTION_RESPONSE_TIMEOUT_SECONDS
from src.common.custom_logging.logger_manager import get_logger # 确保日志可以工作
from src.config import config # 加载配置
from src.database.core.connection_manager import ArangoDBConnectionManager, CoreDBCollections
from src.database.services.event_storage_service import EventStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.database.services.action_log_storage_service import ActionLogStorageService 

from aicarus_protocols import Event as ProtocolEvent, Seg, ConversationInfo

logger = get_logger("AIcarusCore.FullActionResponseTest")

# --- 辅助函数 ---
def create_mock_action_event_dict(action_id: str, platform: str, event_type: str, content_segs: list) -> dict:
    return {
        "event_id": action_id, "event_type": event_type, "timestamp": int(time.time() * 1000),
        "platform": platform, "bot_id": "test_core_bot_id", 
        "conversation_info": {"conversation_id": "test_conversation_for_action", "type": "private", "platform": platform},
        "content": content_segs, "protocol_version": config.inner.protocol_version
    }

def create_mock_action_response_event_dict(original_action_id: str, platform: str, status: str, error_info: str = None, result_details: dict = None) -> dict:
    action_status_seg = Seg(type="action_status", data={"status": status, "error_info": error_info, "result_details": result_details})
    event_type = f"action_response.{status}"
    return ProtocolEvent(
        event_id=original_action_id, event_type=event_type, platform=platform, bot_id="mock_adapter_bot_id", 
        time=int(time.time() * 1000), content=[action_status_seg], 
        conversation_info=ConversationInfo(conversation_id="test_conversation_for_action", type="private", platform=platform)
    ).to_dict()

class MockCoreWebsocketServer:
    def __init__(self, action_handler_instance: ActionHandler = None):
        self.sent_actions = [] 
        self.action_handler = action_handler_instance
        self.logger = get_logger("AIcarusCore.MockWsServer")
    async def send_action_to_adapter_by_id(self, adapter_id: str, action_event: dict) -> bool:
        self.sent_actions.append({"adapter_id": adapter_id, "action_event": action_event})
        return True
    async def simulate_incoming_action_response(self, response_event_data: dict):
        if self.action_handler: await self.action_handler.handle_action_response(response_event_data)
    def get_last_sent_action(self): return self.sent_actions[-1] if self.sent_actions else None
    def clear_sent_actions(self): self.sent_actions = []

class TestFullActionResponseFlow(unittest.IsolatedAsyncioTestCase):
    """
    主人，这是我们完整的 action_response 流程测试哦！
    小猫咪会在这里模拟 Core 的决策、动作发送、适配器响应和超时，
    确保一切都像主人期望的那样色情地运作！
    """

    @classmethod
    async def asyncSetUpClass(cls):
        logger.critical("主人主人！asyncSetUpClass 被狠狠地调用了（如果这条日志出现的话）！但我们不再用它设置共享状态了！")
        pass

    @classmethod
    async def asyncTearDownClass(cls):
        logger.critical("主人主人！asyncTearDownClass 也要被调用了（如果这条日志出现的话）！")
        pass

    async def asyncSetUp(self):
        """每个测试用例前创建所有需要的实例，包括数据库连接。"""
        await super().asyncSetUp() 
        logger.debug(f"开始为测试用例 {self.id()} 执行 asyncSetUp...")
        
        # 1. 创建数据库连接管理器
        all_core_collection_configs = CoreDBCollections.get_all_core_collection_configs()
        logger.info(f"asyncSetUp ({self.id()}): 尝试创建 ArangoDBConnectionManager...")
        self.conn_manager = await ArangoDBConnectionManager.create_from_config(
            object(), 
            core_collection_configs=all_core_collection_configs
        )
        self.assertIsNotNone(self.conn_manager, f"asyncSetUp ({self.id()}): self.conn_manager is None after ArangoDBConnectionManager.create_from_config")
        self.assertIsNotNone(self.conn_manager.db, f"asyncSetUp ({self.id()}): self.conn_manager.db is None after ArangoDBConnectionManager.create_from_config")
        logger.info(f"asyncSetUp ({self.id()}): ArangoDBConnectionManager 已成功创建，连接到数据库: {self.conn_manager.db.name}")

        # 2. 创建服务实例
        self.thought_storage = ThoughtStorageService(conn_manager=self.conn_manager)
        self.event_storage = EventStorageService(conn_manager=self.conn_manager)
        # ActionLogStorageService 实例现在也在这里创建，但我们测试 ActionHandler 时会 mock 它
        self.action_log_service_mock = AsyncMock(spec=ActionLogStorageService) 
        
        self.assertIsNotNone(self.thought_storage, f"asyncSetUp ({self.id()}): self.thought_storage is None")
        self.assertIsNotNone(self.event_storage, f"asyncSetUp ({self.id()}): self.event_storage is None")
        self.assertIsNotNone(self.action_log_service_mock, f"asyncSetUp ({self.id()}): self.action_log_service_mock is None")
        logger.info(f"asyncSetUp ({self.id()}): 所有存储服务和 ActionLogService mock 已创建。")

        # 3. 创建 ActionHandler 和 MockCoreWebsocketServer
        self.action_handler = ActionHandler()
        self.mock_ws_server = MockCoreWebsocketServer(action_handler_instance=self.action_handler)
        
        self.action_handler.set_dependencies(
            thought_service=self.thought_storage,
            event_service=self.event_storage,
            action_log_service=self.action_log_service_mock, # 注入真实的 ActionLogService 的 mock
            comm_layer=self.mock_ws_server 
        )
        self.action_handler.action_llm_client = AsyncMock()
        self.action_handler.summary_llm_client = AsyncMock()
        
        # Mock 服务上的方法
        self.thought_storage.update_action_status_in_thought_document = AsyncMock()
        self.event_storage.save_event_document = AsyncMock()
        # self.event_storage.get_last_action_response = AsyncMock(return_value=None) # 这个方法在当前测试中不直接验证其返回值

        # 为 action_log_service_mock 设置 get_action_log 的 mock 返回值，以模拟 _execute_platform_action 中的读取
        self.action_log_service_mock.get_action_log = AsyncMock(return_value=None)


        self.action_handler._pending_actions = {}
        self.mock_ws_server.clear_sent_actions()
        logger.debug(f"测试用例 {self.id()} 的 asyncSetUp 执行完毕。")

    async def asyncTearDown(self):
        logger.debug(f"开始为测试用例 {self.id()} 执行 asyncTearDown...")
        if hasattr(self, 'conn_manager') and self.conn_manager:
            await self.conn_manager.close_client()
            logger.info(f"asyncTearDown ({self.id()}): 数据库连接已关闭。")
        await super().asyncTearDown() 
        logger.debug(f"测试用例 {self.id()} 的 asyncTearDown 执行完毕。")

    async def test_platform_action_successful_response_flow(self):
        logger.info(f"--- 开始测试: {self.id()} ---")
        action_description = "给主人发送一条充满爱意的消息"
        thought_doc_key = f"thought_{uuid.uuid4().hex}" 
        core_action_id = f"core_action_{uuid.uuid4().hex}" 
        target_platform = "test_adapter_01"
        action_type_sent = "action.message.send"
        content_sent = [{"type": "text", "data": {"text": "主人，小猫咪好爱你哦！"}}]
        
        action_to_send_to_adapter = create_mock_action_event_dict(
            action_id=core_action_id, platform=target_platform,
            event_type=action_type_sent, content_segs=content_sent
        )
        action_to_send_to_adapter["bot_id"] = "core_bot_for_sending"
        action_to_send_to_adapter["conversation_info"]["conversation_id"] = "master_bedroom"

        # 模拟 get_action_log 在成功时返回相应的日志条目
        async def mock_get_action_log_success(action_id_param):
            if action_id_param == core_action_id:
                # 模拟在 handle_action_response 中更新后的 ActionLog 条目
                return {
                    "action_id": core_action_id,
                    "status": "success", # 关键状态
                    "error_info": None,
                    # ... 其他字段
                }
            return None
        self.action_log_service_mock.get_action_log.side_effect = mock_get_action_log_success


        async def simulate_adapter_success_response():
            await asyncio.sleep(0.05) 
            response_event = create_mock_action_response_event_dict(
                original_action_id=core_action_id, platform=target_platform, status="success",
                result_details={"platform_message_id": "pf_msg_success_123"}
            )
            await self.mock_ws_server.simulate_incoming_action_response(response_event)

        execute_task = asyncio.create_task(
            self.action_handler._execute_platform_action(
                action_to_send=action_to_send_to_adapter,
                thought_doc_key=thought_doc_key,
                original_action_description=action_description
            )
        )
        response_simulation_task = asyncio.create_task(simulate_adapter_success_response())
        
        done, pending = await asyncio.wait([execute_task, response_simulation_task], return_when=asyncio.ALL_COMPLETED, timeout=ACTION_RESPONSE_TIMEOUT_SECONDS + 1 )
        self.assertEqual(len(done), 2, f"并非所有模拟任务都已完成. Done: {len(done)}, Pending: {len(pending)}")
        for task in done: 
            if task.exception(): self.fail(f"模拟任务中发生异常: {task.exception()}")

        was_response_received, exec_result_message = await execute_task 

        self.assertTrue(was_response_received, "成功响应时，_execute_platform_action 应返回 True")
        self.assertIn("已成功执行并收到响应", exec_result_message)
        
        self.action_log_service_mock.save_action_attempt.assert_called_once()
        save_attempt_args = self.action_log_service_mock.save_action_attempt.call_args[1]
        self.assertEqual(save_attempt_args['action_id'], core_action_id)
        self.assertEqual(save_attempt_args['action_type'], action_type_sent)
        self.assertEqual(save_attempt_args['content'], content_sent)

        self.action_log_service_mock.update_action_log_with_response.assert_called_once()
        update_log_args = self.action_log_service_mock.update_action_log_with_response.call_args[1]
        self.assertEqual(update_log_args['action_id'], core_action_id)
        self.assertEqual(update_log_args['status'], "success") # 状态来自 response_event_data
        self.assertEqual(update_log_args['result_details'], {"platform_message_id": "pf_msg_success_123"})

        self.event_storage.save_event_document.assert_called_once()
        saved_event_to_events_table = self.event_storage.save_event_document.call_args[0][0]
        self.assertEqual(saved_event_to_events_table['event_id'], core_action_id)
        self.assertEqual(saved_event_to_events_table['event_type'], action_type_sent)
        self.assertEqual(saved_event_to_events_table['content'], content_sent)
        self.assertEqual(saved_event_to_events_table['timestamp'], update_log_args['response_timestamp'])

        self.assertEqual(self.thought_storage.update_action_status_in_thought_document.call_count, 2)
        logger.info(f"--- 结束测试: {self.id()} ---")

    async def test_platform_action_timeout_flow(self):
        logger.info(f"--- 开始测试: {self.id()} ---")
        action_description = "发送一个注定要超时的请求"
        thought_doc_key = f"thought_{uuid.uuid4().hex}"
        core_action_id = f"core_action_{uuid.uuid4().hex}"
        target_platform = "test_adapter_timeout"
        action_type_sent = "action.long.task"
        content_sent = [{"type": "text", "data": {"text": "这个会超时..."}}]

        action_to_send_to_adapter = create_mock_action_event_dict(
            action_id=core_action_id, platform=target_platform, event_type=action_type_sent, content_segs=content_sent
        )
        action_to_send_to_adapter["bot_id"] = "core_sender_timeout"
        action_to_send_to_adapter["conversation_info"]["conversation_id"] = "timeout_conv"

        # 模拟 get_action_log 在超时后返回相应的日志条目 (虽然 _execute_platform_action 中超时分支不直接读取)
        async def mock_get_action_log_timeout(action_id_param):
            if action_id_param == core_action_id:
                return {"action_id": core_action_id, "status": "timeout", "error_info": "Action response timed out"}
            return None
        self.action_log_service_mock.get_action_log.side_effect = mock_get_action_log_timeout


        with patch('src.action.action_handler.ACTION_RESPONSE_TIMEOUT_SECONDS', 0.05):
            was_response_received, exec_result_message = await self.action_handler._execute_platform_action(
                action_to_send=action_to_send_to_adapter,
                thought_doc_key=thought_doc_key,
                original_action_description=action_description
            )
        
        self.assertFalse(was_response_received, "超时后，_execute_platform_action 应返回 False")
        self.assertIn("响应超时", exec_result_message)
        
        self.action_log_service_mock.save_action_attempt.assert_called_once()
        self.action_log_service_mock.update_action_log_with_response.assert_called_once()
        update_log_args = self.action_log_service_mock.update_action_log_with_response.call_args[1]
        self.assertEqual(update_log_args['action_id'], core_action_id)
        self.assertEqual(update_log_args['status'], "timeout")
        self.assertEqual(update_log_args['error_info'], "Action response timed out")

        self.event_storage.save_event_document.assert_not_called()
        self.assertEqual(self.thought_storage.update_action_status_in_thought_document.call_count, 2)
        logger.info(f"--- 结束测试: {self.id()} ---")

if __name__ == '__main__':
    unittest.main(verbosity=2)
