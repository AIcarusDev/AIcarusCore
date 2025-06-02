# src/message_processing/default_message_processor.py
import datetime
import time # 🐾 小猫爪：虽然没直接用，但原文件有，保留
import uuid
from typing import Any, Optional, TYPE_CHECKING, List, Dict # 🐾 小猫爪：导入 List, Dict

from aicarus_protocols import BaseMessageInfo, GroupInfo, MessageBase, Seg, UserInfo
from websockets.server import WebSocketServerProtocol

from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import AlcarusRootConfig
from src.core_communication.message_sender import MessageSender
from src.database.arangodb_handler import ArangoDBHandler

# 🐾 小猫爪：导入我们新建的 ChatSessionManager
from src.sub_consciousness.chat_session_handler import ChatSessionManager

if TYPE_CHECKING:
    from src.core_logic.main import CoreLogic # 🐾 小猫爪：用于类型提示 CoreLogic


logger = get_logger("AIcarusCore.message_processor")


class DefaultMessageProcessor:
    def __init__(
        self,
        db_handler: ArangoDBHandler,
        root_config: AlcarusRootConfig,
        # 🐾 小猫爪：新增 chat_session_manager 参数
        chat_session_manager: ChatSessionManager,
        core_logic_ref: 'CoreLogic' # 🐾 小猫爪：新增对 CoreLogic 的引用
    ):
        self.db_handler: ArangoDBHandler = db_handler
        self.config: AlcarusRootConfig = root_config
        self.bot_name: str = root_config.persona.bot_name
        self.message_sender: MessageSender = MessageSender()
        # 🐾 小猫爪：存储 ChatSessionManager 实例
        self.chat_session_manager: ChatSessionManager = chat_session_manager
        self.core_logic_ref: 'CoreLogic' = core_logic_ref # 存储 CoreLogic 引用
        logger.info("DefaultMessageProcessor 初始化完成 (包含 MessageSender 和 ChatSessionManager)。")

    def _generate_conversation_id(self, platform: str, group_id: Optional[str], user_id: Optional[str]) -> str:
        # 🐾 小猫爪：这里的 bot_name 应该是机器人自身的唯一标识，用于生成稳定的私聊ID
        robot_identifier = self.bot_name

        if group_id:
            return f"{platform}_group_{group_id}"
        elif user_id:
            participants = sorted([user_id, robot_identifier])
            return f"{platform}_dm_{participants[0]}_{participants[1]}"
        else:
            logger.warning("无法生成有效的 conversation_id：缺少 group_id 和 user_id。将生成随机ID。")
            return f"{platform}_unknown_{str(uuid.uuid4())}"

    async def process_message(self, message: MessageBase, websocket: WebSocketServerProtocol) -> None:
        if not message or not message.message_info:
            logger.error(f"核心消息处理器收到无效消息对象或缺少 message_info。消息: {message}")
            return

        if message.message_info.interaction_purpose in ["platform_meta", "platform_heartbeat"]:
            logger.debug(f"跳过平台元消息/心跳包 ({message.message_info.interaction_purpose})，不进行处理。")
            return

        text_content = ""
        original_sender_user_id: Optional[str] = None
        original_sender_user_info: Optional[UserInfo] = None
        original_group_id: Optional[str] = None
        original_group_info: Optional[GroupInfo] = None

        if message.message_info.user_info and message.message_info.user_info.user_id:
            original_sender_user_id = message.message_info.user_info.user_id
            original_sender_user_info = message.message_info.user_info

        if message.message_info.group_info and message.message_info.group_info.group_id:
            original_group_id = message.message_info.group_info.group_id
            original_group_info = message.message_info.group_info
        
        if message.message_segment:
            segments_to_process: List[Seg] = []
            current_outer_segment = message.message_segment
            if isinstance(current_outer_segment, dict):
                try:
                    current_outer_segment = Seg.from_dict(current_outer_segment)
                except Exception as e_outer_conv:
                    logger.error(f"无法将 message_segment 从 dict 转换为 Seg 对象: {e_outer_conv}")
                    current_outer_segment = None
            if current_outer_segment and isinstance(current_outer_segment, Seg):
                if current_outer_segment.type == "seglist":
                    if isinstance(current_outer_segment.data, list):
                        for item_in_list in current_outer_segment.data:
                            if isinstance(item_in_list, Seg):
                                segments_to_process.append(item_in_list)
                            elif isinstance(item_in_list, dict):
                                try:
                                    segments_to_process.append(Seg.from_dict(item_in_list))
                                except Exception as e_item_conv:
                                    logger.error(f"无法将 seglist 中的 dict 项目转换为 Seg: {e_item_conv}")
                elif isinstance(current_outer_segment, Seg):
                    segments_to_process.append(current_outer_segment)
            
            for seg_item in segments_to_process:
                if seg_item.type == "text":
                    if isinstance(seg_item.data, str):
                        text_content += seg_item.data.strip()
                    elif isinstance(seg_item.data, dict) and "text" in seg_item.data:
                        text_content += str(seg_item.data.get("text", "")).strip()
        
        logger.debug(f"提取到的文本内容用于命令检查: '{text_content}'")

        if text_content.strip().lower() == "测试test":
            logger.info("接收到 '测试test' 命令。准备组合发送消息和戳一戳的核心动作。")
            if not original_sender_user_id:
                logger.warning("原始消息中缺少发送者 user_id，无法执行回复和戳一戳动作。")
                return

            action_segments_list: List[Dict[str, Any]] = []
            send_message_action_data: Dict[str, Any] = {
                "segments": [Seg(type="text", data="测试成功").to_dict()],
            }
            if original_group_id:
                send_message_action_data["target_group_id"] = original_group_id
            else:
                send_message_action_data["target_user_id"] = original_sender_user_id
            send_message_action_seg_dict = Seg(type="action:send_message", data=send_message_action_data).to_dict()
            action_segments_list.append(send_message_action_seg_dict)

            poke_action_data: Dict[str, Any] = {"target_user_id": original_sender_user_id}
            if original_group_id:
                poke_action_data["target_group_id"] = original_group_id
            poke_action_seg_dict = Seg(type="action:send_poke", data=poke_action_data).to_dict()
            action_segments_list.append(poke_action_seg_dict)

            core_action_message_info = BaseMessageInfo(
                platform=message.message_info.platform,
                bot_id=self.bot_name,
                message_id=f"core_action_test_reply_poke_{str(uuid.uuid4())}",
                time=int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000),
                interaction_purpose="core_action",
                user_info=original_sender_user_info,
                group_info=original_group_info,
                additional_config={"protocol_version": "1.2.0"},
            )
            combined_action_message = MessageBase(
                message_info=core_action_message_info,
                message_segment=Seg(type="seglist", data=action_segments_list),
            )
            try:
                send_success = await self.message_sender.send_message(websocket, combined_action_message)
                if send_success:
                    logger.info("组合动作 (回复并戳一下) 请求已成功发送。")
                else:
                    logger.error("发送组合动作 (回复并戳一下) 请求失败。")
            except Exception as e:
                logger.error(f"发送组合动作时发生异常: {e}", exc_info=True)
            return

        logger.info(
            f"消息处理器收到来自适配器 ({websocket.remote_address}) 的消息，类型: {message.message_info.interaction_purpose} (将进行常规处理)"
        )

        if not self.db_handler:
            logger.error("数据库处理器 (ArangoDBHandler) 未在消息处理器中初始化，无法存储消息。")
            return
        if not self.chat_session_manager: 
            logger.error("ChatSessionManager 未在消息处理器中初始化，无法处理消息到子思维。")
            return

        platform_id_str = message.message_info.platform or "unknown_platform"
        conversation_id = self._generate_conversation_id(
            platform_id_str,
            original_group_id,
            original_sender_user_id 
        )
        
        content_segments_to_save = []
        if message.message_segment and isinstance(message.message_segment, Seg):
            segments_for_saving: List[Seg] = []
            current_outer_segment_for_save = message.message_segment
            if current_outer_segment_for_save.type == "seglist" and isinstance(current_outer_segment_for_save.data, list):
                for seg_obj_item in current_outer_segment_for_save.data:
                    if isinstance(seg_obj_item, Seg):
                        segments_for_saving.append(seg_obj_item)
                    elif isinstance(seg_obj_item, dict):
                        try:
                            segments_for_saving.append(Seg.from_dict(seg_obj_item))
                        except Exception as e_seg_save:
                            logger.error(f"无法转换消息段进行保存 (dict in list): {seg_obj_item}, 错误: {e_seg_save}")
            elif isinstance(current_outer_segment_for_save, Seg):
                segments_for_saving.append(current_outer_segment_for_save)
            for seg_to_save_item in segments_for_saving:
                content_segments_to_save.append(seg_to_save_item.to_dict())

        formatted_message_data: Dict[str, Any] = {
            "platform_message_id": message.message_info.message_id,
            "conversation_id": conversation_id, 
            "platform_id_ref": f"Platforms/{platform_id_str}", 
            "group_id_ref": f"Groups/{platform_id_str}_{original_group_id}" if original_group_id else None, 
            "sender_user_id_ref": f"Users/{platform_id_str}_{original_sender_user_id}" if original_sender_user_id else None, 
            "bot_id_ref": None,
            "timestamp": message.message_info.time, 
            "message_type": message.message_info.interaction_purpose,
            "content_segments": content_segments_to_save,
            "mentions_bot": True, 
            "is_direct_message_to_bot": not bool(original_group_id),
            "raw_message_info_dump": message.message_info.to_dict(),
            "raw_message_segment_dump": message.message_segment.to_dict() if message.message_segment else None,
        }
        if original_sender_user_id == self.bot_name: 
            formatted_message_data["sender_user_id_ref"] = None
            formatted_message_data["bot_id_ref"] = f"Bots/{self.bot_name}"

        try:
            saved_message_key = await self.db_handler.save_raw_chat_message(formatted_message_data)
            if saved_message_key:
                logger.info(f"消息 (Key: {saved_message_key}, 会话: {conversation_id}) 已由消息处理器成功存入数据库。")
            else:
                logger.error(f"消息处理器未能保存消息 (会话: {conversation_id}) 到数据库或获取有效的key。")
        except Exception as e_save:
            logger.error(f"消息处理器在调用 save_raw_chat_message 时发生错误: {e_save}", exc_info=True)
            return 

        # 🐾 小猫爪：将消息/事件传递给 ChatSessionManager 进行上下文更新
        if message.message_info.interaction_purpose == "user_message":
            logger.info(f"用户消息 (会话: {conversation_id}) 将传递给 ChatSessionManager 处理上下文。")
            await self.chat_session_manager.handle_incoming_user_message(message)
        elif message.message_info.interaction_purpose == "platform_notification":
            logger.info(f"平台通知 (会话: {conversation_id}) 将传递给 ChatSessionManager 处理上下文。")
            await self.chat_session_manager.handle_incoming_platform_event(message)
        elif message.message_info.interaction_purpose == "platform_request":
            logger.info(f"平台请求 (会话: {conversation_id}) 将传递给 ChatSessionManager 处理上下文 (作为一种平台事件)。")
            await self.chat_session_manager.handle_incoming_platform_event(message) # 平台请求也作为一种事件传递
        else:
            logger.debug(f"消息类型 {message.message_info.interaction_purpose} (会话: {conversation_id}) 当前不由 ChatSessionManager 直接处理上下文。")

