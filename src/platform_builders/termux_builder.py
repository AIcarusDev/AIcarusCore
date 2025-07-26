# AIcarusCore/src/platform_builders/termux_builder.py

import time
import uuid

from aicarus_protocols import Event, Seg
from src.common.custom_logging.logging_config import get_logger
from src.platform_builders.base_builder import BasePlatformBuilder

logger = get_logger(__name__)


class TermuxBuilder(BasePlatformBuilder):
    """Termux平台的构建器，负责向Core注册TermuxAdapter的能力.

    它就像是TermuxAdapter在Core那边的“外交大使”.
    """

    @property
    def needs_on_connect_inspection(self) -> bool:
        """Termux平台不需要复杂的连接安检."""
        return False

    @property
    def is_person_platform(self) -> bool:
        """Termux平台不具有身份内容, 返回 False."""
        return False

    @property
    def platform_id(self) -> str:
        """Termux平台的唯一标识符.

        这个ID必须和TermuxAdapter的config.toml里的一模一样，
        否则Core无法正确识别和调用Termux的功能.
        """
        return "termux"

    def get_level_consciousness_controls_definitions(self, level: str) -> tuple[dict, dict]:
        """Termux平台本身不提供任何意识控制（如focus, return）.

        这些是Core的通用能力，所以我们这里返回空字典.
        """
        return {}, {}

    def get_level_consciousness_controls_descriptions(self, level: str) -> str:
        """Termux平台没有专属的意识控制描述."""
        return ""

    def build_action_event(self, action_name: str, params: dict, bot_id: str) -> Event | None:
        """这个方法负责将Core的指令，转换成发往Adapter的标准Event."""
        # 我们的Termux动作都很标准，可以直接用一个通用模板来构建
        final_event_type = f"action.{self.platform_id}.{action_name}"
        action_seg = Seg(type="action_params", data=params)

        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=bot_id,  # bot_id 在这里通常就是 platform_id
            content=[action_seg],
        )

    def get_level_actions_definitions(self, level: str) -> tuple[dict, dict]:
        """核心！在这里向Core的大脑注册我们所有的技能!

        LLM会读取这个Schema，来学习如何正确地调用我们的功能.
        """
        props = {}

        # 我们的Termux技能在任何层级（core, platform, cellular）都应该可用
        # 所以我们不需要判断 level

        # 技能一：发送通知
        props["notification"] = {
            "type": "object",
            "description": "在手机上发送一条系统通知。",
            "properties": {
                "title": {"type": "string", "description": "通知的标题。"},
                "content": {"type": "string", "description": "通知的正文内容。"},
                "motivation": {"type": "string", "description": "你为什么要发这条通知？"},
            },
            "required": ["title", "content", "motivation"],
        }

        # 技能二：语音播报
        props["tts_speak"] = {
            "type": "object",
            "description": "使用手机的TTS引擎朗读一段文字。",
            "properties": {
                "text": {"type": "string", "description": "要朗读的文本。"},
                "motivation": {"type": "string", "description": "你为什么要朗读这段话？"},
            },
            "required": ["text", "motivation"],
        }

        # 技能三：手机振动
        props["vibrate"] = {
            "type": "object",
            "description": "让手机振动一下。",
            "properties": {
                "duration_ms": {
                    "type": "integer",
                    "description": "振动的持续时间（毫秒），默认200。",
                },
                "motivation": {"type": "string", "description": "你为什么要让手机振动？"},
            },
            "required": ["motivation"],  # 只有动机是必需的
        }

        # 技能四：弹出Toast
        props["toast"] = {
            "type": "object",
            "description": "在手机屏幕底部弹出一个短暂的提示消息。",
            "properties": {
                "text": {"type": "string", "description": "要显示的提示文本。"},
                "short": {"type": "boolean", "description": "是否使用短时显示，默认True。"},
                "motivation": {"type": "string", "description": "你为什么要弹出这个提示？"},
            },
            "required": ["text", "motivation"],
        }

        # 以后有新技能，就在这里继续添加 props["新技能名"] = { ... }

        schema = {"type": "object", "properties": props} if props else {}
        return schema, {}

    def get_level_actions_descriptions(self, level: str) -> str:
        """这里是用自然语言向LLM描述我们的技能，让它更容易理解."""
        descs = [
            "    - `notification(title, content, motivation)`: 在手机上发送一条通知。",
            "    - `tts_speak(text, motivation)`: 使用手机TTS朗读文字。",
            "    - `vibrate(duration_ms, motivation)`: 让手机振动。",
            "    - `toast(text, short, motivation)`: 在屏幕底部弹出提示。",
        ]

        # 同样，这些技能在所有层级都可用
        return "\n".join(descs)
