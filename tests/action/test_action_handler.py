# tests/action/test_action_handler.py

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture
from src.action.action_handler import ActionHandler
from src.domain.models import ActionMetadata

# 标记整个模块的所有测试都需要异步环境
pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_dependencies(mocker: MockerFixture) -> dict:
    """一个集中管理所有 ActionHandler 依赖项 mock 的 fixture。"""
    mocks = {
        "thought_service": mocker.AsyncMock(),
        "event_service": mocker.AsyncMock(),
        "action_log_service": mocker.AsyncMock(),
        "action_sender": mocker.AsyncMock(),
        "entity_service": mocker.AsyncMock(),
        "chat_session_manager": mocker.MagicMock(),
        "core_logic": mocker.MagicMock(),
        "sticker_service": mocker.AsyncMock(),
        "narrative_vectorizer": mocker.MagicMock(),
        "web_search_agent_client": mocker.AsyncMock(),
        "url_context_agent_client": mocker.AsyncMock(),
        "immediate_thought_trigger": mocker.MagicMock(spec=asyncio.Event),
    }
    # 为需要返回值的 mock 设置默认返回值
    mocks["web_search_agent_client"].make_llm_request.return_value = {"text": "mocked web search result"}
    return mocks


@pytest.fixture
def action_handler_instance(mock_dependencies: dict) -> ActionHandler:
    """创建一个 ActionHandler 实例并注入所有模拟的依赖。"""
    handler = ActionHandler()

    # 注入依赖
    handler.set_dependencies(
        thought_service=mock_dependencies["thought_service"],
        event_service=mock_dependencies["event_service"],
        action_log_service=mock_dependencies["action_log_service"],
        action_sender=mock_dependencies["action_sender"],
        entity_service=mock_dependencies["entity_service"],
        chat_session_manager=mock_dependencies["chat_session_manager"],
        core_logic=mock_dependencies["core_logic"],
        sticker_service=mock_dependencies["sticker_service"],
        narrative_vectorizer=mock_dependencies["narrative_vectorizer"],
    )
    handler.set_thought_trigger(mock_dependencies["immediate_thought_trigger"])

    # 手动设置LLM客户端
    handler.web_search_agent_client = mock_dependencies["web_search_agent_client"]
    handler.url_context_agent_client = mock_dependencies["url_context_agent_client"]

    return handler


class TestActionHandlerHelpers:
    """测试 ActionHandler 内部的辅助函数。"""

    def test_resolve_safe_path_valid(self, action_handler_instance: ActionHandler, mocker: MockerFixture):
        """测试一个安全、有效的路径。"""
        # 准备
        workspace_root = action_handler_instance._get_safe_workspace_root()
        mocker.patch('os.path.realpath', side_effect=lambda p: str(p)) # 简单模拟 realpath

        # 执行
        safe_path = action_handler_instance._resolve_safe_path("project/main.py")

        # 断言
        assert safe_path is not None
        assert safe_path == workspace_root / "project/main.py"

    def test_resolve_safe_path_traversal_attack(self, action_handler_instance: ActionHandler, mocker: MockerFixture):
        """测试路径遍历攻击是否被阻止。"""
        # 准备
        workspace_root = action_handler_instance._get_safe_workspace_root()
        # 模拟 ../../ 向上遍历超出了工作区
        mocker.patch('os.path.realpath', side_effect=lambda p: str(p).replace('../', ''))

        def mock_commonpath(paths):
            # 如果路径包含 'etc', 模拟它超出了 common path
            if any('etc' in str(p) for p in paths):
                return "/different/path"
            return str(workspace_root)
        mocker.patch('os.path.commonpath', side_effect=mock_commonpath)

        # 执行
        unsafe_path = action_handler_instance._resolve_safe_path("../../etc/passwd")

        # 断言
        assert unsafe_path is None


class TestProcessActionFlowDispatch:
    """测试 process_action_flow 的分发逻辑。"""

    @pytest.fixture
    def mock_action_handler_methods(self, action_handler_instance: ActionHandler, mocker: MockerFixture) -> dict:
        """使用 mocker.spy 来监视内部方法的调用情况。"""
        spies = {
            "_handle_do_nothing_action": mocker.spy(action_handler_instance, "_handle_do_nothing_action"),
            "_handle_core_action_flow": mocker.spy(action_handler_instance, "_handle_core_action_flow"),
            "_execute_platform_action_flow": mocker.spy(action_handler_instance, "_execute_platform_action_flow"),
        }
        return spies

    async def test_dispatch_do_nothing(self, action_handler_instance: ActionHandler, mock_action_handler_methods: dict):
        """测试 do_nothing 动作是否被正确分发。"""
        # 准备
        action_json = {"core": {"do_nothing": {"motivation": "test"}}}
        metadata = ActionMetadata(motivation="test")

        # 执行
        await action_handler_instance.process_action_flow("action_id_1", "doc_key_1", action_json, metadata)

        # 断言
        mock_action_handler_methods["_handle_do_nothing_action"].assert_called_once()
        mock_action_handler_methods["_handle_core_action_flow"].assert_not_called()
        mock_action_handler_methods["_execute_platform_action_flow"].assert_not_called()




    async def test_dispatch_core_action(self, action_handler_instance: ActionHandler, mock_action_handler_methods: dict, mock_dependencies: dict):
        """测试核心动作 (如 web_search) 是否被正确分发。"""
        # 准备
        action_json = {"core": {"web_search": {"query": "test query", "motivation": "research"}}}
        metadata = ActionMetadata(motivation="research")

        # 执行
        await action_handler_instance.process_action_flow("action_id_2", "doc_key_2", action_json, metadata)

        # 断言
        mock_action_handler_methods["_handle_do_nothing_action"].assert_not_called()
        mock_action_handler_methods["_handle_core_action_flow"].assert_called_once()
        mock_action_handler_methods["_execute_platform_action_flow"].assert_not_called()

        # 验证 LLM 客户端是否被调用
        web_search_client = mock_dependencies["web_search_agent_client"]
        web_search_client.make_llm_request.assert_awaited_once()


    async def test_dispatch_platform_action(
            self,
            action_handler_instance: ActionHandler,
            mock_action_handler_methods: dict,
            mock_dependencies: dict
            ) -> None:
        """测试平台动作是否被正确分发。"""
        # 准备
        action_json = {"qq": {"send_message": {"content": "hello"}}}
        metadata = ActionMetadata(motivation="greeting")

        # 模拟 ActionSender 的状态
        mock_dependencies["action_sender"].connected_adapters = {"qq": MagicMock()}
        # 模拟 Entity Service 返回自身实体
        mock_dependencies["entity_service"].get_all_self_entities.return_value = [
            {"details": {"platform": "qq", "platform_id": "12345"}}
        ]
        # 模拟 PendingActionManager 的行为
        action_handler_instance.pending_action_manager = AsyncMock()


        # 执行
        await action_handler_instance.process_action_flow("action_id_3", "doc_key_3", action_json, metadata)

        # 断言
        mock_action_handler_methods["_handle_do_nothing_action"].assert_not_called()
        mock_action_handler_methods["_handle_core_action_flow"].assert_not_called()
        # 这里我们spy的是 _execute_platform_action_flow，它是调用 pending_action_manager 的入口
        mock_action_handler_methods["_execute_platform_action_flow"].assert_called_once()


class TestActionHandlerFileSystem:
    """使用 pyfakefs 测试文件操作。"""

    def test_write_and_read_file(self, action_handler_instance: ActionHandler, fs):
        """测试写入和读取文件。fs fixture 由 pyfakefs 提供。"""
        # 准备
        workspace_root = action_handler_instance._get_safe_workspace_root()
        fs.create_dir(workspace_root) # 创建虚拟的工作区目录

        file_path = "test_dir/output.txt"
        content = "Hello, AIcarus!"

        # 执行写入
        write_params = {"path": file_path, "content": content, "append": False}
        write_result = action_handler_instance._execute_core_write_file(write_params)

        # 断言写入结果
        assert "成功！已覆写文件" in write_result
        assert content in write_result

        # 验证虚拟文件系统中的文件内容
        full_path = workspace_root / file_path
        assert full_path.exists()
        assert full_path.read_text() == content

        # 执行读取
        read_params = {"path": file_path}
        read_result = action_handler_instance._execute_core_read_file(read_params)

        # 断言读取结果
        assert f"文件 '{file_path}' 的内容如下" in read_result
        assert content in read_result

