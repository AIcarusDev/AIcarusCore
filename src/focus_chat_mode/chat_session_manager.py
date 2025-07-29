# src/focus_chat_mode/chat_session_manager.py
# 聊天会话管理器模块，用于管理聊天会话的生命周期和相关操作。
import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Optional

from aicarus_protocols import Event
from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.config.aicarus_configs import FocusChatModeSettings
from src.database import ConversationStorageService
from src.database.services.event_storage_service import EventStorageService
from src.database.services.summary_storage_service import SummaryStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as LLMProcessorClient

from .chat_session import ChatSession

if TYPE_CHECKING:
    # 引入智能中断系统模块，用于类型提示。
    from src.common.intelligent_interrupt_system.intelligent_interrupter import (
        IntelligentInterrupter,
    )
    from src.common.summarization_observation.summarization_service import SummarizationService
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow
    from src.core_logic.internal_info_builder import InternalInfoBuilder
    from src.database.services.entity_graph_service import EntityGraphService

logger = get_logger(__name__)


class ChatSessionManager:
    """管理所有 ChatSession 实例，处理消息分发和会话生命周期.

    这个类负责创建、获取和管理聊天会话的生命周期，包括消息的分发和处理。

    Attributes:
        config (FocusChatModeSettings): 专注聊天模式的配置设置。
        llm_client (LLMProcessorClient): LLM 处理器客户端，用于与 LLM 交互。
        event_storage (EventStorageService): 事件存储服务，用于存储和检索事件。
        action_handler (ActionHandler): 动作处理器，用于处理会话中的动作。
        bot_id (str): 祂的唯一标识符，用于识别和处理消息。
        conversation_service (ConversationStorageService): 会话存储服务，用于管理会话数据.
        summarization_service (SummarizationService): 摘要服务，用于生成会话摘要.
        summary_storage_service (SummaryStorageService): 摘要存储服务，用于存储和检索摘要数据.
        intelligent_interrupter (IntelligentInterrupter): 智能中断系统，用于处理
            会话中的智能中断逻辑.
        thought_storage_service (ThoughtStorageService): 思考存储服务，用于存储和检索思考数据.
        core_logic (Optional[CoreLogicFlow]): 核心逻辑流实例，用于处理会话的核心逻辑和决策.
        sessions (dict[str, ChatSession]): 存储所有活动聊天会话的字典，键为会话ID，
            值为 ChatSession 实例.
        lock (asyncio.Lock): 异步锁，用于确保对会话字典的线程安全访问.
        focus_session_inactive_event (Optional[asyncio.Event]): 用于唤醒主意识的
            事件对象，当所有专注会话结束时触发.
    """

    def __init__(
        self,
        config: FocusChatModeSettings,
        llm_client: LLMProcessorClient,
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        self_bot_ids_map: dict[str, str],
        conversation_service: ConversationStorageService,
        summarization_service: "SummarizationService",
        summary_storage_service: "SummaryStorageService",
        intelligent_interrupter: "IntelligentInterrupter",
        entity_graph_service: "EntityGraphService",
        thought_storage_service: "ThoughtStorageService",
        internal_info_builder: "InternalInfoBuilder",
        core_logic: Optional["CoreLogicFlow"] = None,
    ) -> None:
        self.config = config
        self.llm_client = llm_client
        self.event_storage = event_storage
        self.action_handler = action_handler
        self.self_bot_ids_map = self_bot_ids_map

        self.conversation_service = conversation_service
        self.summarization_service = summarization_service
        self.summary_storage_service = summary_storage_service
        self.thought_storage_service = thought_storage_service
        self.internal_info_builder = internal_info_builder
        self.entity_graph_service = entity_graph_service

        self.intelligent_interrupter = intelligent_interrupter

        self.core_logic = core_logic

        self.sessions: dict[str, ChatSession] = {}
        self.lock = asyncio.Lock()
        initial_focus_entry = {
            "timestamp": int(time.time() * 1000),
            "command": "initialize",
            "motivation": "AI意识苏醒，默认处于核心思考状态。",
            "target_path": "core",
        }
        self.focus_history = deque([initial_focus_entry], maxlen=10)
        self._last_switch_description: str = "你刚刚从发呆的状态中回过神来"

        # 创建一个指令到处理函数的“映射表”
        self._command_handlers = {
            "push_focus": self._handle_push_focus,
            "pop_focus": self._handle_pop_focus,
            "back": self._handle_back,
            "swap_focus": self._handle_swap_focus,
            "teleport_focus": self._handle_teleport_focus,
            "jump_to_history": self._handle_jump_to_history,
        }

        logger.info("ChatSessionManager 初始化完成。")

    @property
    def current_focus_path(self) -> dict[str, Any] | None:
        """属性：返回当前焦点路径（堆栈顶部）."""
        return self.focus_history[-1]

    @property
    def previous_focus_path(self) -> dict[str, Any] | None:
        """属性：返回上一个焦点路径（堆栈次顶部）."""
        return self.focus_history[-2] if len(self.focus_history) > 1 else None

    def _get_conversation_id(self, event: Event) -> str:
        # 从 Event 中提取唯一的会话ID (例如 group_id 或 user_id)
        # 此处需要根据 aicarus_protocols 的具体定义来实现
        info = event.conversation_info
        return info.conversation_id if info else "default_conv"

    async def get_or_create_session(
        self,
        conversation_id: str,
    ) -> ChatSession | None:  # 返回值可能为 None
        """获取或创建一个聊天会话实例.

        如果会话已存在，则返回现有实例；如果不存在，则创建一个新的会话实例。

        Args:
            conversation_id (str): 会话的唯一标识符。
            platform (str | None): 消息来源的平台（如 QQ、Telegram 等）。
            conversation_type (str | None): 会话类型（如 group、private 等）。
        Returns:
            ChatSession: 对应的聊天会话实例。
        """
        async with self.lock:
            if conversation_id in self.sessions:
                return self.sessions[conversation_id]

            logger.info(f"[SessionManager] 为 '{conversation_id}' 创建新的会话实例。")

            # 从数据库获取完整的会话档案
            conv_doc = await self.conversation_service.get_conversation_document_by_id(
                conversation_id
            )
            if not conv_doc:
                logger.error(
                    f"严重错误：尝试为 '{conversation_id}' 创建会话，但在数据库中找不到其档案！"
                )
                return None  # 创建失败

            from src.database.models import EnrichedConversationInfo

            conversation_info_obj = EnrichedConversationInfo.from_db_document(conv_doc)

            if not self.core_logic:
                raise RuntimeError("CoreLogic未注入，ChatSessionManager无法创建会话。")

            bot_id_for_session = self.self_bot_ids_map.get(conversation_info_obj.platform)
            if not bot_id_for_session:
                raise RuntimeError(
                    f"无法为平台 '{conversation_info_obj.platform}' "
                    f"创建会话，ID地图中找不到对应ID。"
                )

            self.sessions[conversation_id] = ChatSession(
                conversation_info=conversation_info_obj,
                conversation_id=conversation_id,
                llm_client=self.llm_client,
                event_storage=self.event_storage,
                action_handler=self.action_handler,
                bot_id=bot_id_for_session,
                core_logic=self.core_logic,
                chat_session_manager=self,
                conversation_service=self.conversation_service,
                summarization_service=self.summarization_service,
                summary_storage_service=self.summary_storage_service,
                intelligent_interrupter=self.intelligent_interrupter,
                thought_storage_service=self.thought_storage_service,
                internal_info_builder=self.internal_info_builder,
                initial_last_processed_timestamp=conversation_info_obj.last_processed_timestamp,
                entity_graph_service=self.entity_graph_service,
            )

            return self.sessions[conversation_id]

    async def deactivate_session(
        self, conversation_id: str, handover_context: dict | None = None
    ) -> None:
        """处理会话停用。现在它负责触发最终总结并从管理器中移除会话档案."""
        async with self.lock:
            if session := self.sessions.pop(conversation_id, None):
                logger.info(f"[SessionManager] 会话 '{conversation_id}' 的档案正在被移除。")

                # 在移除会话前，将会话内存中的“最后已读时间戳”持久化到数据库。
                # 我们真正需要记录的是“AI离开这个会话的时刻”。
                # 直接使用当前时间作为最终的处理时间戳。
                # 这确保了任何在此之前发生的消息都被视为“已读”或“已知晓”。
                final_timestamp = time.time() * 1000.0
                if final_timestamp > 0:
                    logger.info(
                        f"[{conversation_id}] 正在将会话的最终处理时间戳 "
                        f"({final_timestamp}) 保存到数据库..."
                    )
                    await self.conversation_service.update_conversation_processed_timestamp(
                        conversation_id, int(final_timestamp)
                    )

                shift_motivation = handover_context.get("motivation") if handover_context else None
                target_id = handover_context.get("target_id") if handover_context else None
                await session.summarization_manager.create_and_save_final_summary(
                    shift_motivation=shift_motivation, target_conversation_id=target_id
                )
                logger.info(
                    f"[SessionManager] 会话 '{conversation_id}' 的最终总结已处理，档案已移除。"
                )

    async def shutdown(self) -> None:
        """关闭所有活动的聊天会话."""
        logger.info("[SessionManager] 正在开始关闭所有活动会话...")
        active_sessions: list[ChatSession]
        async with self.lock:
            # 创建一个当前活动会话的副本进行操作，避免在迭代时修改字典
            active_sessions = list(self.sessions.values())

        if not active_sessions:
            logger.info("[SessionManager] 没有活动的会话需要关闭。")
            return

        shutdown_tasks = [session.shutdown() for session in active_sessions]
        results = await asyncio.gather(*shutdown_tasks, return_exceptions=True)

        for session, result in zip(active_sessions, results, strict=False):
            if isinstance(result, Exception):
                logger.error(
                    f"[SessionManager] 关闭会话 '{session.conversation_id}' 时发生错误: {result}",
                    exc_info=result,
                )
            else:
                logger.info(f"[SessionManager] 会话 '{session.conversation_id}' 已成功关闭。")

        logger.info("[SessionManager] 所有活动会话的关闭流程已完成。")

    async def _get_focus_description(self, focus_path_or_entry: str | dict | None) -> str:
        """根据 focus_path 或历史条目 生成一个详细的、人类可读的位置描述."""
        # --- 步骤 1: 预处理，从输入中提取出纯粹的路径字符串 ---
        focus_path: str | None = None
        if isinstance(focus_path_or_entry, dict):
            # 如果输入是我们的历史条目字典，就从中提取 'target_path'
            focus_path = focus_path_or_entry.get("target_path")
        elif isinstance(focus_path_or_entry, str):
            # 如果输入直接就是字符串，就直接使用
            focus_path = focus_path_or_entry
        # 如果输入是 None 或者其他类型，focus_path 将保持为 None

        # --- 步骤 2: 执行原有的描述生成逻辑 (这部分逻辑完全不用变) ---
        if focus_path is None or focus_path == "core":
            return "正在发呆/自我思考"

        path_parts = focus_path.split(".")

        # 平台层
        if len(path_parts) == 1:
            return f"平台'{path_parts[0]}'"

        # 会话层 (cellular)
        if len(path_parts) >= 2:
            platform_id = path_parts[0]
            # 修复：会话ID可能是由多个部分组成的，例如 "private.123456"
            conv_id = ".".join(path_parts[1:])

            # 尝试从内存中的 session 获取信息
            session = self.sessions.get(conv_id)
            if session and session.conversation_name:  # 优先使用内存中更新的会话名
                conv_type_str = "群会话" if session.conversation_type == "group" else "私聊会话"
                return f"{conv_type_str}'{session.conversation_name}'(ID: {conv_id})"

            # 如果 session 不在内存或没有名字，从数据库查
            conv_doc = await self.conversation_service.get_conversation_document_by_id(conv_id)
            if conv_doc:
                conv_type = conv_doc.get("type", "unknown")
                conv_name = conv_doc.get("name", conv_id)
                conv_type_str = "群会话" if conv_type == "group" else "私聊会话"
                return f"{conv_type_str}'{conv_name}'(ID: {conv_id})"

            # 如果都找不到，提供一个保底描述
            logger.warning(
                f"无法获取会话 '{conv_id}' 的详细信息，可能是因为它不在内存中且数据库中也不存在。"
            )
            return f"一个位于平台'{platform_id}'下的未知会话(ID: {conv_id})"

        return "一个未知的地方"

    def get_last_switch_description(self) -> str:
        """获取上次焦点切换的格式化描述."""
        return self._last_switch_description

    async def handle_consciousness_control(self, control_json: dict) -> None:
        """处理来自LLM决策的意识控制指令.

        是一个干净利落的“总调度中心”.

        Args:
            control_json (dict): 包含意识控制指令的 JSON 对象.
        """
        if not (command := next(iter(control_json), None)) or not (
            params := control_json.get(command)
        ):
            logger.warning(f"收到的意识控制指令格式不正确或为空: {control_json}")
            return

        logger.info(f"焦点管理器(v2.1)收到指令: {command}, 参数: {params}")

        # 从“映射表”里找到对应的处理函数
        handler = self._command_handlers.get(command)
        if not handler:
            logger.error(f"收到未知的意识控制指令: '{command}'，无法处理。")
            return

        previous_path_for_desc = self.current_focus_path
        history_entry_base = {
            "timestamp": int(time.time() * 1000),
            "command": command,
            "motivation": params.get("motivation", "没有明确动机"),
        }

        # 把具体的工作交给专业的辅助函数去做，自己只关心结果
        focus_switched = await handler(params, history_entry_base)

        # 如果报告说“搞定了”，就统一处理后续事宜
        if focus_switched:
            from_desc = await self._get_focus_description(previous_path_for_desc)
            to_desc = await self._get_focus_description(self.current_focus_path)
            self._last_switch_description = f"你刚刚从“{from_desc}”来到了“{to_desc}”"

            logger.info(
                f"AI 决定 [{command}]，{self._last_switch_description} "
                f"(动机: {history_entry_base['motivation']})"
            )

            if self.core_logic and hasattr(self.core_logic, "prompt_builder"):
                self.core_logic.prompt_builder.is_context_switch_flag = True
                self.core_logic.trigger_immediate_thought_cycle()

    # 下面是所有被拆分出来的“专业处理函数”

    async def _handle_push_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'push_focus' 指令."""
        if not (target_id := params.get("target_id")):
            logger.error("'push_focus' 指令缺少 'target_id'。")
            return False

        current_entry = self.current_focus_path
        current_path_str = (
            current_entry.get("target_path") if isinstance(current_entry, dict) else current_entry
        )
        new_path = target_id if current_path_str == "core" else f"{current_path_str}.{target_id}"

        entry_to_push = {**history_entry_base, "target_path": new_path}
        self.focus_history.append(entry_to_push)
        logger.info(f"[堆栈 PUSH] 焦点下潜至: {new_path}")

        if "." in new_path:
            conv_id = ".".join(new_path.split(".")[1:])
            if not await self.get_or_create_session(conversation_id=conv_id):
                logger.error(f"无法 'push_focus'，数据库中找不到会话 '{conv_id}' 的档案。")
                self.focus_history.pop()  # 回滚
                return False
        return True

    async def _handle_pop_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'pop_focus' 指令.

        这会从 focus_history 堆栈的顶部移除当前焦点.
        """
        if len(self.focus_history) <= 1:
            logger.warning("在顶层Core-Level尝试执行 'pop_focus'，无效操作，已忽略。")
            return False

        # 这是【空间】上浮的核心：直接 pop 掉最后一个元素
        leaving_entry = self.focus_history.pop()
        leaving_path = (
            leaving_entry.get("target_path") if isinstance(leaving_entry, dict) else leaving_entry
        )
        logger.info(f"[堆栈 POP - 空间] 焦点从 '{leaving_path}' 上浮。")

        if leaving_path and "." in leaving_path:
            conv_id_to_deactivate = ".".join(leaving_path.split(".")[1:])
            await self.deactivate_session(
                conv_id_to_deactivate, {"motivation": history_entry_base["motivation"]}
            )

        # 检查上浮后的新焦点是否需要激活
        new_focus_entry = self.current_focus_path
        new_path_str = (
            new_focus_entry.get("target_path")
            if isinstance(new_focus_entry, dict)
            else new_focus_entry
        )
        if new_path_str and "." in new_path_str:
            conv_id = ".".join(new_path_str.split(".")[1:])
            await self.get_or_create_session(conversation_id=conv_id)
        return True

    async def _handle_back(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'back' 指令。【时间】回溯.

        这会回到 T-1 的焦点，本质上是 jump_to_history(-1).
        """
        if len(self.focus_history) <= 1:
            logger.warning("历史记录不足，无法执行 'back' 操作。")
            return False

        # 这是回溯的核心：我们先看看 T-1 是谁，然后再决定怎么做
        # 为了代码复用和逻辑一致性，我们直接调用 jump 的逻辑！
        # jump_to_history 的 history_index T-1 对应的是 -1
        logger.info("检测到 'back' 指令，将其作为 'jump_to_history' (index=-1) 处理。")
        return await self._handle_jump_to_history(
            {"history_index": -1, "motivation": history_entry_base["motivation"]},
            history_entry_base,
        )

    async def _handle_swap_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'swap_focus' 指令."""
        current_entry = self.current_focus_path
        current_path_str = (
            current_entry.get("target_path") if isinstance(current_entry, dict) else current_entry
        )

        if (
            not (target_id := params.get("target_id"))
            or not current_path_str
            or "." not in current_path_str
        ):
            logger.error("'swap_focus' 指令无效：缺少目标ID或当前不在底层会话中。")
            return False

        path_parts = current_path_str.split(".")
        parent_path = ".".join(path_parts[:-1])
        new_path = f"{parent_path}.{target_id}"

        self.focus_history.pop()
        conv_id_to_deactivate = path_parts[-1]
        await self.deactivate_session(
            conv_id_to_deactivate,
            {"motivation": history_entry_base["motivation"], "target_id": target_id},
        )

        if await self.get_or_create_session(conversation_id=target_id):
            entry_to_push = {**history_entry_base, "target_path": new_path}
            self.focus_history.append(entry_to_push)
            logger.info(f"[堆栈 SWAP] 焦点切换至: {new_path}")
        else:
            logger.error(
                f"无法 'swap_focus'，数据库中找不到目标会话 '{target_id}'。切换中止，停留在平台层。"
            )

        return True  # 无论是否成功找到新会话，焦点都已经切换了（至少是上浮了）

    async def _handle_teleport_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'teleport_focus' 指令."""
        if not (target_path := params.get("target_path")):
            logger.error("'teleport_focus' 指令缺少 'target_path'。")
            return False

        current_entry = self.current_focus_path
        current_path_str = (
            current_entry.get("target_path") if isinstance(current_entry, dict) else current_entry
        )
        if current_path_str and "." in current_path_str:
            conv_id_to_deactivate = ".".join(current_path_str.split(".")[1:])
            await self.deactivate_session(
                conv_id_to_deactivate, {"motivation": f"传送到 {target_path}"}
            )

        self.focus_history.clear()
        self.focus_history.append({"target_path": "core"})
        entry_to_push = {**history_entry_base, "target_path": target_path}
        self.focus_history.append(entry_to_push)
        logger.info(f"[堆栈 TELEPORT] 焦点已传送至: {target_path}")

        path_parts = target_path.split(".")
        if len(path_parts) >= 2:
            conv_id = ".".join(path_parts[1:])
            if not await self.get_or_create_session(conversation_id=conv_id):
                logger.warning(
                    f"传送目标 '{target_path}' 无法在数据库中找到对应会话，可能无法正常交互。"
                )
        return True

    async def _handle_jump_to_history(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'jump_to_history' 指令."""
        if (history_index := params.get("history_index")) is None:
            logger.error("'jump_to_history' 指令缺少 'history_index'。")
            return False

        try:
            history_len = len(self.focus_history)
            target_deque_index = -1 + history_index
            if not (-history_len <= target_deque_index < 0):
                logger.error(
                    f"历史索引 {history_index} 超出范围 (当前历史深度: {history_len - 1})。"
                )
                return False

            target_path = self.focus_history[target_deque_index].get("target_path")
            current_entry = self.current_focus_path
            current_path_str = (
                current_entry.get("target_path")
                if isinstance(current_entry, dict)
                else current_entry
            )
            if current_path_str and "." in current_path_str:
                conv_id_to_deactivate = ".".join(current_path_str.split(".")[1:])
                await self.deactivate_session(
                    conv_id_to_deactivate, {"motivation": f"跳跃到历史焦点 {target_path}"}
                )

            for _ in range(abs(target_deque_index) - 1):
                self.focus_history.pop()
            logger.info(f"[堆栈 JUMP] 焦点已跳跃至历史记录: {target_path}")

            if target_path and "." in target_path:
                conv_id = ".".join(target_path.split(".")[1:])
                await self.get_or_create_session(conversation_id=conv_id)
            return True
        except (IndexError, TypeError) as e:
            logger.error(f"处理 'jump_to_history' 时发生错误，索引: {history_index}, 错误: {e}")
            return False
