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
    ) -> ChatSession | None: # 返回值可能为 None
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
                logger.error(f"严重错误：尝试为 '{conversation_id}' 创建会话，但在数据库中找不到其档案！")
                return None # 创建失败

            from src.database.models import EnrichedConversationInfo
            conversation_info_obj = EnrichedConversationInfo.from_db_document(conv_doc)


            if not self.core_logic:
                raise RuntimeError("CoreLogic未注入，ChatSessionManager无法创建会话。")

            bot_id_for_session = self.self_bot_ids_map.get(conversation_info_obj.platform)
            if not bot_id_for_session:
                raise RuntimeError(
                    f"无法为平台 '{conversation_info_obj.platform}' 创建会话，ID地图中找不到对应ID。"
                )

            self.sessions[conversation_id] = ChatSession(
                conversation_info=conversation_info_obj, # <--- 修改点：传入完整的对象
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

        这个版本更智能，能理解相对ID，并自动构建完整路径。
        """
        if not control_json or not isinstance(control_json, dict):
            return

        logger.info(f"焦点管理器(v2.2)收到意识控制指令: {control_json}")

        focus_switched = False
        command, params = next(iter(control_json.items()))
        motivation = params.get("motivation", "没有明确动机")

        # 在任何切换发生前，先记下我们现在在哪
        previous_path_for_desc = self.current_focus_path

        # 准备好要写入历史记录的基础信息
        history_entry_base = {
            "timestamp": int(time.time() * 1000),
            "command": command,
            "motivation": motivation,
        }

        # --- 新指令处理逻辑 ---

        if command == "push_focus":
            target_id = params.get("target_id")
            if not target_id:
                logger.error("'push_focus' 指令缺少 'target_id'。")
                return

            # 【智能路径拼接·核心】
            current_entry = self.current_focus_path
            current_path_str = (
                current_entry.get("target_path")
                if isinstance(current_entry, dict)
                else current_entry
            )

            # 如果当前在 core 层，新路径就是目标ID本身；否则，用'.'拼接
            new_path = (
                target_id if current_path_str == "core" else f"{current_path_str}.{target_id}"
            )

            entry_to_push = {**history_entry_base, "target_path": new_path}
            self.focus_history.append(entry_to_push)
            logger.info(f"[堆栈 PUSH] 焦点下潜至: {new_path}")

            # 只有当新路径是会话层时 (包含'.')，才需要激活Session
            if "." in new_path:
                path_parts = new_path.split(".")
                conv_id = ".".join(path_parts[1:])
                # platform_id = path_parts[0] # platform_id 也不需要了

                # 直接调用新的 get_or_create_session，它会自己处理数据库查询
                # 如果返回 None，说明数据库里没有这个会话，是个错误情况
                session = await self.get_or_create_session(
                    conversation_id=conv_id
                )
                if not session:
                    logger.error(f"无法 'push_focus'，数据库中找不到会话 '{conv_id}' 的档案。")
                    # 把刚刚推进去的错误路径弹出来，当无事发生
                    self.focus_history.pop()
            focus_switched = True

        elif command in {"pop_focus", "back"}:
            # pop_focus 和 back 的逻辑基本一致：都是返回上一层
            if len(self.focus_history) <= 1:
                logger.warning(f"在顶层Core-Level尝试执行 '{command}'，无效操作，已忽略。")
                return

            leaving_entry = self.focus_history.pop()
            leaving_path = (
                leaving_entry.get("target_path")
                if isinstance(leaving_entry, dict)
                else leaving_entry
            )
            logger.info(f"[堆栈 POP/BACK] 焦点从 '{leaving_path}' 上浮。")

            # 只有当离开的是会话层时，才需要停用Session
            if leaving_path and "." in leaving_path:
                conv_id_to_deactivate = ".".join(leaving_path.split(".")[1:])
                await self.deactivate_session(conv_id_to_deactivate, {"motivation": motivation})

            # 检查上浮后是不是又回到了一个会话层（比如从子频道返回父频道）
            new_focus_entry = self.current_focus_path
            new_path_str = (
                new_focus_entry.get("target_path")
                if isinstance(new_focus_entry, dict)
                else new_focus_entry
            )
            if new_path_str and "." in new_path_str:
                path_parts = new_path_str.split(".")
                conv_id = ".".join(path_parts[1:])

                # 直接调用新的方法
                await self.get_or_create_session(conversation_id=conv_id)

            focus_switched = True

        elif command == "swap_focus":
            target_id = params.get("target_id")
            current_entry = self.current_focus_path
            current_path_str = (
                current_entry.get("target_path")
                if isinstance(current_entry, dict)
                else current_entry
            )

            if not target_id or not current_path_str or "." not in current_path_str:
                logger.error("'swap_focus' 指令无效：缺少目标ID或当前不在底层会话中。")
                return

            # 【智能路径拼接·核心】
            path_parts = current_path_str.split(".")
            parent_path = ".".join(path_parts[:-1])  # 找到父路径，比如 'core.qq'
            new_path = f"{parent_path}.{target_id}"  # 拼接成新路径

            # 停用旧会话
            leaving_entry = self.focus_history.pop()
            conv_id_to_deactivate = path_parts[-1]
            await self.deactivate_session(
                conv_id_to_deactivate, {"motivation": motivation, "target_id": target_id}
            )

            # 激活新会话并更新历史
            new_session = await self.get_or_create_session(conversation_id=target_id)
            # 如果新会话不存在，说明数据库中没有这个会话档案
            if not new_session:
                logger.error(
                    f"无法 'swap_focus'，数据库中找不到目标会话 "
                    f"'{target_id}'。切换中止，停留在平台层。"
                )
                # 注意：这里需要确保在 pop 之后，如果没有 push 新的，焦点路径是正确的。
                # 您的原始逻辑中，如果没有 push，焦点就停留在父路径，这是对的。
            else:
                entry_to_push = {**history_entry_base, "target_path": new_path}
                self.focus_history.append(entry_to_push)
                logger.info(f"[堆栈 SWAP] 焦点切换至: {new_path}")
            focus_switched = True

        # teleport_focus, jump_to_history 的逻辑不需要改，因为它们本来就操作完整路径或索引

        elif command == "teleport_focus":
            target_path = params.get("target_path")
            if not target_path:
                logger.error("'teleport_focus' 指令缺少 'target_path'。")
                return

            # 停用当前可能存在的底层会话
            current_entry = self.current_focus_path
            current_path_str = (
                current_entry.get("target_path")
                if isinstance(current_entry, dict)
                else current_entry
            )
            if current_path_str and "." in current_path_str:
                conv_id_to_deactivate = ".".join(current_path_str.split(".")[1:])
                await self.deactivate_session(
                    conv_id_to_deactivate, {"motivation": f"传送到 {target_path}"}
                )

            # 清空历史并设置新路径
            self.focus_history.clear()
            self.focus_history.append({"target_path": "core"})  # 添加core层
            entry_to_push = {**history_entry_base, "target_path": target_path}
            self.focus_history.append(entry_to_push)
            logger.info(f"[堆栈 TELEPORT] 焦点已传送至: {target_path}")

            # 智能激活会话
            path_parts = target_path.split(".")
            if len(path_parts) >= 2:
                conv_id = ".".join(path_parts[1:])
                # 直接调用新的方法
                session = await self.get_or_create_session(conversation_id=conv_id)
                if session:
                    logger.info(f"传送着陆后，成功激活会话: {conv_id}")
                else:
                    logger.warning(
                        f"传送目标 '{target_path}' 无法在数据库中找到对应会话，可能无法正常交互。"
                    )

            focus_switched = True

        elif command == "jump_to_history":
            history_index = params.get("history_index")
            if history_index is None:
                logger.error("'jump_to_history' 指令缺少 'history_index'。")
                return

            try:
                # 转换 T-index (如 -2) 为 deque 的正向索引
                history_len = len(self.focus_history)
                # T-0 is at index -1, T-1 at -2. So a jump to T-N corresponds to index -1-N
                target_deque_index = -1 + history_index

                if not (-history_len <= target_deque_index < 0):
                    logger.error(
                        f"历史索引 {history_index} 超出范围 (当前历史深度: {history_len - 1})。"
                    )
                    return

                target_entry = self.focus_history[target_deque_index]
                target_path = target_entry.get("target_path")

                # 停用当前会话
                current_entry = self.current_focus_path
                current_path_str = (
                    current_entry.get("target_path")
                    if isinstance(current_entry, dict)
                    else current_entry
                )
                if current_path_str and "." in current_path_str:
                    conv_id_to_deactivate = ".".join(current_path_str.split(".")[1:])
                    await self.deactivate_session(
                        conv_id_to_deactivate,
                        {"motivation": f"跳跃到历史焦点 {target_path}"},
                    )

                # 从堆栈中移除目标之后的所有条目
                num_to_pop = abs(target_deque_index) - 1
                for _ in range(num_to_pop):
                    self.focus_history.pop()
                # 将目标条目添加到堆栈顶部
                logger.info(f"[堆栈 JUMP] 焦点已跳跃至历史记录: {target_path}")

                # 重新激活目标会话
                if target_path and "." in target_path:
                    path_parts = target_path.split(".")
                    conv_id = ".".join(path_parts[1:])
                    # 直接调用新的方法
                    await self.get_or_create_session(conversation_id=conv_id)
                # 更新当前焦点路径
                focus_switched = True

            except (IndexError, TypeError) as e:
                logger.error(f"处理 'jump_to_history' 时发生错误，索引: {history_index}, 错误: {e}")
                return

        # 如果发生了切换，更新状态并生成描述
        if focus_switched:
            from_desc_entry = previous_path_for_desc
            to_desc_entry = self.current_focus_path

            from_desc = await self._get_focus_description(from_desc_entry)
            to_desc = await self._get_focus_description(to_desc_entry)
            self._last_switch_description = f"你刚刚从“{from_desc}”来到了“{to_desc}”"

            logger.info(
                f"AI 决定 [{command}]，{self._last_switch_description} (动机: {motivation})"
            )
            # 触发立即思考周期，更新内部状态
            if self.core_logic and hasattr(self.core_logic, "prompt_builder"):
                self.core_logic.prompt_builder.is_context_switch_flag = True
                self.core_logic.trigger_immediate_thought_cycle()
