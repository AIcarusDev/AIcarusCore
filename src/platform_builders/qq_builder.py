# ruff: noqa: E501
# src/platform_builders/qq_builder.py
import time
import uuid
from typing import Any

from aicarus_protocols import ConversationInfo, Event, Seg
from src.common.custom_logging.logging_config import get_logger
from src.platform_builders.base_builder import BasePlatformBuilder

logger = get_logger(__name__)


class QQBuilder(BasePlatformBuilder):
    """QQ平台的构建器，负责将平台特有的动作翻译成标准Event格式.

    这个构建器是为QQ平台定制的，提供了所有QQ特有动作的翻译方法。

    Attributes:
        platform_id (str): 平台ID，唯一标识一个平台。
        这个ID必须和Adapter的core_platform_id完全一致，以确保适配器能够正确识别。
    """

    @property
    def needs_on_connect_inspection(self) -> bool:
        """QQ平台需要在连接时进行“上线安检”."""
        return True

    @property
    def is_person_platform(self) -> bool:
        """QQ平台具有身份内容, 返回 True."""
        return True

    def __init__(self) -> None:
        super().__init__()
        self._generic_actions = {
            "recall_message",
            "poke_user",
            "send_message",
            "kick_member",
            "ban_member",
            "ban_all_members",
            "set_member_card",
            "set_member_title",
            "leave_conversation",
            "handle_friend_request",
            "handle_group_request",
            "get_group_info",
            "get_bot_profile",
            "sign_in",
            "set_status",
            "set_avatar",
            "get_history",
            "get_list",
            "forward_single_message",
            "set_admin",
            "set_conversation_name",
            "get_group_files",
            "upload_group_file",
            "delete_group_item",
            "create_group_folder",
            "get_group_file_url",
            "get_group_honor_info",
            "send_group_notice",
            "get_group_notice",
            "set_message_emoji_like",
            "get_recent_contacts",
            "get_ai_characters",
            "send_ai_voice",
            "delete_friend",
        }

        self._special_action_handlers = {
            "send_message": self._build_send_message,
            "send_forward_message": self._build_send_forward_message,
        }

    @property
    def platform_id(self) -> str:
        """返回平台ID，唯一标识一个平台，这个ID必须和Adapter的core_platform_id完全一致."""
        return "qq"

    def build_action_event(
        self, action_name: str, params: dict[str, Any], bot_id: str
    ) -> Event | None:
        """这个方法负责将平台特有的动作转换成标准的Event格式，它会根据动作名称和参数来决定如何构建Event对象.

        Args:
            action_name (str): 动作名称，标识要执行的具体动作.
            params (dict[str, Any]): 动作参数，包含执行这个动作所需的所有信息.
            bot_id (str): 处理此动作的机器人的唯一标识符.

        Returns:
            Event | None: 返回一个Event对象，表示这个动作的执行结果.
            如果这个动作不在白名单或特殊处理列表中，则返回 None.

        这个方法的核心逻辑是：
        1. 先处理那些需要特殊处理的动作，比如发消息和合并转发.
        2. 然后，检查这个动作是不是在白名单里.
            如果在，就用通用模板来处理，并返回一个Event对象.
        3. 如果哪个都不沾，那就真的不认识了，返回 None.
        """
        # 1. 优先查找特殊处理函数
        if handler := self._special_action_handlers.get(action_name):
            return handler(params, bot_id)

        # 2. 其次检查是否在通用动作白名单中
        if action_name in self._generic_actions:
            conv_info = None
            if action_name in ["get_group_info", "get_history", "forward_single_message"]:
                conv_id = params.get("group_id") or params.get("conversation_id")
                if conv_id:
                    conv_info = ConversationInfo(conversation_id=str(conv_id), type="group")
            elif action_name == "poke_user" and (conv_id := params.get("target_group_id")):
                conv_info = ConversationInfo(conversation_id=str(conv_id), type="group")

            return self._build_generic_event(action_name, params, bot_id, conv_info)

        # 3. 如果都不匹配，那就是不认识的动作
        logger.warning(f"QQBuilder 的白名单和特殊名单里都没有这个动作: {action_name}")
        return None

    # --- 下面是每个动作的具体“翻译”实现 ---

    def _build_generic_event(
        self,
        action_name: str,
        params: dict[str, Any],
        bot_id: str,
        conv_info: ConversationInfo | None = None,
    ) -> Event:
        """一个通用的翻译模板，这个方法会根据动作名称和参数来构建一个标准的Event对象.

        Args:
            action_name (str): 动作名称，标识要执行的具体动作.
            params (dict[str, Any]): 动作参数，包含执行这个动作所需的所有信息.
            conv_info (ConversationInfo | None): 可选的会话信息，如果有的话.
            bot_id (str): 处理此动作的机器人的唯一标识符.

        Returns:
            Event: 返回一个Event对象，表示这个动作的执行结果.
        """
        final_event_type = f"action.{self.platform_id}.{action_name}"
        action_seg = Seg(type="action_params", data=params)
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=bot_id,
            content=[action_seg],
            conversation_info=conv_info,
        )

    def _build_send_message(self, params: dict[str, Any], bot_id: str) -> Event | None:
        """这个发消息的比较特殊，内容是消息段列表，而不是 action_params.

        Args:
            params (dict[str, Any]): 包含发送消息所需的所有参数.
            bot_id (str): 机器人ID.

        Returns:
            Event | None: 返回一个Event对象，表示发送消息的动作.
        """
        conversation_id = params.get("conversation_id")
        content_segs_data = params.get("content", [])

        if not conversation_id or not isinstance(content_segs_data, list):
            logger.warning("发消息缺少 conversation_id 或 content。")
            return None

        # 它的 content 就是消息本身，而不是 action_params
        message_segs = [
            Seg(type=seg.get("type"), data=seg.get("data", {})) for seg in content_segs_data
        ]
        conv_type = params.get("conversation_type", "group")

        return Event(
            event_id=str(uuid.uuid4()),
            event_type=f"action.{self.platform_id}.send_message",
            time=int(time.time() * 1000),
            bot_id=bot_id,
            content=message_segs,
            conversation_info=ConversationInfo(
                conversation_id=str(conversation_id), type=conv_type
            ),
        )

    def _build_send_forward_message(self, params: dict[str, Any], bot_id: str) -> Event | None:
        """这个合并转发消息的处理也比较特殊，它需要一个节点列表和会话信息.

        Args:
            params (dict[str, Any]): 包含转发消息所需的所有参数.
            bot_id (str): 机器人ID.

        Returns:
            Event | None: 返回一个Event对象，表示转发消息的动作.
        """
        nodes = params.get("nodes", [])
        conv_info_dict = params.get("conversation_info", {})
        if not nodes or not conv_info_dict:
            logger.warning(
                f"构建合并转发消息失败：缺少 'nodes' 或 'conversation_info'。收到的参数: {params}"
            )
            return None

        node_segs = [Seg(type="node", data=node_data) for node_data in nodes]

        return Event(
            event_id=str(uuid.uuid4()),
            event_type=f"action.{self.platform_id}.send_forward_message",
            time=int(time.time() * 1000),
            bot_id=bot_id,
            content=node_segs,
            conversation_info=ConversationInfo.from_dict(conv_info_dict),
        )

    def get_level_consciousness_controls_definitions(
        self, level: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """QQ平台不提供任何意识控制，由CoreBuilder统一管理."""
        empty_schema = {"type": "object", "properties": {}}
        return empty_schema, empty_schema

    def get_level_consciousness_controls_descriptions(self, level: str) -> str:
        """QQ平台不提供任何意识控制的描述."""
        return ""

    def get_level_actions_definitions(self, level: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """根据层级，提供QQ平台专属动作的JSON Schema."""
        # 平台层级的Schema，ID是必须的
        platform_delete_friend_schema = {
            "type": "object",
            "description": "【谨慎使用】删除一个好友。",
            "properties": {
                "user_id": {"type": "string", "description": "要删除的好友的QQ号。"},
                "motivation": {"type": "string"},
            },
            "required": ["user_id", "motivation"],
        }
        platform_leave_conversation_schema = {
            "type": "object",
            "description": "【谨慎使用】退出一个群聊（将你自己从某个群聊移出）。",
            "properties": {
                "group_id": {"type": "string", "description": "要退出的群的群号。"},
                "motivation": {"type": "string"},
            },
            "required": ["group_id", "motivation"],
        }

        # 会话层级的Schema，ID是可选的
        cellular_delete_friend_schema = {
            "type": "object",
            "description": "【谨慎使用】删除一个好友。如果当前就在与该好友的私聊中，可以不提供user_id。",
            "properties": {
                "user_id": {"type": "string", "description": "（可选）要删除的好友的ID（原始QQ号或实体UID）。"},
                "motivation": {"type": "string"},
            },
            "required": ["motivation"],
        }
        cellular_leave_conversation_schema = {
            "type": "object",
            "description": "【谨慎使用】退出一个群聊（将你自己从某个群聊移出）。如果当前就在该群聊中，可以不提供group_id。",
            "properties": {
                "group_id": {"type": "string", "description": "（可选）要退出的群的群号。"},
                "motivation": {"type": "string"},
            },
            "required": ["motivation"],
        }

        handle_friend_request_schema = {
            "type": "object",
            "description": "处理一个好友请求。你可以选择同意、拒绝或忽略。",
            "properties": {
                "user_id": {"type": "string", "description": "请求者的QQ号。"},
                "flag": {
                    "type": "string",
                    "description": "从 <friend_request> 块中获取到的请求 flag 标识。",
                },
                "approve": {
                    "type": "boolean",
                    "description": "是否同意请求。True为同意，False为拒绝。",
                },
                "remark": {
                    "type": "string",
                    "description": "（可选）同意好友请求后，为对方设置的备注。",
                },
                "motivation": {"type": "string"},
            },
            "required": ["user_id", "flag", "approve", "motivation"],
        }

        level_to_props_map = {
            "platform": {
                "get_list": {
                    "type": "object",
                    "properties": {
                        "list_type": {"type": "string", "enum": ["friend", "group"]},
                        "motivation": {"type": "string"},
                    },
                    "required": ["list_type", "motivation"],
                },
                "scroll": {
                    "type": "object",
                    "description": "像使用鼠标滚轮一样，向上或向下翻阅当前看到的会话列表。",
                    "properties": {
                        "params": {
                            "type": "string",
                            "enum": ["up", "down"],
                            "description": "向上或向下滚动。",
                        },
                        "motivation": {"type": "string"},
                    },
                    "required": ["params", "motivation"],
                },
                "delete_friend": platform_delete_friend_schema,
                "leave_conversation": platform_leave_conversation_schema,
                "handle_friend_request": handle_friend_request_schema,
            },
            "cellular": {
                "send_message": {
                    "type": "object",
                    "properties": {
                        "steps": {
                            "type": "array",
                            "description": "构建消息的指令序列。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "command": {
                                        "type": "string",
                                        "description": "要执行的指令名称。",
                                        "enum": ["reply", "at", "text", "send_and_break"],
                                    },
                                    "params": {
                                        "type": "object",
                                        "description": "与指令对应的参数包。根据'command'的值，只填写其中对应的字段。",
                                        "properties": {
                                            # 'reply' command 用的字段
                                            "message_id": {
                                                "type": "string",
                                                "description": "要引用/回复的消息ID。",
                                            },
                                            # 'at' command 用的字段
                                            "user_id": {
                                                "type": "string",
                                                "description": "要@的用户的ID。",
                                            },
                                            # 'text' command 用的字段
                                            "content": {
                                                "type": "string",
                                                "description": "要发送的文本内容。",
                                            },
                                        },
                                    },
                                },
                                "required": ["command", "params"],
                            },
                        },
                        "motivation": {"type": "string"},
                    },
                    "required": ["steps", "motivation"],
                },
                "poke_user": {
                    "type": "object",
                    "properties": {
                        "target_user_id": {"type": "string"},
                        "motivation": {"type": "string"},
                    },
                    "required": ["target_user_id", "motivation"],
                },
                "get_list": {
                    "type": "object",
                    "properties": {
                        "list_type": {"type": "string", "enum": ["friend", "group"]},
                        "motivation": {"type": "string"},
                    },
                    "required": ["list_type", "motivation"],
                },
                "delete_friend": cellular_delete_friend_schema,
                "leave_conversation": cellular_leave_conversation_schema,
                "handle_friend_request": handle_friend_request_schema,
            },
        }

        props = level_to_props_map.get(level, {})
        schema = {"type": "object", "properties": props}
        return schema, {}

    def get_level_actions_descriptions(self, level: str) -> str:
        """根据层级，提供QQ平台专属动作的自然语言描述."""
        level_to_descs_map = {
            "platform": [
                "    - `get_list(type, motivation)`: 直接获取完整的本平台的好友或群聊列表。",
                "    - `scroll(params, motivation)`: 向上('up')或向下('down')翻阅会话列表。",
                "    - `delete_friend(user_id, motivation)`: 【谨慎使用】删除指定ID的好友。",
                "    - `leave_conversation(group_id, motivation)`: 【谨慎使用】退出指定ID的群聊（将你自己从某个群聊移出）。",
                "    - `handle_friend_request(user_id, flag, approve, remark, motivation)`: 处理好友请求。",
            ],
            "cellular": [
                "    - `send_message`: 在当前会话中发送消息。",
                "    - `poke_user`: 在当前会话中戳一戳某人。",
                "    - `get_list`: 获取本平台的好友或群聊列表。",
                "    - `delete_friend(user_id, motivation)`: 【谨慎使用】删除指定ID的好友（如果想删除的好友就是对方，可省略user_id）。",
                "    - `leave_conversation(group_id, motivation)`: 【谨慎使用】退出一个群聊（将你自己从某个群聊移出）。如果当前就在该群聊中，可以不提供group_id。",
                "    - `handle_friend_request(user_id, flag, approve, remark, motivation)`: 处理好友请求。",
            ],
        }

        descs = level_to_descs_map.get(level, [])
        return "\n".join(descs) or "你当前没有可用的动作。"
