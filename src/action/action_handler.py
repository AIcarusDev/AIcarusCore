# 文件: src/action/action_handler.py (竞速模式适配版 V1.0)
import asyncio
import io
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.action.components.pending_action_manager import PendingActionManager
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import find_files, generate_file_tree
from src.config import config
from src.config.config_paths import PROJECT_ROOT
from src.core_communication.action_sender import ActionSender
from src.database import (
    ActionLogStorageService,
    ConversationStorageService,
    EntityGraphService,
    EventStorageService,
    ThoughtStorageService,
)
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.platform_builders.registry import platform_builder_registry
from src.prompt_templates.url_context import URL_CONTEXT_SYSTEM_PROMPT, URL_CONTEXT_USER_PROMPT
from src.prompt_templates.web_search import WEB_SEARCH_SYSTEM_PROMPT, WEB_SEARCH_USER_PROMPT

if TYPE_CHECKING:
    from src.core_logic.consciousness_flow import CoreLogic
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)

ACTION_RESPONSE_TIMEOUT_SECONDS = 30
MAX_CONTENT_PREVIEW_SIZE = 32768
MAX_AGGREGATE_SIZE = 65535


class ActionHandler:
    """处理所有与动作相关的逻辑.

    它现在是一个纯粹的动作执行器，不再负责触发思考循环.
    """

    def __init__(self) -> None:
        self.web_search_agent_client: ProcessorClient | None = None
        self.url_context_agent_client: ProcessorClient | None = None
        self.action_sender: ActionSender | None = None
        self.thought_storage_service: ThoughtStorageService | None = None
        self.action_log_service: ActionLogStorageService | None = None
        self.thought_trigger: asyncio.Event | None = None
        self.pending_action_manager: PendingActionManager | None = None
        self.chat_session_manager: ChatSessionManager | None = None
        self.core_logic: CoreLogic | None = None
        self.entity_service: EntityGraphService | None = None
        logger.info(f"{self.__class__.__name__} instance created.")
        self._workspace_root: Path | None = None
        logger.info(f"{self.__class__.__name__} instance created (等待依赖注入).")

    def _initialize_workspace(self) -> None:
        """一个全新的、专门用来初始化工作区路径的私有方法.

        它必须在所有依赖注入完成后被调用!
        """
        if self._workspace_root is not None:
            return  # 防止重复初始化

        workspace_path_from_config = config.runtime_environment.workspace_root
        sanitized_workspace_path = workspace_path_from_config.lstrip("/\\")
        if sanitized_workspace_path != workspace_path_from_config:
            logger.warning(
                f"检测到工作区路径 '{workspace_path_from_config}' 以斜杠开头，"
                f"已自动修正为 '{sanitized_workspace_path}'。建议直接修改 config.toml。"
            )

        self._workspace_root = (PROJECT_ROOT / workspace_path_from_config).resolve()
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        logger.info(f"文件操作沙箱已通过延迟初始化成功定位，根目录: {self._workspace_root}")

    def _get_safe_workspace_root(self) -> Path:
        """一个安全的获取器，确保在使用 _workspace_root 之前它一定被初始化了."""
        if self._workspace_root is None:
            # 这是我们的保险丝！
            self._initialize_workspace()
        return self._workspace_root

    def _resolve_safe_path(self, user_path: str) -> Path | None:
        """解析用户提供的相对路径，并确保它在安全的工作区内.

        这是防止路径遍历攻击 (../) 和根目录解析问题的关键！
        """
        # 1. 通过安全的获取器
        workspace_root = self._get_safe_workspace_root()

        sanitized_path_str = user_path.strip().lstrip("/\\")
        candidate_path = workspace_root.joinpath(sanitized_path_str)

        # 2. 严防死守！使用 os.path.commonpath 来进行最严格的检查。
        #    这个函数会告诉我们两个路径的共同祖先是谁。
        #    如果共同祖先不是我们的工作区根目录，那绝对有问题！
        #    这能完美防御 "../" 这种越狱小花招。
        try:
            # os.path.realpath 会解析所有符号链接，确保我们得到的是物理真实路径
            real_workspace_root = os.path.realpath(self._workspace_root)
            real_candidate_path = os.path.realpath(candidate_path)

            common_prefix = os.path.commonpath([real_workspace_root, real_candidate_path])

            if os.path.realpath(common_prefix) == real_workspace_root:
                # 只有当共同前缀就是我们的工作区时，才证明这个路径是安全的
                return Path(real_candidate_path)
            else:
                # 如果共同前缀不是工作区，说明路径已经跑到外面去了！
                logger.error(
                    f"路径遍历攻击尝试被阻止！目标路径 '{real_candidate_path}' "
                    f"超出工作区 '{real_workspace_root}'。"
                )
                return None
        except ValueError:
            # 如果两个路径在不同盘符（比如 C: 和 D:），commonpath 会抛出 ValueError
            logger.error(f"路径 '{candidate_path}' 与工作区不在同一驱动器上，操作被拒绝。")
            return None
        except Exception as e:
            logger.error(f"解析安全路径时发生未知错误: {e}", exc_info=True)
            return None

    def set_dependencies(
        self,
        thought_service: ThoughtStorageService,
        event_service: EventStorageService,
        action_log_service: ActionLogStorageService,
        conversation_service: ConversationStorageService,
        action_sender: ActionSender,
        entity_service: EntityGraphService,
        chat_session_manager: "ChatSessionManager",
        core_logic: "CoreLogic",
    ) -> None:
        """设置 ActionHandler 的依赖服务."""
        self.thought_storage_service = thought_service
        self.action_log_service = action_log_service
        self.action_sender = action_sender
        self.entity_service = entity_service
        self.chat_session_manager = chat_session_manager
        self.core_logic = core_logic
        # 关键：将 ActionHandler 自身的实例传递给 PendingActionManager
        self.pending_action_manager = PendingActionManager(
            action_log_service=action_log_service,
            thought_storage_service=thought_service,
            event_storage_service=event_service,
            conversation_service=conversation_service,
            action_handler_instance=self,  # 把自己传进去
        )
        self._initialize_workspace()
        logger.info("ActionHandler 的依赖已成功设置。")

    def set_thought_trigger(self, trigger_event: asyncio.Event | None) -> None:
        """设置主思维触发器 (在竞速模式下，此触发器主要由CoreLogic自身管理)."""
        self.thought_trigger = trigger_event
        if trigger_event:
            logger.info("ActionHandler 的主思维触发器已成功设置。")

    async def initialize_llm_clients(self) -> None:
        """按需初始化LLM客户端."""
        if self.web_search_agent_client and self.url_context_agent_client:
            return
        from src.action.components.llm_client_factory import LLMClientFactory

        factory = LLMClientFactory()
        try:
            if not self.web_search_agent_client:
                self.web_search_agent_client = factory.create_client(purpose_key="web_search_agent")
                logger.info("ActionHandler 的 web_search_agent_client 初始化成功。")
            if not self.url_context_agent_client:
                self.url_context_agent_client = factory.create_client(
                    purpose_key="url_context_agent"
                )
                logger.info("ActionHandler 的 url_context_agent_client 初始化成功。")
        except RuntimeError as e:
            logger.critical(f"为 ActionHandler 初始化LLM客户端失败: {e}")
            raise

    async def handle_action_response(self, response_event_data: dict[str, Any]) -> None:
        """处理来自适配器的动作响应."""
        if self.pending_action_manager:
            await self.pending_action_manager.handle_response(response_event_data)
        else:
            logger.error("PendingActionManager 未初始化，无法处理动作响应。")

    async def process_action_flow(
        self,
        action_id: str,
        doc_key_for_updates: str,
        action_json: dict[str, Any],
    ) -> None:
        """统一的行动处理流程.

        它现在不再触发思考，只负责执行动作并将结果写回思想点.
        """
        logger.info(f"[探灯B] ActionHandler 收到的 action_json: {action_json}")
        logger.info(f"--- [Action ID: {action_id}] 开始处理行动流程 ---")

        # 1. 检查是否为“不行动”决策
        if "do_nothing" in action_json.get("core", {}):
            motivation = action_json["core"]["do_nothing"].get("motivation", "决定保持沉默")
            logger.info(f"AI 决定不行动，动机: {motivation}")
            if self.thought_storage_service:
                await self.thought_storage_service.save_action_result_to_thought(
                    thought_key=doc_key_for_updates,
                    result_text=f"决定不行动，原因：{motivation}",
                )
            return

        # 2. 动态解析出需要执行的动作
        # 我们不再写死平台名，而是动态地查找
        core_actions = action_json.get("core", {})
        # 找到第一个不是'core'的键和值，作为平台动作
        platform_actions_tuple = next(
            ((key, value) for key, value in action_json.items() if key != "core"),
            (None, None),
        )
        platform_id_from_action, platform_actions = platform_actions_tuple

        actions_to_process = platform_actions or core_actions

        if not actions_to_process:
            logger.info("AI决策的动作对象为空，无需执行。")
            return

        platform_id = platform_id_from_action if platform_actions else "core"
        action_name, params = next(iter(actions_to_process.items()))

        if platform_id == "core":
            result_text = await self._execute_core_action(action_name, params)

            # 将结果写回思想点
            if self.thought_storage_service:
                await self.thought_storage_service.save_action_result_to_thought(
                    thought_key=doc_key_for_updates,
                    result_text=result_text,
                )

            # 核心动作执行完，立即触发思考！
            if self.thought_trigger:
                logger.info(
                    f"核心动作 '{action_name}' 完成 (Action ID: {action_id})，立即触发新一轮思考。"
                )
                self.thought_trigger.set()
        else:
            await self._execute_platform_action_flow(
                platform_id, action_name, params, doc_key_for_updates
            )

    async def _execute_core_action(self, action_name: str, params: dict) -> str:
        """核心动作的统一分发中心."""
        if action_name == "web_search":
            return await self._execute_core_web_search(params)
        if action_name == "summarize_url":
            return await self._execute_core_summarize_url(params)

        file_op_handlers = {
            "list_files": self._execute_core_list_files,
            "read_file": self._execute_core_read_file,
            "write_file": self._execute_core_write_file,
            "edit_file": self._execute_core_edit_file,
            "get_aggregated_content": self._execute_core_get_aggregated_content,
        }

        handler = file_op_handlers.get(action_name)
        if handler:
            logger.info(f"检测到核心文件操作 '{action_name}'，正在后台线程中执行...")
            # 将同步的文件操作函数放到独立的线程中运行，防止阻塞主事件循环
            return await asyncio.to_thread(handler, params)

        logger.error(f"收到了一个未知的核心动作: '{action_name}'")
        return f"错误：未知核心动作 '{action_name}'。"

    async def _execute_core_web_search(self, params: dict) -> str:
        """执行核心的网页搜索动作，并直接返回结果字符串."""
        await self.initialize_llm_clients()
        query = params.get("query")
        motivation = params.get("motivation", "没有明确动机")

        if not query or not self.web_search_agent_client:
            result_text = "动作执行失败：LLM想搜索但没提供关键词，或者搜索代理客户端未初始化。"
            logger.warning(result_text)
            return result_text

        logger.info(f"正在调用搜索代理LLM，查询: '{query}'")
        system_prompt = WEB_SEARCH_SYSTEM_PROMPT.format(bot_name=config.persona.bot_name)
        user_prompt = WEB_SEARCH_USER_PROMPT.format(query=query, motivation=motivation)
        response = await self.web_search_agent_client.make_llm_request(
            prompt=user_prompt, system_prompt=system_prompt, is_stream=False, use_google_search=True
        )
        return response.get("text", "搜索失败或未返回任何信息。")

        # 4. 【移除】不再从此触发思考
        # if self.thought_trigger:
        #     logger.info(f"行动流程处理完毕 (Action ID: {action_id})，触发思考。")
        #     self.thought_trigger.set()

    def _execute_core_list_files(self, params: dict) -> str:
        path_str = params.get("path", ".")
        safe_path = self._resolve_safe_path(path_str)
        if not safe_path:
            return f"错误：路径 '{path_str}' 不安全或无效。"

        try:
            if not safe_path.exists():
                return f"错误：路径 '{path_str}' 不存在。"
            if not safe_path.is_dir():
                return f"错误：'{path_str}' 不是一个目录。"

            items = []
            for item in safe_path.iterdir():
                item_type = "DIR" if item.is_dir() else "FILE"
                items.append(f"[{item_type}] {item.name}")

            if not items:
                return f"目录 '{path_str}' 是空的。"
            return f"目录 '{path_str}' 下的内容：\n" + "\n".join(items)
        except Exception as e:
            logger.error(f"列出文件时出错 ({path_str}): {e}", exc_info=True)
            return f"错误：列出文件时发生未知错误: {e}"

    def _execute_core_read_file(self, params: dict) -> str:
        path_str = params.get("path")
        if not path_str:
            return "错误：未提供要读取的文件路径。"

        safe_path = self._resolve_safe_path(path_str)
        if not safe_path:
            return f"错误：路径 '{path_str}' 不安全或无效。"

        try:
            if not safe_path.is_file():
                return f"错误：路径 '{path_str}' 不是一个文件或不存在。"

            content = safe_path.read_text(encoding="utf-8")
            return f"文件 '{path_str}' 的内容如下：\n---\n{content}\n---"
        except Exception as e:
            logger.error(f"读取文件时出错 ({path_str}): {e}", exc_info=True)
            return f"错误：读取文件时发生未知错误: {e}"

    def _execute_core_write_file(self, params: dict) -> str:
        path_str = params.get("path")
        content = params.get("content")
        append = params.get("append", True)  # 默认为追加模式

        if not path_str or content is None:
            return "错误：未提供文件路径或写入内容。"

        safe_path = self._resolve_safe_path(path_str)
        if not safe_path:
            return f"错误：路径 '{path_str}' 不安全或无效。"

        try:
            # 确保父目录存在
            safe_path.parent.mkdir(parents=True, exist_ok=True)

            mode = "a" if append else "w"
            with safe_path.open(mode, encoding="utf-8") as f:
                f.write(content)
            final_content = safe_path.read_text(encoding="utf-8")

            # 检查内容长度，如果太长就截断
            if len(final_content.encode("utf-8")) > MAX_CONTENT_PREVIEW_SIZE:
                # 按字符截断，而不是字节，避免截断半个汉字
                final_content = final_content[:MAX_CONTENT_PREVIEW_SIZE] + "\n... [内容已截断]"

            action_desc = "追加内容到" if params.get("append", True) else "覆写"
            return (
                f"成功！已{action_desc}文件 '{params.get('path')}'。\n"
                f"目前文件的内容为:\n---\n{final_content}\n---"
            )
        except Exception as e:
            logger.error(f"写入文件时出错 ({path_str}): {e}", exc_info=True)
            return f"错误：写入文件时发生未知错误: {e}"

    def _execute_core_edit_file(self, params: dict) -> str:
        path_str = params.get("path")
        search_pattern = params.get("search_pattern")
        replace_string = params.get("replace_string")

        if not all([path_str, search_pattern, replace_string is not None]):
            return "错误：缺少编辑文件所需的参数。"

        safe_path = self._resolve_safe_path(path_str)
        if not safe_path:
            return f"错误：路径 '{path_str}' 不安全或无效。"

        try:
            if not safe_path.is_file():
                return f"错误：路径 '{path_str}' 不是一个文件或不存在。"

            original_content = safe_path.read_text(encoding="utf-8")
            if search_pattern not in original_content:
                return (
                    f"操作完成，但在文件 '{params.get('path')}' "
                    f"中未找到要替换的文本 '{search_pattern}'。\n"
                    f"目前文件内容未改变:\n---\n{original_content[:MAX_CONTENT_PREVIEW_SIZE]}\n---"
                )

            new_content = original_content.replace(search_pattern, replace_string)

            safe_path.write_text(new_content, encoding="utf-8")

            final_content_preview = new_content
            if len(final_content_preview.encode("utf-8")) > MAX_CONTENT_PREVIEW_SIZE:
                final_content_preview = (
                    final_content_preview[:MAX_CONTENT_PREVIEW_SIZE] + "\n... [内容已截断]"
                )

            return (
                f"成功！已编辑文件 '{path_str}'，"
                f"将所有 '{search_pattern}' 替换为 '{replace_string}'。"
                f"\n目前文件的内容为:\n---\n{final_content_preview}\n---"
            )
        except Exception as e:
            logger.error(f"编辑文件时出错 ({path_str}): {e}", exc_info=True)
            return f"错误：编辑文件时发生未知错误: {e}"

    def _execute_core_get_aggregated_content(self, params: dict) -> str:
        if find_files is None or generate_file_tree is None:
            return "错误：代码聚合功能的核心模块未能加载，无法执行此操作。"

        source_path_str = params.get("source_path")
        if not source_path_str:
            return "错误：缺少源路径参数。"

        safe_source_path = self._resolve_safe_path(source_path_str)
        if not safe_source_path:
            return f"错误：源路径 '{source_path_str}' 不安全或无效。"

        # 使用 io.StringIO 作为内存中的“日志队列”，避免打印到控制台
        log_buffer = io.StringIO()

        class FakeQueue:
            def put(self, msg: str) -> None:
                log_buffer.write(str(msg) + "\n")

        fake_log_queue = FakeQueue()

        try:
            # 借用 CodeAggregatorAPI 的默认配置
            api_defaults = {
                "extensions": [".py", ".md", ".txt", ".json", ".toml", ".yaml"],
                "ignore_items": {
                    "venv",
                    "__pycache__",
                    ".git",
                    ".vscode",
                    "node_modules",
                    "dist",
                    "build",
                    ".pytest_cache",
                    "output",
                },
            }

            extensions = params.get("extensions", api_defaults["extensions"])
            ignore_items = set(params.get("ignore_items", [])) | api_defaults["ignore_items"]

            # 1. 查找文件
            found_files = find_files(
                str(safe_source_path), extensions, ignore_items, fake_log_queue
            )

            if not found_files:
                return f"在 '{source_path_str}' 路径下未找到符合条件的文件。"

            # 2. 在内存中构建聚合内容
            # 我们使用 StringIO 来模拟一个文件对象
            output_buffer = io.StringIO()

            # --- 写入头部信息 ---
            output_buffer.write("=" * 80 + "\n")
            output_buffer.write(f"根目录: {safe_source_path}\n")
            output_buffer.write(f"共 {len(found_files)} 个文件\n")
            output_buffer.write("=" * 80 + "\n\n")

            # --- 写入文件树 ---
            tree_structure = generate_file_tree(str(safe_source_path), found_files, fake_log_queue)
            output_buffer.write("文件结构树:\n")
            output_buffer.write(tree_structure)
            output_buffer.write("\n\n" + "=" * 80 + "\n\n")

            # --- 写入每个文件的内容 ---
            for file_path in found_files:
                output_buffer.write("-" * 80 + "\n")
                output_buffer.write(f"文件路径: {file_path}\n")
                output_buffer.write("-" * 80 + "\n\n")
                try:
                    with open(file_path, encoding="utf-8", errors="ignore") as input_file:
                        content = input_file.read()
                        lang = os.path.splitext(file_path)[1].lstrip(".")
                        output_buffer.write(f"```{lang}\n")
                        output_buffer.write(content)
                        output_buffer.write("\n```\n\n")
                except Exception as e:
                    output_buffer.write(f"!!! 读取文件时出错: {file_path} -> {e} !!!\n\n")

            # 3. 从内存中获取最终的字符串
            final_content = output_buffer.getvalue()

            # 对最终内容进行截断，防止撑爆LLM的上下文窗口
            if len(final_content.encode("utf-8")) > MAX_AGGREGATE_SIZE:
                # 智能截断：从末尾开始找，找到一个文件分隔符，从那里截断
                # 这样可以保证最后一个文件是完整的
                safe_cut_pos = final_content.rfind("\n" + "-" * 80, 0, MAX_AGGREGATE_SIZE)
                if safe_cut_pos != -1:
                    final_content = final_content[:safe_cut_pos]
                else:  # 如果找不到，就硬截断
                    final_content = final_content[:MAX_AGGREGATE_SIZE]
                final_content += "\n... [聚合内容过长，已在末尾截断]"

            return f"成功聚合了 '{source_path_str}' 的内容：\n{final_content}"

        except Exception as e:
            logger.error(f"聚合内容时出错 ({source_path_str}): {e}", exc_info=True)
            return f"错误：聚合内容时发生未知错误: {e}"

    async def _execute_platform_action_flow(
        self, platform_id: str, action_name: str, params: dict, doc_key_for_updates: str
    ) -> None:
        """执行一个平台动作的完整流程：构建->发送->等待响应."""
        if not self.action_sender or platform_id not in self.action_sender.connected_adapters:
            error_msg = f"动作执行失败：平台 '{platform_id}' 理论上存在，但当前未连接。"
            logger.error(error_msg)
            if self.thought_storage_service:
                await self.thought_storage_service.save_action_result_to_thought(
                    thought_key=doc_key_for_updates,
                    result_text=error_msg,
                )
            return

        builder = platform_builder_registry.get_builder(platform_id)
        if not builder:
            logger.error(f"找不到平台 '{platform_id}' 的翻译官。")
            return

        if not self.entity_service:
            logger.error("EntityGraphService 未注入到 ActionHandler，无法获取祂的ID！")
            return

        self_entity = await self.entity_service.get_self_entity_for_platform(platform_id)

        if not self_entity or not self_entity.get("platform_id"):  # <--- (±) 使用新的变量
            logger.error(f"无法为平台 '{platform_id}' 获取已安检的祂的客观实体ID。动作无法执行。")
            await self.thought_storage_service.save_action_result_to_thought(
                thought_key=doc_key_for_updates,
                result_text=f"动作执行失败：我找不到自己在这个平台({platform_id})上的身份信息。",
            )
            return

        correct_bot_id = self_entity["platform_id"]  # <--- (±) 从新的变量中获取ID
        action_event = builder.build_action_event(action_name, params, bot_id=correct_bot_id)
        if not action_event:
            logger.error(f"平台 '{platform_id}' 的翻译官不会翻译动作 '{action_name}'。")
            return

        await self._execute_platform_action(
            action_to_send=action_event.to_dict(),
            thought_doc_key=doc_key_for_updates,
            original_action_description=f"{platform_id}.{action_name}",
        )

    async def execute_simple_action(
        self,
        platform_id: str,
        action_name: str,
        params: dict,
        bot_id: str,
        description: str,
        motivation: str | None = None,
    ) -> tuple[bool, Any]:
        """一个更简单的动作执行入口，供 MessageBuilder 等内部系统调用.

        它会返回执行结果和包含 action_id 的 payload.
        """
        builder = platform_builder_registry.get_builder(platform_id)
        if not builder:
            return False, {"error": f"找不到平台 '{platform_id}' 的翻译官。"}

        action_event = builder.build_action_event(action_name, params, bot_id=bot_id)
        if not action_event:
            return False, {"error": f"平台 '{platform_id}' 的翻译官不会翻译动作 '{action_name}'。"}

        success, message_payload = await self._execute_platform_action(
            action_to_send=action_event.to_dict(),
            thought_doc_key=None,
            original_action_description=description,
            motivation=motivation,
        )

        if isinstance(message_payload, dict):
            message_payload["action_id"] = action_event.event_id

        return success, message_payload

    async def _execute_platform_action(
        self,
        action_to_send: dict[str, Any],
        thought_doc_key: str | None,
        original_action_description: str,
        motivation: str | None = None,
    ) -> tuple[bool, Any]:
        """底层动作执行器：发送动作到适配器并等待响应."""
        if not self.action_sender or not self.action_log_service or not self.pending_action_manager:
            return False, {"error": "内部错误：核心服务不可用。"}

        event_type = action_to_send.get("event_type", "")
        platform = event_type.split(".")[1] if "." in event_type else "unknown"
        core_action_id = action_to_send.setdefault("event_id", str(uuid.uuid4()))
        timestamp = int(time.time() * 1000)
        action_to_send["timestamp"] = timestamp
        bot_id_for_log = action_to_send.get("bot_id")
        if not bot_id_for_log:
            logger.error(
                f"严重逻辑错误：动作事件中缺少 bot_id！无法记录日志。事件: {action_to_send}"
            )
            bot_id_for_log = "error_missing_bot_id"

        await self.action_log_service.save_action_attempt(
            action_id=core_action_id,
            action_type=event_type,
            timestamp=timestamp,
            bot_id=bot_id_for_log,
            platform=platform,
            conversation_id=action_to_send.get("conversation_info", {}).get(
                "conversation_id", "unknown_conv_id"
            ),
            content=action_to_send.get("content", []),
        )
        try:
            send_success = await self.action_sender.send_action_to_adapter_by_id(
                platform, action_to_send
            )
            if not send_success:
                return False, {"error": f"发送到适配器 '{platform}' 失败。"}
        except Exception as e:
            return False, {"error": f"发送平台动作时发生意外异常: {e}"}

        success, result_payload = await self.pending_action_manager.add_and_wait_for_action(
            action_id=core_action_id,
            thought_doc_key=thought_doc_key,
            original_action_description=original_action_description,
            action_to_send=action_to_send,
            motivation=motivation,
        )

        if isinstance(result_payload, dict):
            result_payload["action_id"] = core_action_id

        return success, result_payload

    async def _execute_core_summarize_url(self, params: dict) -> str:
        """执行核心的 URL 总结动作，并直接返回结果字符串."""
        await self.initialize_llm_clients()
        url = params.get("url")
        motivation = params.get("motivation", "没有明确动机")

        if not url or not self.url_context_agent_client:
            result_text = (
                "动作执行失败：LLM想访问URL但没提供网址，或者URL上下文代理客户端未初始化。"
            )
            logger.warning(result_text)
            return result_text

        logger.info(f"正在调用 URL 上下文代理LLM，目标URL: '{url}'")

        # 使用新的 prompt 模板
        system_prompt = URL_CONTEXT_SYSTEM_PROMPT
        # 在用户 prompt 中直接嵌入 URL，Gemini 会自动识别并提取
        user_prompt = URL_CONTEXT_USER_PROMPT.format(url=url, motivation=motivation)

        # 调用 LLM，并开启 use_url_context 功能
        response = await self.url_context_agent_client.make_llm_request(
            prompt=user_prompt,
            system_prompt=system_prompt,
            is_stream=False,
            use_url_context=True,  # 关键！开启 URL 上下文功能
        )
        return response.get("text", "访问URL失败或未返回任何信息。")
