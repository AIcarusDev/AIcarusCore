# src/action/action_handler.py
import asyncio
import io
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from src.action.components.pending_action_manager import PendingActionManager
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import find_files, generate_file_tree, parse_entity_uid
from src.config import config
from src.config.config_paths import PROJECT_ROOT
from src.core_communication.action_sender import ActionSender
from src.database import (
    ActionLogStorageService,
    EntityGraphService,
    EventStorageService,
    ThoughtStorageService,
)
from src.domain.models import ActionMetadata, ActionResult
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

INFO_GATHERING_ACTIONS = {"get_list", "get_group_info", "get_bot_profile", "get_history"}


class ActionHandler:
    """处理所有与动作相关的逻辑."""

    NORMALIZATION_ACTIONS: ClassVar[dict[str, str]] = {
        "delete_friend": "user_id",
        "leave_conversation": "group_id",
    }

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
        self._workspace_root: Path | None = None
        logger.info(f"{self.__class__.__name__} instance created (等待依赖注入).")

    def _initialize_workspace(self) -> None:
        if self._workspace_root is not None:
            return
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
        if self._workspace_root is None:
            self._initialize_workspace()
        return self._workspace_root

    def _resolve_safe_path(self, user_path: str) -> Path | None:
        workspace_root = self._get_safe_workspace_root()
        sanitized_path_str = user_path.strip().lstrip("/\\")
        candidate_path = workspace_root.joinpath(sanitized_path_str)
        try:
            real_workspace_root = os.path.realpath(self._workspace_root)
            real_candidate_path = os.path.realpath(candidate_path)
            common_prefix = os.path.commonpath([real_workspace_root, real_candidate_path])
            if os.path.realpath(common_prefix) == real_workspace_root:
                return Path(real_candidate_path)
            else:
                logger.error(
                    f"路径遍历攻击尝试被阻止！目标路径 '{real_candidate_path}' "
                    f"超出工作区 '{real_workspace_root}'。"
                )
                return None
        except ValueError:
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
        action_sender: ActionSender,
        entity_service: EntityGraphService,
        chat_session_manager: "ChatSessionManager",
        core_logic: "CoreLogic",
    ) -> None:
        """设置依赖服务."""
        self.thought_storage_service = thought_service
        self.action_log_service = action_log_service
        self.action_sender = action_sender
        self.entity_service = entity_service
        self.chat_session_manager = chat_session_manager
        self.core_logic = core_logic
        self.pending_action_manager = PendingActionManager(
            action_log_service=action_log_service,
            thought_storage_service=thought_service,
            event_storage_service=event_service,
            action_handler_instance=self,
        )
        self._initialize_workspace()
        logger.info("ActionHandler 的依赖已成功设置。")

    def set_thought_trigger(self, trigger_event: asyncio.Event | None) -> None:
        """设置主思维触发器."""
        self.thought_trigger = trigger_event
        if trigger_event:
            logger.info("ActionHandler 的主思维触发器已成功设置。")

    async def initialize_llm_clients(self) -> None:
        """初始化 LLM 客户端."""
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
        """处理动作响应."""
        if self.pending_action_manager:
            await self.pending_action_manager.handle_response(response_event_data)
        else:
            logger.error("PendingActionManager 未初始化，无法处理动作响应。")

    async def _handle_do_nothing_action(self, action_json: dict, doc_key: str) -> None:
        """处理 do_nothing 动作."""
        motivation = action_json["core"]["do_nothing"].get("motivation", "决定保持沉默")
        logger.info(f"AI 决定不行动，动机: {motivation}")
        if self.core_logic and (session := self.core_logic._get_current_session()):
            session.no_action_count += 1
            logger.debug(
                f"[{session.conversation_id}] 连续不发言计数器已递增至: {session.no_action_count}"
            )
        if self.thought_storage_service:
            await self.thought_storage_service.save_action_result_to_thought(
                thought_key=doc_key, result_text=f"决定不行动，原因：{motivation}"
            )

    async def _handle_local_action(
        self, platform_id: str, action_name: str, params: dict, doc_key: str
    ) -> None:
        """处理本地执行的动作 (如 qq.scroll)."""
        result_text = ""
        if platform_id == "qq" and action_name == "scroll":
            result_text = self._execute_local_scroll_action(platform_id, params)

        if self.thought_storage_service:
            await self.thought_storage_service.save_action_result_to_thought(
                thought_key=doc_key, result_text=result_text
            )
        if self.thought_trigger:
            logger.info(f"本地动作 '{platform_id}.{action_name}' 完成，立即触发新一轮思考。")
            self.thought_trigger.set()

    async def _handle_core_action_flow(self, action_name: str, params: dict, doc_key: str) -> None:
        """处理 'core' 命名空间下的动作."""
        result_text = await self._execute_core_action(action_name, params)
        if self.thought_storage_service:
            await self.thought_storage_service.save_action_result_to_thought(
                thought_key=doc_key, result_text=result_text
            )
        if self.thought_trigger:
            logger.info(f"核心动作 '{action_name}' 完成，立即触发新一轮思考。")
            self.thought_trigger.set()

    async def process_action_flow(
        self,
        action_id: str,
        doc_key_for_updates: str,
        action_json: dict[str, Any],
        metadata: ActionMetadata,
    ) -> None:
        """统一的行动处理流程，负责分发任务到具体的处理器."""
        logger.info(f"[探灯B] ActionHandler 收到的 action_json: {action_json}")
        logger.info(
            f"-- [Action ID: {action_id}] 开始处理行动流程 (动机: {metadata.motivation[:50]}...) --"
        )

        # Guard Clause 1: 处理 do_nothing
        if "do_nothing" in action_json.get("core", {}):
            await self._handle_do_nothing_action(action_json, doc_key_for_updates)
            return

        # Guard Clause 2: 验证动作格式
        if not (platform_id := next(iter(action_json), None)) or not (
            actions_to_process := action_json.get(platform_id)
        ):
            logger.info("AI决策的动作对象为空或格式不正确，无需执行。")
            if self.core_logic and (session := self.core_logic._get_current_session()):
                session.no_action_count += 1
            return

        action_name, params = next(iter(actions_to_process.items()))

        # --- 调度逻辑 ---
        if platform_id == "qq" and action_name == "scroll":
            await self._handle_local_action(platform_id, action_name, params, doc_key_for_updates)
        elif platform_id == "core":
            await self._handle_core_action_flow(action_name, params, doc_key_for_updates)
        else:
            await self._execute_platform_action_flow(
                platform_id, action_name, params, doc_key_for_updates, metadata
            )
            if action_name in INFO_GATHERING_ACTIONS and self.thought_trigger:
                logger.info(
                    f"信息获取类平台动作 '{platform_id}.{action_name}' 完成，立即触发新一轮思考。"
                )
                self.thought_trigger.set()

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
            "delete_workspace_file": self._execute_core_delete_workspace_file,
        }
        handler = file_op_handlers.get(action_name)
        if handler:
            logger.info(f"检测到核心文件操作 '{action_name}'，正在后台线程中执行...")
            return await asyncio.to_thread(handler, params)
        logger.error(f"收到了一个未知的核心动作: '{action_name}'")
        return f"错误：未知核心动作 '{action_name}'。"

    async def _execute_core_web_search(self, params: dict) -> str:
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
            items = [
                f"[{'DIR' if item.is_dir() else 'FILE'}] {item.name}"
                for item in safe_path.iterdir()
            ]
            if not items:
                return f"目录 '{path_str}' 是空的。"
            return f"目录 '{path_str}' 下的内容：\n" + "\n".join(items)
        except Exception as e:
            logger.error(f"列出文件时出错 ({path_str}): {e}", exc_info=True)
            return f"错误：列出文件时发生未知错误: {e}"

    def _validate_and_read_file_content(self, safe_path: Path, original_path: str) -> str:
        """验证路径是否为文件并读取其内容。失败时抛出 FileNotFoundError."""
        if not safe_path.is_file():
            raise FileNotFoundError(f"错误：路径 '{original_path}' 不是一个文件或不存在。")
        return safe_path.read_text(encoding="utf-8")

    def _execute_core_read_file(self, params: dict) -> str:
        path_str = params.get("path")
        if not path_str:
            return "错误：未提供要读取的文件路径。"
        safe_path = self._resolve_safe_path(path_str)
        if not safe_path:
            return f"错误：路径 '{path_str}' 不安全或无效。"
        try:
            content = self._validate_and_read_file_content(safe_path, path_str)
            return f"文件 '{path_str}' 的内容如下：\n---\n{content}\n---"
        except FileNotFoundError as e:
            return str(e)
        except Exception as e:
            logger.error(f"读取文件时出错 ({path_str}): {e}", exc_info=True)
            return f"错误：读取文件时发生未知错误: {e}"

    def _execute_core_write_file(self, params: dict) -> str:
        path_str = params.get("path")
        content = params.get("content")
        append = params.get("append", True)
        if not path_str or content is None:
            return "错误：未提供文件路径或写入内容。"
        safe_path = self._resolve_safe_path(path_str)
        if not safe_path:
            return f"错误：路径 '{path_str}' 不安全或无效。"
        try:
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with safe_path.open(mode, encoding="utf-8") as f:
                f.write(content)
            final_content = safe_path.read_text(encoding="utf-8")
            if len(final_content.encode("utf-8")) > MAX_CONTENT_PREVIEW_SIZE:
                final_content = final_content[:MAX_CONTENT_PREVIEW_SIZE] + "\n... [内容已截断]"
            action_desc = "追加内容到" if append else "覆写"
            return (
                f"成功！已{action_desc}文件 '{path_str}'。\n"
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
            original_content = self._validate_and_read_file_content(safe_path, path_str)
            if search_pattern not in original_content:
                return (
                    f"操作完成，但在文件 '{path_str}' 中未找到要替换的文本 '{search_pattern}'。\n"
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
                f"成功！已编辑文件 '{path_str}'，将所有 '{search_pattern}' "
                f"替换为 '{replace_string}'。\n目前文件的内容为:\n---\n{final_content_preview}\n---"
            )
        except FileNotFoundError as e:
            return str(e)
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
        log_buffer = io.StringIO()

        class FakeQueue:
            def put(self, msg: str) -> None:
                log_buffer.write(str(msg) + "\n")

        fake_log_queue = FakeQueue()
        try:
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
            found_files = find_files(
                str(safe_source_path), extensions, ignore_items, fake_log_queue
            )
            if not found_files:
                return f"在 '{source_path_str}' 路径下未找到符合条件的文件。"
            output_buffer = io.StringIO()
            output_buffer.write("=" * 80 + "\n")
            output_buffer.write(f"根目录: {safe_source_path}\n")
            output_buffer.write(f"共 {len(found_files)} 个文件\n")
            output_buffer.write("=" * 80 + "\n\n")
            tree_structure = generate_file_tree(str(safe_source_path), found_files, fake_log_queue)
            output_buffer.write("文件结构树:\n")
            output_buffer.write(tree_structure)
            output_buffer.write("\n\n" + "=" * 80 + "\n\n")
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
            final_content = output_buffer.getvalue()
            if len(final_content.encode("utf-8")) > MAX_AGGREGATE_SIZE:
                safe_cut_pos = final_content.rfind("\n" + "-" * 80, 0, MAX_AGGREGATE_SIZE)
                if safe_cut_pos != -1:
                    final_content = final_content[:safe_cut_pos]
                else:
                    final_content = final_content[:MAX_AGGREGATE_SIZE]
                final_content += "\n... [聚合内容过长，已在末尾截断]"
            return f"成功聚合了 '{source_path_str}' 的内容：\n{final_content}"
        except Exception as e:
            logger.error(f"聚合内容时出错 ({source_path_str}): {e}", exc_info=True)
            return f"错误：聚合内容时发生未知错误: {e}"

    def _get_id_from_params(self, action_name: str, params: dict) -> str | None:
        """根据动作名称，从参数字典中提取目标ID字符串."""
        id_key = "user_id" if "friend" in action_name else "group_id"
        return params.get(id_key)

    def _get_id_from_session(self) -> str | None:
        """如果在会话上下文中，则从中提取原生ID作为回退."""
        if self.core_logic and (session := self.core_logic._get_current_session()):
            if parsed_tuple := parse_entity_uid(session.conversation_id):
                # parse_entity_uid 返回 (platform, type, native_id)
                return parsed_tuple[2]

            logger.error(f"无法从当前会话的实体UID '{session.conversation_id}' 中解析出原生ID。")
            return None

    def _normalize_id_string(self, id_string: str, platform_id: str) -> str | None:
        """将一个可能是完整UID的字符串规范化为平台原生ID."""
        # 尝试将其作为完整的实体UID进行解析
        if parsed_tuple := parse_entity_uid(id_string):
            parsed_platform, _, native_id = parsed_tuple
            if parsed_platform != platform_id:
                logger.warning(
                    f"解析出的实体UID平台 '{parsed_platform}' 与当前动作平台 '{platform_id}' "
                    f"不匹配。将仍然使用其原生ID部分 '{native_id}'。"
                )
            return native_id
        # 如果无法解析，则假定它已经是平台原生ID
        return id_string

    def _resolve_target_id(self, action_name: str, params: dict, platform_id: str) -> str | None:
        """以清晰、可维护的方式解析出动作所需的目标原生ID.

        它会依次尝试从动作参数和当前会话上下文中获取ID，然后进行规范化处理。
        """
        # 步骤 1: 尝试从动作参数中获取ID
        raw_id = self._get_id_from_params(action_name, params)

        # 步骤 2: 如果参数中没有，则尝试从当前会话上下文中获取
        if not raw_id:
            raw_id = self._get_id_from_session()

        # 步骤 3: 验证是否成功获取ID，如果两种方式都失败了，则记录错误并返回
        if not raw_id:
            id_key = "user_id" if "friend" in action_name else "group_id"
            logger.error(f"动作 '{action_name}' 缺少必要的 '{id_key}' 且不在有效的会话上下文中。")
            return None

        # 步骤 4: 对获取到的ID字符串进行规范化处理，确保返回的是原生ID
        return self._normalize_id_string(raw_id, platform_id)

    async def _execute_platform_action_flow(
        self,
        platform_id: str,
        action_name: str,
        params: dict,
        doc_key_for_updates: str,
        metadata: ActionMetadata,
    ) -> None:
        """执行一个平台动作的完整流程，并传递元数据."""
        if not self.action_sender or platform_id not in self.action_sender.connected_adapters:
            error_msg = f"动作执行失败：平台 '{platform_id}' 理论上存在，但当前未连接。"
            logger.error(error_msg)
            if self.thought_storage_service:
                await self.thought_storage_service.save_action_result_to_thought(
                    thought_key=doc_key_for_updates, result_text=error_msg
                )
            return

        builder = platform_builder_registry.get_builder(platform_id)
        if not builder:
            logger.error(f"找不到平台 '{platform_id}' 的翻译官。")
            return
        if not self.entity_service:
            logger.error("EntityGraphService 未注入到 ActionHandler，无法获取祂的ID！")
            return
        if action_name in self.NORMALIZATION_ACTIONS:
            resolved_id = self._resolve_target_id(action_name, params, platform_id)
            if not resolved_id:
                error_msg = f"动作 '{action_name}' 执行失败：无法确定目标ID。"
                logger.error(error_msg)
                if self.thought_storage_service:
                    await self.thought_storage_service.save_action_result_to_thought(
                        thought_key=doc_key_for_updates, result_text=error_msg
                    )
                return
            id_key = self.NORMALIZATION_ACTIONS[action_name]
            params[id_key] = resolved_id
            logger.debug(f"已将动作 '{action_name}' 的目标ID归一化为: '{resolved_id}'")

        all_self_entities = await self.entity_service.get_all_self_entities()
        self_entity = next(
            (
                entity
                for entity in all_self_entities
                if entity.get("details", {}).get("platform") == platform_id
            ),
            None,
        )
        if not self_entity or not self_entity.get("details", {}).get("platform_id"):
            logger.error(f"无法为平台 '{platform_id}' 获取已安检的祂的客观实体ID。动作无法执行。")
            if self.thought_storage_service:
                await self.thought_storage_service.save_action_result_to_thought(
                    thought_key=doc_key_for_updates,
                    result_text=f"动作执行失败：我找不到自己在这个平台({platform_id})上的身份信息。",
                )
            return

        correct_bot_id = self_entity["details"]["platform_id"]
        action_event = builder.build_action_event(action_name, params, bot_id=correct_bot_id)
        if not action_event:
            logger.error(f"平台 '{platform_id}' 的翻译官不会翻译动作 '{action_name}'。")
            return

        # 将 metadata 传递给底层执行器
        await self._execute_platform_action(
            action_to_send=action_event.to_dict(),
            thought_doc_key=doc_key_for_updates,
            original_action_description=f"{platform_id}.{action_name}",
            metadata=metadata,
        )

    async def execute_simple_action(
        self,
        platform_id: str,
        action_name: str,
        params: dict,
        bot_id: str,
        description: str,
        motivation: str | None = None,
    ) -> ActionResult:
        """一个更简单的动作执行入口，供 MessageBuilder 等内部系统调用.

        它现在返回一个 ActionResult 领域模型对象.
        """
        builder = platform_builder_registry.get_builder(platform_id)
        if not builder:
            return ActionResult(
                action_id="",
                is_success=False,
                error_message=f"找不到平台 '{platform_id}' 的翻译官。",
            )

        action_event = builder.build_action_event(action_name, params, bot_id=bot_id)
        if not action_event:
            return ActionResult(
                action_id="",
                is_success=False,
                error_message=f"平台 '{platform_id}' 的翻译官不会翻译动作 '{action_name}'。",
            )

        # 内部调用时，我们自己创建一个 ActionMetadata
        metadata = ActionMetadata(
            motivation=motivation or "由内部系统（如MessageBuilder）发起",
            source_event_id=None,  # 内部调用通常没有直接的源事件
            source_thought_id=None,
        )

        return await self._execute_platform_action(
            action_to_send=action_event.to_dict(),
            thought_doc_key=None,
            original_action_description=description,
            metadata=metadata,
        )

    async def _execute_platform_action(
        self,
        action_to_send: dict[str, Any],
        thought_doc_key: str | None,
        original_action_description: str,
        metadata: ActionMetadata,
    ) -> ActionResult:
        """底层动作执行器：发送动作到适配器并等待响应."""
        core_action_id = action_to_send.setdefault("event_id", str(uuid.uuid4()))
        if not self.action_sender or not self.action_log_service or not self.pending_action_manager:
            return ActionResult(
                action_id=core_action_id,
                is_success=False,
                error_message="内部错误：核心服务不可用。",
            )

        event_type = action_to_send.get("event_type", "")
        platform = event_type.split(".")[1] if "." in event_type else "unknown"
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
                return ActionResult(
                    action_id=core_action_id,
                    is_success=False,
                    error_message=f"发送到适配器 '{platform}' 失败。",
                )
        except Exception as e:
            return ActionResult(
                action_id=core_action_id,
                is_success=False,
                error_message=f"发送平台动作时发生意外异常: {e}",
            )

        return await self.pending_action_manager.add_and_wait_for_action(
            action_id=core_action_id,
            thought_doc_key=thought_doc_key,
            original_action_description=original_action_description,
            action_to_send=action_to_send,
            metadata=metadata,
        )

    def _execute_local_scroll_action(self, platform_id: str, params: dict) -> str:
        params = params.get("params")
        if not params or params not in ["up", "down"]:
            return f"错误：收到无效的滚动方向 '{params}'。"
        if not self.chat_session_manager:
            return "错误：会话管理器未就绪，无法执行滚动。"
        if platform_id not in self.chat_session_manager.platform_view_states:
            return f"错误：找不到平台 '{platform_id}' 的视图状态。"
        state = self.chat_session_manager.platform_view_states[platform_id]
        current_offset = state.get("scroll_offset", 0)
        page_size = 10
        if params == "down":
            state["scroll_offset"] = current_offset + page_size
            action_desc = "向下"
        elif params == "up":
            state["scroll_offset"] = max(0, current_offset - page_size)
            action_desc = "向上"
        logger.info(f"平台 '{platform_id}' 视图已滚动, 新偏移量: {state['scroll_offset']}")
        return f"成功地将列表 {action_desc} 滚动了一页。"

    async def _execute_core_summarize_url(self, params: dict) -> str:
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
        system_prompt = URL_CONTEXT_SYSTEM_PROMPT
        user_prompt = URL_CONTEXT_USER_PROMPT.format(url=url, motivation=motivation)
        response = await self.url_context_agent_client.make_llm_request(
            prompt=user_prompt,
            system_prompt=system_prompt,
            is_stream=False,
            use_url_context=True,
        )
        return response.get("text", "访问URL失败或未返回任何信息。")

    def _execute_core_delete_workspace_file(self, params: dict) -> str:
        path_str = params.get("path")
        if not path_str:
            return "错误：未提供要删除的文件路径。"
        safe_path = self._resolve_safe_path(path_str)
        if not safe_path:
            return f"错误：路径 '{path_str}' 不安全或无效。"
        try:
            if not safe_path.exists():
                return f"操作完成：文件 '{path_str}' 本来就不存在。"
            if not safe_path.is_file():
                return f"错误：路径 '{path_str}' 是一个目录，此功能只能删除文件。"
            safe_path.unlink()
            return f"成功！已删除文件 '{path_str}'。"
        except Exception as e:
            logger.error(f"删除文件时出错 ({path_str}): {e}", exc_info=True)
            return f"错误：删除文件时发生未知错误: {e}"
