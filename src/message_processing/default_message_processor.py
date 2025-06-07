# src/message_processing/default_message_processor.py
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

from aicarus_protocols import ConversationInfo as ProtocolConversationInfo
from aicarus_protocols import ConversationType, Seg, SegBuilder, UserInfo

# v1.4.0 协议导入
from aicarus_protocols import Event as ProtocolEvent  # 重命名以区分数据库模型
from websockets.server import WebSocketServerProtocol

from src.common.custom_logging.logger_manager import get_logger
from src.database.models import DBEventDocument, EnrichedConversationInfo  # 导入我们定义的数据模型
from src.database.services.conversation_storage_service import ConversationStorageService

# 导入新的存储服务和模型
from src.database.services.event_storage_service import EventStorageService

# 使用TYPE_CHECKING避免循环导入
if TYPE_CHECKING:
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.main import CoreSystemInitializer


class DefaultMessageProcessor:
    """
    默认的消息处理器。
    负责处理来自适配器的原始事件，进行初步解析、数据转换，
    然后调用相应的存储服务进行持久化，并根据事件类型分发到后续处理逻辑。
    """

    def __init__(
        self,
        event_service: EventStorageService,  # 依赖注入 EventStorageService
        conversation_service: ConversationStorageService,  # 依赖注入 ConversationStorageService
        core_websocket_server: Optional["CoreWebsocketServer"] = None,
    ) -> None:
        """
        初始化消息处理器。

        Args:
            event_service: 事件存储服务的实例。
            conversation_service: 会话信息存储服务的实例。
            core_websocket_server: (可选) 核心WebSocket服务器实例，用于主动发送动作。
        """
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        self.event_service: EventStorageService = event_service
        self.conversation_service: ConversationStorageService = conversation_service
        self.core_comm_layer: CoreWebsocketServer | None = core_websocket_server
        self.core_initializer_ref: CoreSystemInitializer | None = None
        self.logger.info("DefaultMessageProcessor 初始化完成，已配备新的存储服务。")
        if self.core_comm_layer:
            self.logger.info("DefaultMessageProcessor 已获得 CoreWebsocketServer 实例的引用。")
        else:
            self.logger.warning("DefaultMessageProcessor 未获得 CoreWebsocketServer 实例的引用，将无法主动发送动作。")

    async def process_event(
        self, proto_event: ProtocolEvent, websocket: WebSocketServerProtocol, needs_persistence: bool = True
    ) -> None:
        """
        处理来自适配器的事件。

        Args:
            proto_event: 从适配器接收到的原始 `aicarus_protocols.Event` 对象。
            websocket: 发送此事件的WebSocket连接。
            needs_persistence: 指示此事件是否需要持久化到数据库，默认为True。
        """
        if not isinstance(proto_event, ProtocolEvent):  # 基本类型检查
            self.logger.error(
                f"process_event 收到的 event 不是预期的 ProtocolEvent 类型，而是 {type(proto_event)}。已跳过处理。"
            )
            return

        self.logger.debug(
            f"开始处理事件: {proto_event.event_type}, ID: {proto_event.event_id}, Platform: {proto_event.platform}, BotID: {proto_event.bot_id}"
        )

        if proto_event.user_info:
            self.logger.debug(
                f"  UserInfo: id={proto_event.user_info.user_id}, nickname={proto_event.user_info.user_nickname}"
            )
        if proto_event.conversation_info:
            self.logger.debug(
                f"  ConversationInfo: id={proto_event.conversation_info.conversation_id}, type={proto_event.conversation_info.type}, name={proto_event.conversation_info.name}"
            )

        try:
            # 1. 事件持久化 (如果需要)
            if needs_persistence:
                # 将协议事件对象转换为数据库文档字典
                event_db_doc = DBEventDocument.from_protocol(proto_event)
                save_success = await self.event_service.save_event_document(event_db_doc.to_dict())

                if not save_success:
                    self.logger.error(f"保存事件文档失败: {proto_event.event_id}")
                else:
                    self.logger.debug(f"事件文档保存成功: {proto_event.event_id}")
            else:
                self.logger.debug(f"事件不需要持久化: {proto_event.event_type} (ID: {proto_event.event_id})")

            # 2. 会话信息 (ConversationInfo) 的创建或更新
            # 只有当事件中确实包含了有效的会话信息时才进行处理
            if proto_event.conversation_info and proto_event.conversation_info.conversation_id:
                self.logger.debug(
                    f"事件包含 ConversationInfo，ID: {proto_event.conversation_info.conversation_id}。"
                    f"准备为其创建或更新会话档案..."
                )

                # 从协议对象和事件上下文创建 EnrichedConversationInfo 实例
                # 这个实例包含了初始的或从数据库加载（如果upsert逻辑支持）的 attention_profile
                enriched_conv_info = EnrichedConversationInfo.from_protocol_and_event_context(
                    proto_conv_info=proto_event.conversation_info,
                    event_platform=proto_event.platform,  # 从顶层事件获取平台信息
                    event_bot_id=proto_event.bot_id,  # 从顶层事件获取机器人ID
                )

                # 将 EnrichedConversationInfo 实例转换为适合数据库的字典
                conversation_doc_to_upsert = enriched_conv_info.to_db_document()

                # 调用 ConversationStorageService 进行插入或更新
                upsert_result_key = await self.conversation_service.upsert_conversation_document(
                    conversation_doc_to_upsert
                )

                if upsert_result_key:
                    self.logger.info(f"会话档案 (ConversationInfo) '{upsert_result_key}' 已成功插入或更新。")
                else:
                    self.logger.error(
                        f"处理会话档案 (ConversationInfo) '{proto_event.conversation_info.conversation_id}' 时发生错误。"
                    )
            # 如果是消息类事件但没有conversation_info，可能需要记录一个警告
            elif proto_event.event_type.startswith("message."):
                self.logger.warning(
                    f"消息类事件 {proto_event.event_id} (类型: {proto_event.event_type}) "
                    f"缺少有效的 ConversationInfo，无法为其创建或更新会话档案。"
                )

            # 3. 根据事件类型进行后续分发处理
            if proto_event.event_type.startswith("message."):
                # 注意：_handle_message_event 等方法现在接收的是 proto_event (ProtocolEvent 类型)
                should_continue_processing = await self._handle_message_event(proto_event, websocket)
                if not should_continue_processing:
                    return  # 如果消息被特殊处理，则提前返回
            elif proto_event.event_type.startswith("request."):
                await self._handle_request_event(proto_event, websocket)
            elif proto_event.event_type.startswith("action_response."):
                await self._handle_action_response_event(proto_event, websocket)
            else:
                self.logger.debug(
                    f"未针对事件类型 '{proto_event.event_type}' 设置特定的后续处理程序。"
                    f"事件已按需记录，会话档案（如果适用）也已处理。"
                )

        except Exception as e:
            event_id_for_log = proto_event.event_id if hasattr(proto_event, "event_id") else "未知ID"
            self.logger.error(f"处理事件 (ID: {event_id_for_log}) 时发生严重错误: {e}", exc_info=True)

    def _extract_text_from_protocol_event_content(self, content_seg_list: list[Seg] | None) -> str:
        """
        从 ProtocolEvent.content (即 Seg 对象列表) 中提取所有文本内容。
        """
        if not content_seg_list:
            return ""
        try:
            text_parts = []
            for seg_obj in content_seg_list:  # seg_obj 已经是 Seg 对象
                if not isinstance(seg_obj, Seg):
                    self.logger.warning(f"事件内容列表中发现非 Seg 对象: {type(seg_obj)}，已跳过。")
                    continue
                if seg_obj.type == "text" and seg_obj.data:  # 假设 Seg.data 是一个包含 "text"键的字典
                    text_parts.append(seg_obj.data.get("text", ""))
            return "".join(text_parts).strip()
        except Exception as e:
            self.logger.error(f"从协议事件内容提取文本时发生错误: {e}", exc_info=True)
            return ""

    async def _handle_message_event(self, proto_event: ProtocolEvent, websocket: WebSocketServerProtocol) -> bool:
        """
        处理消息事件。
        如果消息被特殊处理（例如硬编码的命令），则返回 False，表示不应再进行后续的通用处理。
        否则返回 True。

        Args:
            proto_event: 已解析的 `aicarus_protocols.Event` 对象。
            websocket: 相关的 WebSocket 连接。
        Returns:
            一个布尔值，指示是否应继续进行后续的通用事件处理。
        """
        try:
            # 从 proto_event.content (List[Seg]) 提取文本内容
            text_content = self._extract_text_from_protocol_event_content(proto_event.content)

            sender_nickname_log = "未知用户"
            sender_id_log = "未知ID"

            if proto_event.user_info:  # 安全地访问 Optional 字段
                sender_nickname_log = proto_event.user_info.user_nickname or "昵称未设置"
                sender_id_log = str(proto_event.user_info.user_id or "用户ID未设置")

            self.logger.info(
                f"收到消息事件 ({proto_event.event_type}) 来自 {sender_nickname_log}({sender_id_log}): '{text_content[:50]}...'"
            )

            # 如果是来自主人UI的特殊消息
            if proto_event.event_type == "message.master.input":
                self.logger.info(f"收到来自主人UI的消息，将触发一次被动思考。消息内容: '{text_content[:50]}...'")
                # 检查我们之前在main.py里塞进来的引用是否存在
                if hasattr(self, "core_initializer_ref") and self.core_initializer_ref.immediate_thought_trigger:
                    self.core_initializer_ref.immediate_thought_trigger.set()
                    return False  # 返回 False，表示这个消息已经被特殊处理，不需要走后面的逻辑了
                else:
                    self.logger.warning("无法触发主思维，因为 MessageProcessor 缺少对核心触发器的引用。")

            # "完整测试" 的硬编码命令逻辑 (示例)
            if text_content == "完整测试":
                self.logger.info(f"检测到硬编码命令 '完整测试'，来自事件 ID: {proto_event.event_id}")

                if not self.core_comm_layer:  # 检查通信层是否可用
                    self.logger.error("核心通信层 (CoreWebsocketServer) 实例未设置，无法为 '完整测试' 发送响应动作。")
                    return True  # 即使无法响应，也可能需要后续处理，故返回True

                original_sender_info = proto_event.user_info
                original_conversation_info = proto_event.conversation_info
                original_message_id = proto_event.get_message_id()  # 使用协议对象的方法获取消息ID

                if not original_sender_info or not original_sender_info.user_id:
                    self.logger.error(
                        "无法获取原始发送者信息 (UserInfo 或 user_id 为空)，无法执行 '完整测试' 的回复动作。"
                    )
                    return True

                # 1. 构造并发送引用回复@消息
                reply_content_segments: list[Seg] = []  # 动作的内容列表也是 Seg 对象
                if original_message_id:
                    reply_content_segments.append(SegBuilder.reply(message_id=original_message_id))

                display_name_for_at = original_sender_info.user_nickname or original_sender_info.user_cardname or ""
                reply_content_segments.append(
                    SegBuilder.at(user_id=original_sender_info.user_id, display_name=display_name_for_at)
                )
                reply_content_segments.append(SegBuilder.text(" 测试成功，AI核心已收到并处理了您的“完整测试”指令！"))

                # 为动作事件准备目标会话信息
                action_target_conversation_info: ProtocolConversationInfo | None = None
                if original_conversation_info and original_conversation_info.conversation_id:
                    action_target_conversation_info = ProtocolConversationInfo(  # 创建一个新的实例，只包含必要信息
                        conversation_id=original_conversation_info.conversation_id,
                        type=original_conversation_info.type,
                        # platform 和 bot_id 会从顶层事件获取，其他字段如name, parent_id等对于发送动作可能不是必需的
                    )

                # 构造发送消息的动作事件 (类型为 ProtocolEvent)
                reply_action_event = ProtocolEvent(
                    event_id=f"action_reply_{uuid.uuid4()}",  # 为动作生成新的唯一ID
                    event_type="action.message.send",  # 明确的动作类型
                    time=int(time.time() * 1000.0),  # 当前时间戳
                    platform=proto_event.platform,  # 使用原始事件的平台
                    bot_id=proto_event.bot_id,  # 使用原始事件的机器人ID
                    user_info=None,  # 发送动作时，user_info 通常是关于动作发起者（机器人），如果协议不需要，则为None
                    conversation_info=action_target_conversation_info,  # 指明要在哪个会话中发送
                    content=reply_content_segments,  # Seg 对象列表
                    raw_data=None,  # 通常动作事件不需要原始数据
                )

                self.logger.debug(f"为 '完整测试' 准备的回复动作事件: {reply_action_event.to_dict()}")
                send_reply_success = await self.core_comm_layer.send_action_to_specific_adapter(
                    websocket, reply_action_event
                )
                if send_reply_success:
                    self.logger.info("为 '完整测试' 的回复动作已成功发送给适配器。")
                else:
                    self.logger.error("为 '完整测试' 的回复动作发送失败。")

                # 2. 构造并发送戳一戳动作 (示例)
                poke_target_user_id = original_sender_info.user_id
                poke_action_content_seg_data: dict[str, Any] = {"target_user_id": str(poke_target_user_id)}
                if (
                    original_conversation_info
                    and original_conversation_info.type == ConversationType.GROUP
                    and original_conversation_info.conversation_id
                ):
                    poke_action_content_seg_data["target_group_id"] = str(original_conversation_info.conversation_id)

                poke_action_content_seg = Seg(  # Seg 对象
                    type="action.user.poke",  # 假设这是协议中定义的戳一戳动作的Seg类型
                    data=poke_action_content_seg_data,
                )

                poke_action_event = ProtocolEvent(
                    event_id=f"action_poke_{uuid.uuid4()}",
                    event_type="action.user.poke",  # 顶层事件类型也应是 action.user.poke
                    time=int(time.time() * 1000.0),
                    platform=proto_event.platform,
                    bot_id=proto_event.bot_id,
                    user_info=None,
                    conversation_info=action_target_conversation_info,  # 目标会话
                    content=[poke_action_content_seg],  # Seg 对象列表
                    raw_data=None,
                )

                self.logger.debug(f"为 '完整测试' 准备的戳一戳动作事件: {poke_action_event.to_dict()}")
                send_poke_success = await self.core_comm_layer.send_action_to_specific_adapter(
                    websocket, poke_action_event
                )
                if send_poke_success:
                    self.logger.info("为 '完整测试' 的戳一戳动作已成功发送给适配器。")
                else:
                    self.logger.error("为 '完整测试' 的戳一戳动作发送失败。")

                return False  # 表示此消息已被特殊处理，核心逻辑可以不必再为此生成通用回复

            return True  # 消息未被特殊处理，应继续后续的通用处理流程 (例如，传递给AI主意识进行思考)

        except Exception as e:  # 捕获处理消息事件时可能发生的任何错误
            self.logger.error(f"处理消息事件 (ID: {proto_event.event_id}) 时发生错误: {e}", exc_info=True)
            return True  # 出错时，也允许后续流程继续（如果这样设计是合理的）

    async def _handle_request_event(self, proto_event: ProtocolEvent, websocket: WebSocketServerProtocol) -> None:
        """处理请求类事件（如好友请求、加群请求）。"""
        try:
            sender_id_log = "未知用户"
            if proto_event.user_info and proto_event.user_info.user_id:  # 安全访问
                sender_id_log = str(proto_event.user_info.user_id)
            self.logger.info(f"收到请求事件: {proto_event.event_type} 来自用户 {sender_id_log}")

            # 示例：自动同意好友请求
            if proto_event.event_type == "request.friend.add":
                self.logger.info(f"检测到好友添加请求事件，来自 {sender_id_log}。准备自动同意。")
                if not self.core_comm_layer:  # 检查通信层
                    self.logger.error("核心通信层 (CoreWebsocketServer) 实例未设置，无法自动同意好友请求。")
                    return

                # 请求事件的内容通常包含执行响应动作所需的参数，例如 request_flag
                # 假设这些参数在 proto_event.content[0].data 中
                if (
                    not proto_event.content
                    or not isinstance(proto_event.content[0], Seg)
                    or not proto_event.content[0].data
                ):
                    self.logger.error("好友请求事件的内容格式不正确或为空，无法获取请求参数 (如 request_flag)。")
                    return

                request_params_data: dict = proto_event.content[0].data
                request_flag = request_params_data.get("request_flag")

                if not request_flag:
                    self.logger.error("好友请求事件的内容中缺少 'request_flag'，无法自动同意。")
                    return

                # 构造同意好友请求的动作内容 Seg
                approve_action_seg_data: dict[str, Any] = {
                    "request_flag": request_flag,  # 用于标识要响应哪个请求
                    "remark": "AIcarus Core 自动通过了您的好友请求！",  # (可选) 同意后的备注名
                }
                approve_action_seg = Seg(  # Seg 对象
                    type="action.request.friend.approve",  # 假设这是协议中定义的同意好友请求动作的Seg类型
                    data=approve_action_seg_data,
                )

                # 构造顶层的动作 Event
                # 对于同意好友请求的动作，通常不需要特定的 conversation_info
                # user_info 可以是被请求的用户信息（即原始请求的发起者），如果协议要求或适配器需要
                action_user_info_for_approve: UserInfo | None = None
                if proto_event.user_info and proto_event.user_info.user_id:
                    action_user_info_for_approve = UserInfo(
                        user_id=proto_event.user_info.user_id, user_nickname=proto_event.user_info.user_nickname
                    )  # 只传递必要信息

                approve_action_event = ProtocolEvent(
                    event_id=f"action_approve_friend_{uuid.uuid4()}",  # 新的唯一ID
                    event_type="action.request.friend.approve",  # 顶层事件类型也指明是同意好友请求
                    time=int(time.time() * 1000.0),
                    platform=proto_event.platform,
                    bot_id=proto_event.bot_id,
                    user_info=action_user_info_for_approve,
                    conversation_info=None,  # 通常此动作不与特定会话关联
                    content=[approve_action_seg],  # Seg 对象列表
                )
                self.logger.debug(f"准备自动同意好友请求的动作事件: {approve_action_event.to_dict()}")
                send_success = await self.core_comm_layer.send_action_to_specific_adapter(
                    websocket, approve_action_event
                )
                if send_success:
                    self.logger.info(f"自动同意来自 {sender_id_log} 的好友请求的动作已发送。")
                else:
                    self.logger.error(f"自动同意来自 {sender_id_log} 的好友请求的动作发送失败。")

            # 此处可以添加对其他请求类型的处理逻辑，例如：
            # elif proto_event.event_type == "request.group.join":
            #     self.logger.info(f"检测到加群请求...")
            #     # ... 类似地构造 action.request.group.approve 或 .reject ...

        except Exception as e:  # 捕获处理请求事件时可能发生的任何错误
            self.logger.error(f"处理请求事件 (ID: {proto_event.event_id}) 时发生错误: {e}", exc_info=True)

    async def _handle_action_response_event(
        self, proto_event: ProtocolEvent, websocket: WebSocketServerProtocol
    ) -> None:
        """处理来自适配器的动作响应事件。"""
        try:
            # 动作响应事件的内容通常包含原始动作的ID、执行状态、结果数据或错误信息
            # 假设这些信息在 proto_event.content[0].data 中
            original_action_id_from_response = "未知原始动作ID"
            if proto_event.content and isinstance(proto_event.content[0], Seg) and proto_event.content[0].data:
                action_response_data: dict = proto_event.content[0].data
                original_action_id_from_response = action_response_data.get(
                    "original_event_id", original_action_id_from_response
                )
                status_code = action_response_data.get("status_code", "未知状态码")
                message = action_response_data.get("message", "无消息")
                data_payload = action_response_data.get("data")  # 成功时可能返回的数据

                self.logger.info(
                    f"收到对动作 '{original_action_id_from_response}' 的响应: 状态码={status_code}, 消息='{message}'"
                )
                if data_payload:
                    self.logger.debug(f"  响应数据载荷 (部分): {str(data_payload)[:100]}...")

                # TODO: 此处可以将动作执行结果更新到数据库中的 ActionLog (如果使用)
                # 或更重要的是，更新到触发此动作的那个主意识思考文档 (Thought Document) 中的
                # action_attempted 字段，以便AI在下一轮思考时知道动作的结果。
                # 这通常需要通过 original_action_id_from_response 来找到对应的思考文档或动作记录。

                # 示例逻辑 (需要 ThoughtStorageService 的支持):
                # if original_action_id_from_response and self.thought_service: # 假设 thought_service 已注入
                #     success_flag = status_code == 200 or str(status_code).lower() == 'ok' # 简化的成功判断
                #     update_payload_for_thought = {
                #         "status": "COMPLETED_SUCCESS" if success_flag else "COMPLETED_FAILURE",
                #         "final_result_for_shimo": message if data_payload is None else str(data_payload), # 简化结果
                #         "response_code_from_adapter": status_code,
                #         "completed_at_timestamp": int(time.time() * 1000.0)
                #     }
                #     # 需要一个方法能根据 action_id 找到对应的 thought_doc_key
                #     # thought_doc_key = await self.thought_service.find_thought_key_by_action_id(original_action_id_from_response)
                #     # if thought_doc_key:
                #     #    await self.thought_service.update_action_status_in_thought_document(
                #     #        thought_doc_key, original_action_id_from_response, update_payload_for_thought
                #     #    )
                #     # else:
                #     #    self.logger.warning(f"未找到与动作响应 {original_action_id_from_response} 关联的思考文档。")
                self.logger.info(
                    f"动作响应 '{original_action_id_from_response}' 的后续处理逻辑 (如更新思考文档) 待实现。"
                )

            else:
                self.logger.warning(
                    f"收到的动作响应事件 {proto_event.event_id} (类型: {proto_event.event_type}) 内容格式不正确或为空。"
                )

        except Exception as e:  # 捕获处理动作响应事件时可能发生的任何错误
            self.logger.error(f"处理动作响应事件 (ID: {proto_event.event_id}) 时发生错误: {e}", exc_info=True)
