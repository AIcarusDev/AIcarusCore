# src/message_processing/default_message_processor.py
import datetime
import time
import uuid
from typing import Any  # 替换 Optional 为 X | None

from aicarus_protocols import BaseMessageInfo, GroupInfo, MessageBase, Seg, UserInfo
from websockets.server import WebSocketServerProtocol

from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import AlcarusRootConfig
from src.core_communication.message_sender import MessageSender  # 假设 MessageSender 在此路径
from src.database.arangodb_handler import ArangoDBHandler

logger = get_logger("AIcarusCore.message_processor")


class DefaultMessageProcessor:
    def __init__(self, db_handler: ArangoDBHandler, root_config: AlcarusRootConfig) -> None:
        """
        初始化默认消息处理器。
        :param db_handler: ArangoDBHandler 实例，用于数据库交互。
        :param root_config: 全局配置信息。
        """
        self.db_handler = db_handler
        self.config = root_config
        self.bot_name = root_config.persona.bot_name  # 用于机器人身份标识
        self.message_sender = MessageSender()
        logger.info("DefaultMessageProcessor 初始化完成 (包含 MessageSender)。")

    def _generate_conversation_id(self, platform: str, group_id: str | None, user_id: str | None) -> str:
        """
        生成唯一的会话ID。
        """
        if group_id:
            return f"{platform}_{group_id}"
        elif user_id:
            # 使用 bot_name (机器人的唯一ID) 来确保私聊会话ID的稳定性
            participants = sorted([user_id, self.bot_name])
            return f"{platform}_dm_{participants[0]}_{participants[1]}"
        else:
            logger.warning("无法生成有效的 conversation_id：缺少 group_id 和 user_id。将生成随机ID。")
            return f"{platform}_unknown_{str(uuid.uuid4())}"

    async def process_message(self, message: MessageBase, websocket: WebSocketServerProtocol) -> None:
        """
        处理从适配器收到的单个 AIcarusMessageBase 消息。
        """
        # 首先检查消息对象和基本信息是否存在
        if not message or not message.message_info:
            logger.error(f"核心消息处理器收到无效消息对象或缺少 message_info。消息: {message}")
            return

        if (
            message.message_info.interaction_purpose == "platform_meta"
            or message.message_info.interaction_purpose == "platform_heartbeat"
        ):
            logger.debug(f"跳过平台元消息/心跳包 ({message.message_info.interaction_purpose})，不进行处理。")
            return

        text_content = ""
        original_sender_user_id: str | None = None
        original_sender_user_info: UserInfo | None = None
        original_group_id: str | None = None
        original_group_info: GroupInfo | None = None

        if message.message_info.user_info and message.message_info.user_info.user_id:
            original_sender_user_id = message.message_info.user_info.user_id
            original_sender_user_info = message.message_info.user_info

        if message.message_info.group_info and message.message_info.group_info.group_id:
            original_group_id = message.message_info.group_info.group_id
            original_group_info = message.message_info.group_info

        # --- 文本提取逻辑，包含增强日志 ---
        if message.message_segment:
            segments_to_process: list[Seg] = []
            current_outer_segment = message.message_segment  # message.message_segment 应该是一个 Seg 对象

            # 安全检查：如果 current_outer_segment 意外地是 dict (理论上不应发生，因 MessageBase.from_dict 已处理)
            if isinstance(current_outer_segment, dict):
                logger.warning("message.message_segment 是一个 dict，尝试转换为 Seg 对象。这通常不应发生。")
                try:
                    current_outer_segment = Seg.from_dict(current_outer_segment)
                except Exception as e_outer_conv:
                    logger.error(f"无法将 message_segment 从 dict 转换为 Seg 对象: {e_outer_conv}")
                    current_outer_segment = None  # 转换失败，置为 None

            if current_outer_segment and isinstance(
                current_outer_segment, Seg
            ):  # 确保 current_outer_segment 是有效的 Seg 对象
                if current_outer_segment.type == "seglist":
                    if isinstance(current_outer_segment.data, list):
                        for item_in_list in current_outer_segment.data:
                            if isinstance(item_in_list, Seg):  # 期望列表中的项目是 Seg 对象
                                segments_to_process.append(item_in_list)
                            elif isinstance(item_in_list, dict):  # 如果是 dict, 尝试转换 (兼容性处理)
                                logger.warning("seglist 中的项目是一个 dict，尝试转换为 Seg。")
                                try:
                                    segments_to_process.append(Seg.from_dict(item_in_list))
                                except Exception as e_item_conv:
                                    logger.error(f"无法将 seglist 中的 dict 项目转换为 Seg: {e_item_conv}")
                            else:  # 记录非预期的项目类型
                                logger.warning(f"seglist 中的项目既不是 Seg 也不是 dict: {type(item_in_list)}")
                elif isinstance(current_outer_segment, Seg):  # 如果外层不是 seglist，而是单个 Seg
                    segments_to_process.append(current_outer_segment)
                else:  # current_outer_segment 不是 Seg 对象 (例如，之前转换失败置为 None)
                    logger.warning(f"message_segment 不是有效的 Seg 对象: {type(current_outer_segment)}")
            elif not current_outer_segment:  # current_outer_segment 为 None
                logger.warning("message_segment 为 None 或转换失败，无法提取内容。")

            logger.debug(
                f"准备处理的 segments_to_process 列表 (共 {len(segments_to_process)} 个段): {[s.to_dict() for s in segments_to_process]}"
            )

            for seg_item in segments_to_process:
                logger.debug(
                    f"循环内检查 seg_item: type='{seg_item.type}', data_type='{type(seg_item.data)}', data_value='{str(seg_item.data)[:100]}'"
                )  # 截断 data_value 以防过长
                if seg_item.type == "text":
                    if isinstance(seg_item.data, str):
                        # 去除前后空格后追加
                        text_content += seg_item.data.strip()
                        logger.debug(
                            f"已追加文本 (str): '{seg_item.data.strip()}'. 当前 text_content: '{text_content}'"
                        )
                    # 兼容旧的/错误的 data 格式，即文本段的 data 是一个包含 "text" 键的字典
                    elif isinstance(seg_item.data, dict) and "text" in seg_item.data:
                        logger.warning(
                            f"文本段的 data 是一个字典 (协议要求为字符串)，尝试使用 data['text']。段: {seg_item.to_dict()}"
                        )
                        text_content += str(seg_item.data.get("text", "")).strip()
                        logger.debug(
                            f"已追加文本 (from dict): '{str(seg_item.data.get('text', '')).strip()}'. 当前 text_content: '{text_content}'"
                        )
                    else:
                        logger.warning(
                            f"文本段的 data 既不是字符串也不是预期的字典格式。类型: {type(seg_item.data)}, 内容: {str(seg_item.data)[:100]}"
                        )
                else:
                    logger.debug(f"跳过非文本段: type='{seg_item.type}'")
            logger.debug(f"文本提取循环结束后, text_content: '{text_content.strip()}'")
        else:
            logger.debug("message.message_segment 为空，无法提取文本内容。")

        # 这条日志现在应该在循环后，能准确反映最终的 text_content
        logger.debug(f"提取到的文本内容用于命令检查: '{text_content}'")

        if text_content.strip().lower() == "测试test":
            logger.info("接收到 '测试test' 命令。准备组合发送消息和戳一戳的核心动作。")

            if not original_sender_user_id:
                logger.warning("原始消息中缺少发送者 user_id，无法执行回复和戳一戳动作。")
                return

            action_segments_list: list[dict[str, Any]] = []  # 存储 Seg 对象的字典形式

            # 1. 构建 action:send_message Seg 的字典形式
            send_message_action_data: dict[str, Any] = {
                # 根据协议，action:send_message 的 segments 是一个 Seg 对象的列表
                # 每个 Seg 对象再转换为字典
                "segments": [Seg(type="text", data="测试成功").to_dict()],
            }
            if original_group_id:
                send_message_action_data["target_group_id"] = original_group_id
            else:
                send_message_action_data["target_user_id"] = original_sender_user_id

            send_message_action_seg_dict = Seg(type="action:send_message", data=send_message_action_data).to_dict()
            action_segments_list.append(send_message_action_seg_dict)

            # 2. 构建 action:send_poke Seg 的字典形式
            poke_action_data: dict[str, Any] = {"target_user_id": original_sender_user_id}
            if original_group_id:
                poke_action_data["target_group_id"] = original_group_id

            poke_action_seg_dict = Seg(type="action:send_poke", data=poke_action_data).to_dict()
            action_segments_list.append(poke_action_seg_dict)

            # 3. 构建包含这两个动作的 MessageBase (core_action)
            core_action_message_info = BaseMessageInfo(
                platform=message.message_info.platform,
                bot_id=self.bot_name,  # 动作由机器人发起
                message_id=f"core_action_test_reply_poke_{str(uuid.uuid4())}",
                time=int(time.time() * 1000),
                interaction_purpose="core_action",
                user_info=original_sender_user_info,  # 原始消息发送者作为动作的上下文/目标
                group_info=original_group_info,  # 原始消息群组作为动作的上下文/目标
                additional_config={"protocol_version": "1.2.0"},
            )

            # message_segment 的 data 是一个 Seg 对象的列表，这里我们用的是 Seg 对象的字典形式
            combined_action_message = MessageBase(
                message_info=core_action_message_info,
                message_segment=Seg(type="seglist", data=action_segments_list),  # data 是 List[Dict]
            )

            logger.info(f"准备发送组合动作 (回复并戳一下): {combined_action_message.to_dict()}")
            # 在访问 message_info 之前添加检查
            if not hasattr(message, "message_info") or not message.message_info:
                logger.error("消息对象缺少 message_info 属性或其值为 None。")
                return

            # 在发送消息时添加更多日志和异常捕获
            try:
                logger.debug(f"发送的组合动作消息内容: {combined_action_message.to_dict()}")
                send_success = await self.message_sender.send_message(websocket, combined_action_message)
                if send_success:
                    logger.info("组合动作 (回复并戳一下) 请求已成功发送。")
                else:
                    logger.error("发送组合动作 (回复并戳一下) 请求失败。")
            except Exception as e:
                logger.error(f"发送组合动作时发生异常: {e}", exc_info=True)

            return  # "测试test" 命令处理完毕，不再进行后续数据库存储等

        # --- 常规消息处理逻辑 (非 "测试test" 命令) ---
        logger.info(
            f"消息处理器收到来自适配器 ({websocket.remote_address}) 的消息，类型: {message.message_info.interaction_purpose} (非测试命令，将进行常规处理)"
        )
        logger.debug(f"完整收到的 AicarusMessageBase (常规处理): {message.to_dict()}")

        if not self.db_handler:
            logger.error("数据库处理器 (ArangoDBHandler) 未在消息处理器中初始化，无法存储消息。")
            return

        platform_id_str = message.message_info.platform or "unknown_platform"

        sender_user_id_internal_ref: str | None = None
        if original_sender_user_id:
            sender_user_id_internal_ref = f"Users/{platform_id_str}_{original_sender_user_id}"

        group_id_internal_ref: str | None = None
        if original_group_id:
            group_id_internal_ref = f"Groups/{platform_id_str}_{original_group_id}"

        conversation_id = self._generate_conversation_id(
            platform_id_str,
            original_group_id,
            original_sender_user_id,
        )

        content_segments_to_save = []
        if message.message_segment and isinstance(message.message_segment, Seg):
            segments_for_saving: list[Seg] = []  # 存储 Seg 对象
            current_outer_segment_for_save = message.message_segment

            # 此处 current_outer_segment_for_save 已经是 Seg 对象，无需再次 from_dict
            if current_outer_segment_for_save.type == "seglist" and isinstance(
                current_outer_segment_for_save.data, list
            ):
                for seg_obj_item in current_outer_segment_for_save.data:
                    if isinstance(seg_obj_item, Seg):  # 期望是 Seg 对象
                        segments_for_saving.append(seg_obj_item)
                    elif isinstance(seg_obj_item, dict):  # 兼容列表内是字典的情况
                        logger.warning("用于保存的 seglist 中的项目是一个 dict，尝试转换为 Seg。")
                        try:
                            segments_for_saving.append(Seg.from_dict(seg_obj_item))
                        except Exception as e_seg_save:
                            logger.error(f"无法转换消息段进行保存 (dict in list): {seg_obj_item}, 错误: {e_seg_save}")
                            content_segments_to_save.append(
                                {"type": "error", "data": f"原始段数据: {str(seg_obj_item)}"}
                            )
                    # ... (其他类型的错误处理可以添加) ...
            elif isinstance(current_outer_segment_for_save, Seg):  # 单个 Seg 对象
                segments_for_saving.append(current_outer_segment_for_save)

            for seg_to_save_item in segments_for_saving:
                content_segments_to_save.append(seg_to_save_item.to_dict())

        formatted_message_data: dict[str, Any] = {
            "platform_message_id": message.message_info.message_id,
            "conversation_id": conversation_id,
            "platform_id_ref": f"Platforms/{platform_id_str}",
            "group_id_ref": group_id_internal_ref,
            "sender_user_id_ref": sender_user_id_internal_ref,
            "bot_id_ref": None,  # 默认为 None，如果消息来自机器人则会被设置
            "timestamp": datetime.datetime.fromtimestamp(message.message_info.time / 1000.0).isoformat() + "Z",
            "message_type": message.message_info.interaction_purpose,  # 注意：协议中 user_message 有更细的 message_type
            "content_segments": content_segments_to_save,
            "mentions_bot": True,  # TODO: 实现准确的机器人被@检测逻辑
            "is_direct_message_to_bot": not bool(original_group_id),  # 简单判断是否为私聊
            "raw_message_info_dump": message.message_info.to_dict(),
            "raw_message_segment_dump": message.message_segment.to_dict() if message.message_segment else None,
        }

        # 判断消息是否由机器人自己发送 (例如，机器人响应动作后，适配器可能会将机器人的消息也上报)
        if original_sender_user_id == self.bot_name:
            formatted_message_data["sender_user_id_ref"] = None  # 清除用户发送者
            formatted_message_data["bot_id_ref"] = f"Bots/{self.bot_name}"  # 标记为机器人发送

        try:
            saved_message_key = await self.db_handler.save_raw_chat_message(formatted_message_data)
            if saved_message_key:
                logger.info(f"消息 (Key: {saved_message_key}, 会话: {conversation_id}) 已由消息处理器成功存入数据库。")
            else:
                logger.error(f"消息处理器未能保存消息 (会话: {conversation_id}) 到数据库或获取有效的key。")
        except Exception as e_save:
            logger.error(f"消息处理器在调用 save_raw_chat_message 时发生错误: {e_save}", exc_info=True)

        # 根据消息类型记录不同的日志，或触发后续逻辑
        if message.message_info.interaction_purpose == "user_message":
            # 如果是用户消息，并且不是 "测试test" 命令，可能需要传递给 LLM 或其他处理单元
            logger.info(f"用户消息 (会话: {conversation_id}) 已存储，可供后续核心逻辑处理。")
            # 例如: await self.core_logic_handler.process_user_message(message)
        elif message.message_info.interaction_purpose == "platform_request":
            logger.info(f"平台请求类型消息已存储，可供后续思考决策。会话: {conversation_id}")
        elif message.message_info.interaction_purpose == "platform_notification":
            logger.info(f"平台通知类型消息已存储。会话: {conversation_id}")
        # action_response 理论上不应由适配器发给此处理器，而是由 Core 内部处理
        # platform_meta 已在函数开头处理
