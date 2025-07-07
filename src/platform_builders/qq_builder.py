# src/platform_builders/qq_builder.py (小色猫·最终高潮·一步到胃版)
import time
import uuid
from typing import Any

from aicarus_protocols import ConversationInfo, Event, Seg

from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.platform_builders.base_builder import BasePlatformBuilder

logger = get_logger(__name__)


class QQBuilder(BasePlatformBuilder):
    """
    专门伺候 Napcat QQ 这个平台的翻译官 (V6.0 命名空间统治版)。
    它知道所有 QQ 平台能干的“脏活累活”，并能把核心的“通用指令”翻译成它们。
    """

    @property
    def platform_id(self) -> str:
        # 哼，我的平台ID是不会变的！
        # 注意：这里要和Adapter的core_platform_id完全一致！
        return "napcat_qq"

    def build_action_event(self, action_name: str, params: dict[str, Any]) -> Event | None:
        """
        把平台内唯一的“动作别名”(action_name)和参数，翻译成一个带有完整命名空间的标准Event。
        """
        # 我的“服务菜单”，key是动作别名，value是具体的翻译方法
        action_builders = {
            "send_message": self._build_send_message,
            "send_forward_message": self._build_send_forward_message,
            "recall_message": self._build_recall_message,
            "poke_user": self._build_poke_user,
            "kick_member": self._build_kick_member,
            "ban_member": self._build_ban_member,
            "ban_all_members": self._build_ban_all_members,
            "set_member_card": self._build_set_member_card,
            "set_member_title": self._build_set_member_title,
            "leave_conversation": self._build_leave_conversation,
            "handle_friend_request": self._build_handle_friend_request,
            "handle_group_request": self._build_handle_group_request,
            "get_group_info": self._build_get_group_info,
            "get_bot_profile": self._build_get_bot_profile,
            "sign_in": self._build_sign_in,
            "set_status": self._build_set_status,
            "set_avatar": self._build_set_avatar,
            "get_history": self._build_get_history,
            "get_list": self._build_get_list,
        }

        if builder_func := action_builders.get(action_name):
            # 找到对应的翻译方法，让它干活
            return builder_func(params)

        logger.warning(f"QQBuilder 还不知道怎么翻译这个动作: {action_name}")
        return None

    # --- 下面是每个动作的具体“翻译”实现 ---
    def _build_get_list(self, params: dict[str, Any]) -> Event | None:
        """构建获取列表的动作事件，现在群聊和好友都认识了。"""
        list_type = params.get("list_type")

        # 哼，现在我只检查你是不是给了我认识的类型
        if list_type not in ["group", "friend"]:
            logger.error(f"QQBuilder 不支持获取 '{list_type}' 类型的列表。")
            return None

        final_event_type = f"action.{self.platform_id}.get_list"
        # Seg 的 data 里要带上 list_type，好让适配器知道要干嘛
        action_seg = Seg(type="action_params", data={"list_type": list_type})

        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[action_seg],
        )

    def _build_send_message(self, params: dict[str, Any]) -> Event | None:
        conversation_id = params.get("conversation_id")
        content_segs_data = params.get("content", [])
        # 啊~❤ 多么纯粹，多么直接！去掉了中间那层多余的内裤！
        final_event_type = params.get("event_type", f"action.{self.platform_id}.send_message")

        if not conversation_id or not isinstance(content_segs_data, list):
            logger.warning(
                "Missing or invalid parameters in _build_send_message: conversation_id=%r, content=%r",
                conversation_id,
                content_segs_data,
            )
            return None
        # 注意！send_message比较特殊，它的content就是消息本身，所以Seg的type就是text, image等
        message_segs = [Seg(type=seg.get("type"), data=seg.get("data", {})) for seg in content_segs_data]
        conv_type = params.get("conversation_type", "group")

        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=message_segs,
            conversation_info=ConversationInfo(conversation_id=str(conversation_id), type=conv_type),
        )

    def _build_send_forward_message(self, params: dict[str, Any]) -> Event | None:
        nodes = params.get("nodes", [])
        if not nodes:
            return None

        # 合并转发的content也比较特殊，是node列表
        # 验证节点数据结构
        def is_valid_node_data(node_data: dict[str, Any]) -> bool:
            # 检查是否为字典且包含必需字段
            return isinstance(node_data, dict) and "id" in node_data and "content" in node_data

        valid_nodes = [node_data for node_data in nodes if is_valid_node_data(node_data)]
        if not valid_nodes:
            logger.warning("在构建合并转发消息时未找到有效节点")
            return None

        node_segs = [Seg(type="node", data=node_data) for node_data in valid_nodes]
        conv_info_dict = params.get("conversation_info", {})
        # 啊~❤ 统一的快感！
        final_event_type = f"action.{self.platform_id}.send_forward_message"
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=node_segs,
            conversation_info=ConversationInfo.from_dict(conv_info_dict),
        )

    def _build_recall_message(self, params: dict[str, Any]) -> Event | None:
        target_message_id = params.get("target_message_id")
        if not target_message_id:
            return None
        # 啊~❤ 就是这个感觉！
        final_event_type = f"action.{self.platform_id}.recall_message"
        # 看！Seg现在只做一件事：告诉你“我这里有这个动作的参数”，多纯粹！
        recall_seg = Seg(type="action_params", data={"target_message_id": str(target_message_id)})
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[recall_seg],
        )

    def _build_poke_user(self, params: dict[str, Any]) -> Event | None:
        user_id = params.get("user_id")
        conversation_id = params.get("conversation_id")
        if not user_id or not conversation_id:
            return None
        # 啊~❤ 戳进去了！
        final_event_type = f"action.{self.platform_id}.poke_user"
        poke_seg = Seg(
            type="action_params", data={"target_user_id": str(user_id), "target_group_id": str(conversation_id)}
        )
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[poke_seg],
            conversation_info=ConversationInfo(conversation_id=str(conversation_id), type="group"),
        )

    def _build_kick_member(self, params: dict[str, Any]) -> Event | None:
        group_id, user_id = params.get("group_id"), params.get("user_id")
        if not group_id or not user_id:
            return None
        final_event_type = f"action.{self.platform_id}.kick_member"
        kick_seg = Seg(
            type="action_params",
            data={
                "group_id": str(group_id),
                "user_id": str(user_id),
                "reject_add_request": params.get("reject_add_request", False),
            },
        )
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[kick_seg],
        )

    def _build_ban_member(self, params: dict[str, Any]) -> Event | None:
        group_id, user_id = params.get("group_id"), params.get("user_id")
        if not group_id or not user_id:
            return None
        final_event_type = f"action.{self.platform_id}.ban_member"
        ban_seg = Seg(
            type="action_params",
            data={"group_id": str(group_id), "user_id": str(user_id), "duration": params.get("duration", 60)},
        )
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[ban_seg],
        )

    def _build_ban_all_members(self, params: dict[str, Any]) -> Event | None:
        group_id = params.get("group_id")
        if not group_id:
            return None
        final_event_type = f"action.{self.platform_id}.ban_all_members"
        ban_all_seg = Seg(
            type="action_params",
            data={"group_id": str(group_id), "enable": params.get("enable", True)},
        )
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[ban_all_seg],
        )

    def _build_set_member_card(self, params: dict[str, Any]) -> Event | None:
        group_id, user_id = params.get("group_id"), params.get("user_id")
        if not group_id or not user_id:
            return None
        final_event_type = f"action.{self.platform_id}.set_member_card"
        card_seg = Seg(
            type="action_params",
            data={"group_id": str(group_id), "user_id": str(user_id), "card": params.get("card", "")},
        )
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[card_seg],
        )

    def _build_set_member_title(self, params: dict[str, Any]) -> Event | None:
        group_id, user_id = params.get("group_id"), params.get("user_id")
        if not group_id or not user_id:
            return None
        final_event_type = f"action.{self.platform_id}.set_member_title"
        title_seg = Seg(
            type="action_params",
            data={
                "group_id": str(group_id),
                "user_id": str(user_id),
                "special_title": params.get("special_title", ""),
                "duration": params.get("duration", -1),
            },
        )
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[title_seg],
        )

    def _build_leave_conversation(self, params: dict[str, Any]) -> Event | None:
        group_id = params.get("group_id")
        if not group_id:
            return None
        final_event_type = f"action.{self.platform_id}.leave_conversation"
        leave_seg = Seg(
            type="action_params",
            data={"group_id": str(group_id), "is_dismiss": params.get("is_dismiss", False)},
        )
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[leave_seg],
        )

    def _build_handle_friend_request(self, params: dict[str, Any]) -> Event | None:
        request_flag, approve = params.get("request_flag"), params.get("approve", False)
        if not request_flag:
            return None
        # 啊~❤ 现在只有一个动作名了，把 approve 当作参数传进去，好淫荡！
        final_event_type = f"action.{self.platform_id}.handle_friend_request"
        req_seg = Seg(
            type="action_params",
            data={"request_flag": str(request_flag), "approve": approve, "remark": params.get("remark")},
        )
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[req_seg],
        )

    def _build_handle_group_request(self, params: dict[str, Any]) -> Event | None:
        request_flag, approve, sub_type = (
            params.get("request_flag"),
            params.get("approve", False),
            params.get("original_request_sub_type"),
        )
        if not request_flag or not sub_type:
            return None
        # 啊~❤ 这里也一样！
        final_event_type = f"action.{self.platform_id}.handle_group_request"
        req_seg = Seg(
            type="action_params",
            data={
                "request_flag": str(request_flag),
                "approve": approve,
                "reason": params.get("reason"),
                "original_request_sub_type": sub_type,
            },
        )
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[req_seg],
        )

    def _build_get_group_info(self, params: dict[str, Any]) -> Event | None:
        group_id = params.get("group_id")
        if not group_id:
            return None
        final_event_type = f"action.{self.platform_id}.get_group_info"
        # 这个动作没有参数，所以 content 可以为空列表
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[],
            conversation_info=ConversationInfo(conversation_id=str(group_id), type="group"),
        )

    def _build_get_bot_profile(self, params: dict[str, Any]) -> Event | None:
        group_id = params.get("group_id")
        final_event_type = f"action.{self.platform_id}.get_bot_profile"
        action_seg = Seg(type="action_params", data={"group_id": str(group_id)} if group_id else {})
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[action_seg],
        )

    def _build_sign_in(self, params: dict[str, Any]) -> Event | None:
        group_id = params.get("group_id")
        if not group_id:
            return None
        final_event_type = f"action.{self.platform_id}.sign_in"
        sign_in_seg = Seg(type="action_params", data={"group_id": str(group_id)})
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[sign_in_seg],
        )

    def _build_set_status(self, params: dict[str, Any]) -> Event | None:
        status = params.get("status")
        if status is None:
            return None
        final_event_type = f"action.{self.platform_id}.set_status"
        status_seg = Seg(
            type="action_params",
            data={
                "status": status,
                "ext_status": params.get("ext_status", 0),
                "battery_status": params.get("battery_status", 100),
            },
        )
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[status_seg],
        )

    def _build_set_avatar(self, params: dict[str, Any]) -> Event | None:
        file = params.get("file")
        if not file:
            return None
        final_event_type = f"action.{self.platform_id}.set_avatar"
        avatar_seg = Seg(type="action_params", data={"file": file})
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[avatar_seg],
        )

    def _build_get_history(self, params: dict[str, Any]) -> Event | None:
        conv_info_dict = params.get("conversation_info")
        if not conv_info_dict:
            return None
        final_event_type = f"action.{self.platform_id}.get_history"
        history_seg = Seg(
            type="action_params",
            data={"message_seq": params.get("message_seq"), "count": params.get("count", 20)},
        )
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[history_seg],
            conversation_info=ConversationInfo.from_dict(conv_info_dict),
        )

    def get_action_definitions(self) -> dict[str, Any]:
        """
        提供一份我的“服务价目表”(JSON Schema参数定义)，给ActionHandler去看。
        这里的 key 也要和上面的方法名对应起来，哼！
        """
        base_definitions = {
            "send_message": {
                "type": "object",
                "description": "向指定的QQ群或好友发送一条或多条消息。",
                "properties": {
                    "conversation_id": {"type": "string", "description": "目标群号或QQ号。"},
                    "conversation_type": {"type": "string", "enum": ["group", "private"], "description": "会话类型。"},
                    "content": {
                        "type": "array",
                        "description": "一个消息段(Segment)列表，定义了要发送的内容。",
                        "items": {"type": "object"},
                    },
                },
                "required": ["conversation_id", "conversation_type", "content"],
            },
            "send_forward_message": {
                "type": "object",
                "description": "发送合并转发消息。",
                "properties": {
                    "conversation_info": {"type": "object", "description": "目标会话信息。"},
                    "nodes": {"type": "array", "description": "要转发的消息节点列表。", "items": {"type": "object"}},
                },
                "required": ["conversation_info", "nodes"],
            },
            "poke_user": {
                "type": "object",
                "description": "在指定的QQ群里戳一个成员。",
                "properties": {
                    "conversation_id": {"type": "string", "description": "目标群号。"},
                    "user_id": {"type": "string", "description": "要戳的用户的QQ号。"},
                },
                "required": ["conversation_id", "user_id"],
            },
            "recall_message": {
                "type": "object",
                "description": "撤回一条已发送的消息。",
                "properties": {"target_message_id": {"type": "string", "description": "要撤回的消息的ID。"}},
                "required": ["target_message_id"],
            },
            "kick_member": {
                "type": "object",
                "description": "从群聊中踢出成员。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "user_id": {"type": "string", "description": "要踢出的成员QQ号。"},
                    "reject_add_request": {"type": "boolean", "description": "是否拒绝该用户后续加群请求。"},
                },
                "required": ["group_id", "user_id"],
            },
            "ban_member": {
                "type": "object",
                "description": "禁言群成员。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "user_id": {"type": "string", "description": "要禁言的成员QQ号。"},
                    "duration": {"type": "integer", "description": "禁言时长（秒），0为解除禁言。"},
                },
                "required": ["group_id", "user_id", "duration"],
            },
            "ban_all_members": {
                "type": "object",
                "description": "开启或关闭全员禁言。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "enable": {"type": "boolean", "description": "True为开启，False为关闭。"},
                },
                "required": ["group_id", "enable"],
            },
            "set_member_card": {
                "type": "object",
                "description": "设置群成员名片。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "user_id": {"type": "string", "description": "目标成员QQ号。"},
                    "card": {"type": "string", "description": "新的群名片，空字符串为删除。"},
                },
                "required": ["group_id", "user_id"],
            },
            "set_member_title": {
                "type": "object",
                "description": "设置群成员专属头衔。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "user_id": {"type": "string", "description": "目标成员QQ号。"},
                    "special_title": {"type": "string", "description": "专属头衔内容。"},
                    "duration": {"type": "integer", "description": "头衔有效期（秒），-1为永久。"},
                },
                "required": ["group_id", "user_id", "special_title"],
            },
            "leave_conversation": {
                "type": "object",
                "description": "退出群聊或解散群聊。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "is_dismiss": {"type": "boolean", "description": "是否解散群聊（仅群主可用）。"},
                },
                "required": ["group_id"],
            },
            "handle_friend_request": {
                "type": "object",
                "description": "处理好友添加请求。",
                "properties": {
                    "request_flag": {"type": "string", "description": "从请求事件中获取的唯一标识。"},
                    "approve": {"type": "boolean", "description": "是否同意请求。"},
                    "remark": {"type": "string", "description": "（可选）同意后的备注名。"},
                },
                "required": ["request_flag", "approve"],
            },
            "handle_group_request": {
                "type": "object",
                "description": "处理加群请求或邀请。",
                "properties": {
                    "request_flag": {"type": "string", "description": "从请求事件中获取的唯一标识。"},
                    "approve": {"type": "boolean", "description": "是否同意请求。"},
                    "reason": {"type": "string", "description": "（可选）拒绝理由。"},
                    "original_request_sub_type": {
                        "type": "string",
                        "enum": ["join_application", "invite_received"],
                        "description": "原始请求的子类型。",
                    },
                },
                "required": ["request_flag", "approve", "original_request_sub_type"],
            },
            "get_group_info": {
                "type": "object",
                "description": "获取群聊详细信息。",
                "properties": {"group_id": {"type": "string", "description": "目标群号。"}},
                "required": ["group_id"],
            },
            "get_bot_profile": {
                "type": "object",
                "description": "获取机器人自身在一个或所有群聊中的档案信息。",
                "properties": {
                    "group_id": {"type": "string", "description": "（可选）如果提供，则只获取指定群聊中的档案。"}
                },
            },
            "sign_in": {
                "type": "object",
                "description": "在指定群聊中进行签到。",
                "properties": {"group_id": {"type": "string", "description": "目标群号。"}},
                "required": ["group_id"],
            },
            "set_status": {
                "type": "object",
                "description": "设置机器人在线状态。",
                "properties": {
                    "status": {"type": "integer", "description": "在线状态码。"},
                    "ext_status": {"type": "integer", "description": "扩展状态码。"},
                    "battery_status": {"type": "integer", "description": "电量状态。"},
                },
                "required": ["status"],
            },
            "set_avatar": {
                "type": "object",
                "description": "设置机器人头像。",
                "properties": {"file": {"type": "string", "description": "图片文件路径或URL或Base64。"}},
                "required": ["file"],
            },
            "get_history": {
                "type": "object",
                "description": "获取历史消息记录。",
                "properties": {
                    "conversation_info": {"type": "object", "description": "目标会话信息。"},
                    "message_seq": {"type": "string", "description": "（可选）起始消息序号。"},
                    "count": {"type": "integer", "description": "获取的消息数量。"},
                },
                "required": ["conversation_info"],
            },
        }
        base_definitions["get_list"] = {
            "type": "object",
            "description": "获取机器人加入的群聊列表。",
            "properties": {
                "list_type": {
                    "type": "string",
                    "enum": ["group", "friend"],
                    "description": "要获取的列表类型，支持 'group' 或 'friend'。",
                },
                "motivation": {"type": "string", "description": "获取这个列表的动机。"},
            },
            "required": ["list_type"],
        }
        return base_definitions
