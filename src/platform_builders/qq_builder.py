# src/platform_builders/qq_builder.py
import time
import uuid
from typing import Any

from aicarus_protocols import ConversationInfo, Event, Seg
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.platform_builders.base_builder import BasePlatformBuilder

logger = get_logger(__name__)


class QQBuilder(BasePlatformBuilder):
    """QQ平台的构建器，负责将平台特有的动作翻译成标准Event格式.

    这个构建器是为QQ平台定制的，提供了所有QQ特有动作的翻译方法。

    Attributes:
        platform_id (str): 平台ID，唯一标识一个平台。
        这个ID必须和Adapter的core_platform_id完全一致，以确保适配器能够正确识别。
    """

    def __init__(self) -> None:
        super().__init__()
        # 把所有可以用通用模板处理的动作都放在这里。
        # 这样可以避免每次都要写一大堆 if-else 来判断动作类型。
        # 这些动作都可以直接用通用模板来处理，不需要特殊逻辑。
        self._generic_actions = {
            "recall_message",
            "poke_user",
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
        }

    @property
    def platform_id(self) -> str:
        """返回平台ID，唯一标识一个平台，这个ID必须和Adapter的core_platform_id完全一致."""
        return "napcat_qq"

    def build_action_event(self, action_name: str, params: dict[str, Any]) -> Event | None:
        """这个方法负责将平台特有的动作转换成标准的Event格式，它会根据动作名称和参数来决定如何构建Event对象.

        Args:
            action_name (str): 动作名称，标识要执行的具体动作.
            params (dict[str, Any]): 动作参数，包含执行这个动作所需的所有信息.

        Returns:
            Event | None: 返回一个Event对象，表示这个动作的执行结果.
            如果这个动作不在白名单或特殊处理列表中，则返回 None.

        这个方法的核心逻辑是：
        1. 先处理那些需要特殊处理的动作，比如发消息和合并转发.
        2. 然后，检查这个动作是不是在白名单里.
            如果在，就用通用模板来处理，并返回一个Event对象.
        3. 如果哪个都不沾，那就真的不认识了，返回 None.
        """
        # 1. 先处理那些需要特殊处理的动作
        if action_name == "send_message":
            return self._build_send_message(params)
        if action_name == "send_forward_message":
            return self._build_send_forward_message(params)

        # 2. 然后，检查这个动作是不是在白名单里
        if action_name in self._generic_actions:
            # 如果在白名单里，就用通用模板来处理
            # 有些动作需要特别关照一下，把 conversation_info 塞进去
            conv_info = None
            if action_name in ["get_group_info", "get_history", "forward_single_message"]:
                conv_id = params.get("group_id") or params.get("conversation_id")
                conv_info_dict = params.get("conversation_info")
                if conv_info_dict:
                    conv_info = ConversationInfo.from_dict(conv_info_dict)
                elif conv_id:
                    # 这是一个简化处理，实际中最好从params里拿到完整的conv_info
                    conv_info = ConversationInfo(conversation_id=str(conv_id), type="group")
            elif action_name == "poke_user":
                conv_id = params.get("target_group_id")
                if conv_id:
                    conv_info = ConversationInfo(conversation_id=str(conv_id), type="group")

            return self._build_generic_event(action_name, params, conv_info)

        # 3. 如果哪个都不沾，那就真的不认识了
        logger.warning(f"QQBuilder 的白名单和特殊名单里都没有这个动作: {action_name}")
        return None

    # --- 下面是每个动作的具体“翻译”实现 ---

    def _build_generic_event(
        self, action_name: str, params: dict[str, Any], conv_info: ConversationInfo | None = None
    ) -> Event:
        """一个通用的翻译模板，这个方法会根据动作名称和参数来构建一个标准的Event对象.

        Args:
            action_name (str): 动作名称，标识要执行的具体动作.
            params (dict[str, Any]): 动作参数，包含执行这个动作所需的所有信息.
            conv_info (ConversationInfo | None): 可选的会话信息，如果有的话.

        Returns:
            Event: 返回一个Event对象，表示这个动作的执行结果.
        """
        final_event_type = f"action.{self.platform_id}.{action_name}"
        action_seg = Seg(type="action_params", data=params)
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[action_seg],
            conversation_info=conv_info,
        )

    def _build_send_message(self, params: dict[str, Any]) -> Event | None:
        """这个发消息的比较特殊，内容是消息段列表，而不是 action_params.

        Args:
            params (dict[str, Any]): 包含发送消息所需的所有参数.

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
            bot_id=config.persona.qq_id or "unknown_bot",
            content=message_segs,
            conversation_info=ConversationInfo(
                conversation_id=str(conversation_id), type=conv_type
            ),
        )

    def _build_send_forward_message(self, params: dict[str, Any]) -> Event | None:
        """这个合并转发消息的处理也比较特殊，它需要一个节点列表和会话信息.

        Args:
            params (dict[str, Any]): 包含转发消息所需的所有参数.

        Returns:
            Event | None: 返回一个Event对象，表示转发消息的动作.
        """
        nodes = params.get("nodes", [])
        conv_info_dict = params.get("conversation_info", {})
        if not nodes or not conv_info_dict:
            return None

        node_segs = [Seg(type="node", data=node_data) for node_data in nodes]

        return Event(
            event_id=str(uuid.uuid4()),
            event_type=f"action.{self.platform_id}.send_forward_message",
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=node_segs,
            conversation_info=ConversationInfo.from_dict(conv_info_dict),
        )


    def get_level_specific_definitions(self, level: str) -> tuple[dict, dict]:
        controls_properties = {}
        actions_properties = {}

        # --- 中层可用 (Platform Level) ---
        if level == "platform":
            controls_properties.update({
                "focus": {
                    "type": "object",
                    "properties": {
                        "conversation_id": {"type": "string"},
                        "motivation": {"type": "string"}
                    },"required": ["conversation_id", "motivation"]
                },
                "return": {
                    "type": "object",
                    "properties": {
                        "motivation": {"type": "string"}
                    },"required": ["motivation"]
                }
            })
            actions_properties.update({
                "get_list": {
                    "type": "object",
                    "properties": {
                        "list_type": {"type": "string", "enum": ["friend", "group"]},
                        "motivation": {"type": "string"}
                    }, "required": ["list_type","motivation"]
                }
            })
        # --- 底层可用 (Cellular Level) ---
        elif level == "cellular":
            controls_properties.update({
                "shift": {
                    "type": "object",
                    "properties": {
                        "conversation_id": {"type": "string"},
                        "motivation": {"type": "string"}
                    },"required": ["conversation_id", "motivation"]
                },
                "return": {
                    "type": "object",
                    "properties": {
                        "motivation": {"type": "string"}
                    },"required": ["motivation"]
                }
            })
            # 底层的动作定义
            # 为了方便测试，目前只定义了 send_message 和 poke_user 两个动作。
            # 其他动作可以在需要时添加
            actions_properties.update({
                "send_message": {
                    "type": "object",
                    "properties": {
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "command": {"type": "string", "enum": ["text", "at", "reply", "send_and_break"]},
                                    "params": {"type": "object"}
                                },
                                "required": ["command"]
                            }
                        },
                        "motivation": {"type": "string"}
                    },
                    "required": ["steps", "motivation"]
                },
                "poke_user": {
                    "type": "object",
                    "properties": {
                        "target_user_id": {"type": "string"}
                    },"required": ["target_user_id"]
                }
            })
        # --- 所有层级通用 ---
        # web_search 可以在任何层级使用
        actions_properties.update({
            "web_search": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "motivation": {"type": "string"}
                },"required": ["query", "motivation"]
            },
            "do_nothing": {
                "type": "object",
                "description": "当你决定不采取任何外部行动，只想在内心默默思考时，选择此项。",
                "properties": {
                    "motivation": {"type": "string", "description": "你决定保持沉默的内心想法或原因。"}
                },
                    "required": ["motivation"]
            }
        })

        final_controls_schema = {"type": "object", "properties": controls_properties, "maxProperties": 1} if controls_properties else {}
        final_actions_schema = {"type": "object", "properties": actions_properties} if actions_properties else {}

        return final_controls_schema, final_actions_schema

    def get_level_specific_descriptions(self, level: str) -> tuple[str, str]:
        controls_desc_parts = []
        actions_desc_parts = []

        if level == "platform":
            controls_desc_parts.append("- `focus`: 深入到本平台下一个具体的会话。")
            controls_desc_parts.append("- `return`: 返回到顶层(Core)，不再关注QQ平台。")
            actions_desc_parts.append("- `get_list`: 获取本平台的好友或群聊列表。")

        elif level == "cellular":
            controls_desc_parts.append("- `shift`: 将注意力从当前会话转移到同平台的另一个会话。")
            controls_desc_parts.append("- `return`: 返回到QQ平台概览层级。")
            actions_desc_parts.append("- `send_message`: 在当前会话中发送消息。")
            actions_desc_parts.append("- `poke_user`: 在当前会话中戳一戳某人。")
            # ... 其他底层可用动作的描述 ...

        actions_desc_parts.append("- `web_search`: 进行一次网络搜索。")

        return "\n".join(controls_desc_parts) or "无", "\n".join(actions_desc_parts) or "无"

    # 这下面动作定义不再直接使用了
    # 现在所有动作定义都在 get_external_actions() 方法里
    # 下面这些是我们理论上可以用到的，且支持的动作定义
    def get_external_actions(self) -> dict[str, Any]:
        """获取当前平台的所有动作定义.

        返回一个字典，包含所有平台特有的动作定义。
        每个动作定义包含类型、描述和属性等信息。
        Returns:
            dict[str, Any]: 包含当前平台动作定义的字典。
            键是动作名称，值是该动作的定义。
        """
        return {
            "send_message": {
                "type": "object",
                "description": "向指定的QQ群或好友发送一条或多条消息。",
                "properties": {
                    "conversation_type": {
                        "type": "string",
                        "enum": ["group", "private"],
                        "description": "会话类型。",
                    },
                    "content": {
                        "type": "array",
                        "description": "一个消息段(Segment)列表，定义了要发送的内容。",
                        "items": {"type": "object"},
                    },
                },
                "required": ["conversation_id", "content"],
            },
            "send_forward_message": {
                "type": "object",
                "description": "发送合并转发消息。",
                "properties": {
                    "conversation_info": {"type": "object", "description": "目标会话信息。"},
                    "nodes": {
                        "type": "array",
                        "description": "要转发的消息节点列表。",
                        "items": {"type": "object"},
                    },
                },
                "required": ["conversation_info", "nodes"],
            },
            "recall_message": {
                "type": "object",
                "description": "撤回一条已发送的消息。",
                "properties": {
                    "target_message_id": {"type": "string", "description": "要撤回的消息的ID。"}
                },
                "required": ["target_message_id"],
            },
            "poke_user": {
                "type": "object",
                "description": "在指定的QQ群里戳一个成员，或者私聊戳好友。",
                "properties": {
                    "target_user_id": {"type": "string", "description": "要戳的用户的QQ号。"},
                    "target_group_id": {
                        "type": "string",
                        "description": "（可选）如果在群里戳，这里是群号。",
                    },
                },
                "required": ["target_user_id"],
            },
            "kick_member": {
                "type": "object",
                "description": "从群聊中踢出成员。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "user_id": {"type": "string", "description": "要踢出的成员QQ号。"},
                    "reject_add_request": {
                        "type": "boolean",
                        "description": "是否拒绝该用户后续加群请求。",
                    },
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
                    "is_dismiss": {
                        "type": "boolean",
                        "description": "是否解散群聊（仅群主可用）。",
                    },
                },
                "required": ["group_id"],
            },
            "handle_friend_request": {
                "type": "object",
                "description": "处理好友添加请求。",
                "properties": {
                    "request_flag": {
                        "type": "string",
                        "description": "从请求事件中获取的唯一标识。",
                    },
                    "approve": {"type": "boolean", "description": "是否同意请求。"},
                    "remark": {"type": "string", "description": "（可选）同意后的备注名。"},
                },
                "required": ["request_flag", "approve"],
            },
            "handle_group_request": {
                "type": "object",
                "description": "处理加群请求或邀请。",
                "properties": {
                    "request_flag": {
                        "type": "string",
                        "description": "从请求事件中获取的唯一标识。",
                    },
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
                    "group_id": {
                        "type": "string",
                        "description": "（可选）如果提供，则只获取指定群聊中的档案。",
                    }
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
                "properties": {
                    "file": {"type": "string", "description": "图片文件路径或URL或Base64。"}
                },
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
            "get_list": {
                "type": "object",
                "description": "获取机器人加入的群聊或好友列表。",
                "properties": {
                    "list_type": {
                        "type": "string",
                        "enum": ["group", "friend"],
                        "description": "要获取的列表类型。",
                    }
                },
                "required": ["list_type"],
            },
            "forward_single_message": {
                "type": "object",
                "description": "转发单条消息。",
                "properties": {
                    "conversation_info": {"type": "object", "description": "目标会话信息。"},
                    "message_id": {"type": "string", "description": "要转发的消息ID。"},
                },
                "required": ["conversation_info", "message_id"],
            },
            "set_admin": {
                "type": "object",
                "description": "设置或取消群管理员。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "user_id": {"type": "string", "description": "目标成员QQ号。"},
                    "enable": {"type": "boolean", "description": "True为设置，False为取消。"},
                },
                "required": ["group_id", "user_id", "enable"],
            },
            "set_conversation_name": {
                "type": "object",
                "description": "修改群聊名称。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "new_name": {"type": "string", "description": "新的群聊名称。"},
                },
                "required": ["group_id", "new_name"],
            },
            "get_group_files": {
                "type": "object",
                "description": "获取群文件列表。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "folder_id": {
                        "type": "string",
                        "description": "（可选）文件夹ID，不填则为根目录。",
                    },
                },
                "required": ["group_id"],
            },
            "upload_group_file": {
                "type": "object",
                "description": "上传群文件。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "file_path": {"type": "string", "description": "本地文件路径。"},
                    "file_name": {"type": "string", "description": "上传后的文件名。"},
                    "folder_id": {"type": "string", "description": "（可选）目标文件夹ID。"},
                },
                "required": ["group_id", "file_path", "file_name"],
            },
            "delete_group_item": {
                "type": "object",
                "description": "删除群文件或文件夹。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "item_type": {
                        "type": "string",
                        "enum": ["file", "folder"],
                        "description": "要删除的项目类型。",
                    },
                    "file_id": {"type": "string", "description": "（如果删除文件）文件ID。"},
                    "busid": {"type": "integer", "description": "（如果删除文件）busid。"},
                    "folder_id": {"type": "string", "description": "（如果删除文件夹）文件夹ID。"},
                },
                "required": ["group_id", "item_type"],
            },
            "create_group_folder": {
                "type": "object",
                "description": "创建群文件夹。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "name": {"type": "string", "description": "新文件夹的名称。"},
                },
                "required": ["group_id", "name"],
            },
            "get_group_file_url": {
                "type": "object",
                "description": "获取群文件下载链接。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "file_id": {"type": "string", "description": "文件ID。"},
                    "busid": {"type": "integer", "description": "busid。"},
                },
                "required": ["group_id", "file_id", "busid"],
            },
            "get_group_honor_info": {
                "type": "object",
                "description": "获取群荣誉信息。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "type": {
                        "type": "string",
                        "description": "荣誉类型（如 'talkative', 'performer' 等）。",
                    },
                },
                "required": ["group_id", "type"],
            },
            "send_group_notice": {
                "type": "object",
                "description": "发送群公告。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "content": {"type": "string", "description": "公告内容。"},
                    "image": {"type": "string", "description": "（可选）公告附带的图片。"},
                },
                "required": ["group_id", "content"],
            },
            "get_group_notice": {
                "type": "object",
                "description": "获取群公告列表。",
                "properties": {"group_id": {"type": "string", "description": "目标群号。"}},
                "required": ["group_id"],
            },
            "set_message_emoji_like": {
                "type": "object",
                "description": "使用表情对消息进行回应（点赞）。",
                "properties": {
                    "message_id": {"type": "string", "description": "目标消息ID。"},
                    "emoji_id": {"type": "string", "description": "表情ID。"},
                },
                "required": ["message_id", "emoji_id"],
            },
            "get_recent_contacts": {
                "type": "object",
                "description": "获取最近联系人列表。",
                "properties": {"count": {"type": "integer", "description": "获取的数量。"}},
            },
            "get_ai_characters": {
                "type": "object",
                "description": "获取可用的AI语音角色列表。",
                "properties": {"group_id": {"type": "string", "description": "目标群号。"}},
                "required": ["group_id"],
            },
            "send_ai_voice": {
                "type": "object",
                "description": "使用AI语音发送消息。",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群号。"},
                    "character_id": {"type": "string", "description": "AI角色ID。"},
                    "text": {"type": "string", "description": "要转换为语音的文本。"},
                },
                "required": ["group_id", "character_id", "text"],
            },
        }
