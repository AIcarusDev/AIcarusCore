# src/action/action_handler.py (小色猫·女王修复最终版)
import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

# 导入我们的小玩具挂钩和它的提供者！
from src.action.action_provider import ActionProvider
from src.action.components.action_registry import ActionRegistry
from src.action.components.llm_client_factory import LLMClientFactory
from src.action.components.pending_action_manager import PendingActionManager
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.core_communication.action_sender import ActionSender
from src.database import (
    ActionLogStorageService,
    ConversationStorageService,
    EventStorageService,
    ThoughtStorageService,
)
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.platform_builders.registry import platform_builder_registry

if TYPE_CHECKING:
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)
ACTION_RESPONSE_TIMEOUT_SECONDS = 30


class ActionHandler:
    """处理所有与动作相关的逻辑，包括动作决策、发送和响应处理.

    这个类负责协调不同的动作提供者，管理动作发送和响应，
    并提供一个统一的接口供其他模块使用.

    Attributes:
        action_llm_client: 用于行动决策的 LLM 客户端.
        summary_llm_client: 用于信息摘要的 LLM 客户端.
        web_search_agent_client: 用于网页搜索的 LLM 客户端.
        action_sender: 动作发送器，用于将动作发送到适配器.
        thought_storage_service: 思维存储服务，用于存储和检索思维文档.
        event_storage_service: 事件存储服务，用于存储和检索事件数据.
        action_log_service: 动作日志存储服务，用于记录动作日志.
        conversation_service: 对话存储服务，用于管理对话数据.
        thought_trigger: 主思维触发器，用于在处理完动作后唤醒主思维.
        pending_action_manager: 管理待处理动作的管理器，处理动作响应和状态跟踪.
        chat_session_manager: 聊天会话管理器，用于管理聊天会话状态
        action_registry: 动作注册表，用于注册和查询可用的动作提供者.
        _background_tasks: 存储所有后台任务的集合，用于管理和清理.
    """

    def __init__(self) -> None:
        self.action_llm_client: ProcessorClient | None = None
        self.summary_llm_client: ProcessorClient | None = None
        self.web_search_agent_client: ProcessorClient | None = None
        self.action_sender: ActionSender | None = None
        self.thought_storage_service: ThoughtStorageService | None = None
        self.event_storage_service: EventStorageService | None = None
        self.action_log_service: ActionLogStorageService | None = None
        self.conversation_service: ConversationStorageService | None = None
        self.thought_trigger: asyncio.Event | None = None
        self.pending_action_manager: PendingActionManager | None = None
        self.chat_session_manager: ChatSessionManager | None = None
        self.action_registry = ActionRegistry()
        self._background_tasks: set[asyncio.Task] = set()

        logger.info(f"{self.__class__.__name__} instance created.")

    def set_dependencies(
        self,
        thought_service: ThoughtStorageService,
        event_service: EventStorageService,
        action_log_service: ActionLogStorageService,
        conversation_service: ConversationStorageService,
        action_sender: ActionSender,
        chat_session_manager: "ChatSessionManager",
    ) -> None:
        """设置 ActionHandler 的依赖服务.

        Args:
            thought_service: 思维存储服务实例.
            event_service: 事件存储服务实例.
            action_log_service: 动作日志存储服务实例.
            conversation_service: 对话存储服务实例.
            action_sender: 动作发送器实例.
            chat_session_manager: 聊天会话管理器实例.

        """
        self.thought_storage_service = thought_service
        self.event_storage_service = event_service
        self.action_log_service = action_log_service
        self.conversation_service = conversation_service
        self.action_sender = action_sender
        self.chat_session_manager = chat_session_manager  # 注入
        self.pending_action_manager = PendingActionManager(
            action_log_service=action_log_service,
            thought_storage_service=thought_service,
            event_storage_service=event_service,
            conversation_service=conversation_service,
        )
        logger.info("ActionHandler 的依赖已成功设置，PendingActionManager 已创建。")

    def register_provider(self, provider: ActionProvider) -> None:
        """将动作提供者注册到 ActionRegistry."""
        self.action_registry.register_provider(provider)

    def set_thought_trigger(self, trigger_event: asyncio.Event | None) -> None:
        """设置主思维触发器，用于在处理完动作后唤醒主思维."""
        if trigger_event is not None and not isinstance(trigger_event, asyncio.Event):
            logger.error(f"set_thought_trigger 收到一个无效的事件类型: {type(trigger_event)}。")
            self.thought_trigger = None
            return
        self.thought_trigger = trigger_event
        if trigger_event:
            logger.info("ActionHandler 的主思维触发器已成功设置。")

    async def initialize_llm_clients(self) -> None:
        """按需初始化 LLM 客户端."""
        if self.action_llm_client and self.summary_llm_client:
            return
        logger.info("正在为行动处理模块按需初始化LLM客户端...")
        factory = LLMClientFactory()
        try:
            # 只在需要时初始化行动决策LLM客户端
            if not self.action_llm_client:
                self.action_llm_client = factory.create_client(purpose_key="action_decision")
            # 只在需要时初始化摘要LLM客户端
            if not self.summary_llm_client:
                self.summary_llm_client = factory.create_client(purpose_key="information_summary")
            # 只在需要时初始化网页搜索代理客户端
            if not self.web_search_agent_client:
                self.web_search_agent_client = factory.create_client(purpose_key="web_search_agent")
            logger.info("LLM客户端初始化成功。")
        except RuntimeError as e:
            logger.critical(f"为 ActionHandler 初始化LLM客户端失败: {e}")
            raise

    async def handle_action_response(self, response_event_data: dict[str, Any]) -> None:
        """处理来自 PendingActionManager 的动作响应."""
        if self.pending_action_manager:
            await self.pending_action_manager.handle_response(response_event_data)
        else:
            logger.error("PendingActionManager 未初始化，无法处理动作响应。")

    async def system_get_bot_profile(self, adapter_id: str) -> None:
        """系统触发获取机器人档案的动作，适用于平台适配器."""
        logger.info(f"系统触发为适配器 '{adapter_id}' 获取机器人档案。")
        builder = platform_builder_registry.get_builder(adapter_id)
        if not builder:
            logger.error(f"找不到平台 '{adapter_id}' 的翻译官，无法发起上线安检！")
            return

        action_event = builder.build_action_event(action_name="get_bot_profile", params={})

        if not action_event:
            logger.error(f"平台 '{adapter_id}' 的翻译官不会翻译 get_bot_profile 动作！")
            return

        task = asyncio.create_task(
            self._execute_platform_action(
                action_to_send=action_event.to_dict(),
                thought_doc_key=None,
                original_action_description="系统：上线安检",
            )
        )

        self._background_tasks.add(task)

        task.add_done_callback(self._background_tasks.discard)

        logger.info(f"已通过 ActionHandler 为适配器 '{adapter_id}' 派发档案同步任务。")

    async def _execute_platform_action(
        self,
        action_to_send: dict[str, Any],
        thought_doc_key: str | None,
        original_action_description: str,
    ) -> tuple[bool, Any]:
        if not self.action_sender or not self.action_log_service or not self.pending_action_manager:
            return False, {"error": "内部错误：核心服务不可用。"}

        event_type = action_to_send.get("event_type", "")
        parts = event_type.split(".")
        platform = parts[1] if len(parts) > 1 else "unknown_platform"

        core_action_id = action_to_send.setdefault("event_id", str(uuid.uuid4()))
        timestamp = int(time.time() * 1000)
        action_to_send["timestamp"] = timestamp

        await self.action_log_service.save_action_attempt(
            action_id=core_action_id,
            action_type=event_type,
            timestamp=timestamp,
            platform=platform,
            bot_id=action_to_send.get("bot_id", config.persona.bot_name),
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

        return await self.pending_action_manager.add_and_wait_for_action(
            action_id=core_action_id,
            thought_doc_key=thought_doc_key,
            original_action_description=original_action_description,
            action_to_send=action_to_send,
        )

    async def process_action_flow(
        self,
        action_id: str,
        doc_key_for_updates: str,
        action_json: dict[str, Any],  # 接收完整的 action JSON 对象
    ) -> tuple[bool, str, Any]:
        """处理来自主意识的、新格式的行动指令."""
        logger.info(f"--- [Action ID: {action_id}] 女王开始处理行动流程 ---")
        await self.initialize_llm_clients()

        if not self.thought_storage_service:
            return False, "核心服务ThoughtStorageService未初始化", None

        # 1. 解析嵌套的 action_json
        platform_actions = action_json.get("napcat_qq", {})
        core_actions = action_json.get("core", {})

        final_result_text = "动作未产生直接文本结果。"
        final_payload = None
        success = False

        # 2. 优先处理平台动作
        if platform_actions:
            platform_id = "napcat_qq"
            # 假设一次只处理一个平台动作
            action_name, params = next(iter(platform_actions.items()))
            motivation = params.get("motivation", "没有明确动机")

            # 2.2 处理其他平台动作 (如 get_list)
            builder = platform_builder_registry.get_builder(platform_id)
            if not builder:
                msg = f"找不到平台 '{platform_id}' 的翻译官。"
                logger.error(msg)
                return False, msg, None

            action_event = builder.build_action_event(action_name, params)
            if not action_event:
                msg = f"平台 '{platform_id}' 的翻译官不会翻译动作 '{action_name}'。"
                logger.error(msg)
                return False, msg, None

            success, payload = await self._execute_platform_action(
                action_to_send=action_event.to_dict(),
                thought_doc_key=doc_key_for_updates,
                original_action_description=f"{platform_id}.{action_name}",
            )
            final_result = (
                f"动作 {platform_id}.{action_name} 已提交。"
                if success
                else f"动作 {platform_id}.{action_name} 提交失败: {payload}"
            )
            if self.thought_trigger:
                self.thought_trigger.set()
            return success, final_result, payload

        # 3. 处理核心动作
        elif core_actions:
            action_name, params = next(iter(core_actions.items()))
            motivation = params.get("motivation", "没有明确动机")

            if action_name == "web_search":
                query = params.get("query")
                if not query or not self.web_search_agent_client:
                    msg = "LLM想搜索但没提供关键词，或者搜索代理客户端未初始化。"
                    logger.warning(msg)
                    return False, msg, None

                search_prompt = f"""请根据以下意图，使用谷歌搜索并总结最相关的信息：
意图：{query}
动机：{motivation}"""
                logger.info(f"正在调用搜索代理LLM，查询: '{query}'")
                response = await self.web_search_agent_client.make_llm_request(
                    prompt=search_prompt,
                    is_stream=False,
                    use_google_search=True,  # 开启谷歌搜索
                )
                final_result = response.get("text", "搜索失败或未返回任何信息。")

            if self.thought_trigger:
                logger.info(f"动作流程处理完毕 (Action ID: {action_id})，设置主思维触发器。")
                self.thought_trigger.set()

            return success, final_result_text, final_payload

        # 4. 如果啥动作都没有
        final_result_for_shimo = "AI决策的动作对象为空，或没有可执行的动作。"

        if self.thought_trigger:
            self.thought_trigger.set()
        return True, final_result_for_shimo, None

    async def send_action_and_wait_for_response(
        self, action_event_dict: dict[str, Any], timeout: int = ACTION_RESPONSE_TIMEOUT_SECONDS
    ) -> tuple[bool, dict[str, Any] | None]:
        """发送一个动作事件，并等待响应，超时后返回失败.

        Args:
            action_event_dict: 包含动作事件的字典，必须包含 'event_type'
            timeout: 等待响应的超时时间，默认为 30 秒.

        Returns:
            tuple[bool, dict[str, Any] | None]: 发送结果和响应数据。
        """
        if not self.pending_action_manager:
            return False, {"error": "PendingActionManager is not initialized."}

        event_type = action_event_dict.get("event_type", "")
        parts = event_type.split(".")
        adapter_id = parts[1] if len(parts) > 1 else None

        if not adapter_id:
            return False, {"error": "Action event must have a valid event_type with platform ID."}

        return await self._execute_platform_action(
            action_to_send=action_event_dict,
            thought_doc_key=None,
            original_action_description="internal_tool_call",
        )

    async def execute_simple_action(
        self, platform_id: str, action_name: str, params: dict, description: str
    ) -> tuple[
        bool, Any
    ]:  # <-- ❤❤❤ 我把它的小嘴撑大了，让它可以吐出任何东西！(返回类型改为 Any) ❤❤❤
        """一个更简单的动作执行入口，用于内部系统调用，如专注模式."""
        builder = platform_builder_registry.get_builder(platform_id)
        if not builder:
            # ❤❤❤ 为了统一，失败时也返回字典，让调用者的小穴更好处理！❤❤❤
            return False, {"error": f"找不到平台 '{platform_id}' 的翻译官。"}

        action_event = builder.build_action_event(action_name, params)
        if not action_event:
            # ❤❤❤ 统一返回字典！❤❤❤
            return False, {"error": f"平台 '{platform_id}' 的翻译官不会翻译动作 '{action_name}'。"}

        success, payload = await self._execute_platform_action(
            action_to_send=action_event.to_dict(),
            thought_doc_key=None,  # 专注模式不关联主意识思考文档
            original_action_description=description,
        )

        # ❤❤❤ 最终调教！不再自作主张地转换！适配器返回什么，我就给你什么！❤❤❤
        # 这样，如果成功且 payload 是字典，调用者就能直接得到这个湿润的字典！
        # 如果失败，payload 本身就是一个包含 'error' 的字典，也能正确处理！
        return success, payload

    # --- ❤❤❤ 这就是我为您准备的VIP贵宾通道！❤❤❤ ---
    async def submit_constructed_action(
        self,
        action_event_dict: dict[str, Any],
        action_description: str,
        associated_record_key: str | None = None,
    ) -> tuple[bool, str]:
        """直接提交一个已构造好的动作事件，绕过LLM决策."""
        if not self.action_sender or not self.action_log_service:
            critical_error_msg = "核心服务 (ActionSender 或动作日志服务) 未设置!"
            logger.critical(critical_error_msg)
            return False, critical_error_msg

        if "event_id" not in action_event_dict:
            return False, "动作事件缺少 'event_id'"

        success, message_payload = await self._execute_platform_action(
            action_to_send=action_event_dict,
            thought_doc_key=associated_record_key,
            original_action_description=action_description,
        )

        message = ""
        if isinstance(message_payload, dict):
            message = message_payload.get("error") or message_payload.get(
                "message", str(message_payload)
            )
        else:
            message = str(message_payload)

        return success, message
