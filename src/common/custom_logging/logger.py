# logger.py

import contextlib
import os
import sys
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType

from dotenv import load_dotenv
from loguru import logger

# 我会记住上一次被你进行全身服务是在哪一天...
_LAST_HOUSEKEEPING_DATE: date | None = None

# 加载 .env 文件
env_path = Path(os.getcwd()) / ".env"
load_dotenv(dotenv_path=env_path)

# logger 显示昵称
# 从环境变量 BOT_LOG_NICKNAME 读取，如果未设置或为空，则默认为 "bot"
bot_nickname_from_env = os.getenv("BOT_LOG_NICKNAME", "bot").strip()
if not bot_nickname_from_env:
    bot_nickname = "bot"
    print("BOT_LOG_NICKNAME 环境变量为空或未设置，日志昵称将使用默认值: bot")  # 可以替换为早期日志记录
else:
    bot_nickname = bot_nickname_from_env
    print(f"日志昵称已从环境变量配置为: {bot_nickname}")  # 可以替换为早期日志记录

# 保存原生处理器ID
default_handler_id = None
for handler_id in logger._core.handlers:
    default_handler_id = handler_id
    break

# 移除默认处理器
if default_handler_id is not None:
    logger.remove(default_handler_id)

# 类型别名
LoguruLogger = logger.__class__

# 全局注册表：记录模块与处理器ID的映射
_handler_registry: dict[str, list[int]] = {}
_custom_style_handlers: dict[tuple[str, str], list[int]] = {}  # 记录自定义样式处理器ID

# 获取日志存储根地址
ROOT_PATH = os.getcwd()
LOG_ROOT = str(ROOT_PATH) + "/" + "logs"

SIMPLE_OUTPUT = os.getenv("SIMPLE_OUTPUT", "false").strip().lower()
SIMPLE_OUTPUT = SIMPLE_OUTPUT == "true"
print(f"SIMPLE_OUTPUT: {SIMPLE_OUTPUT}")

if not SIMPLE_OUTPUT:
    # 默认全局配置
    DEFAULT_CONFIG = {
        # 日志级别配置
        "console_level": "INFO",
        "file_level": "DEBUG",
        # 格式配置
        "console_format": (
            "<level>{time:YYYY-MM-DD HH:mm:ss}</level> | <cyan>{extra[module]: <12}</cyan> | <level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | {message}",
        "log_dir": LOG_ROOT,
        "rotation": "00:00",
        "retention": "3 days",
        "compression": "zip",
    }
else:
    DEFAULT_CONFIG = {
        # 日志级别配置
        "console_level": "INFO",
        "file_level": "DEBUG",
        # 格式配置
        "console_format": "<level>{time:HH:mm:ss}</level> | <cyan>{extra[module]}</cyan> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | {message}",
        "log_dir": LOG_ROOT,
        "rotation": "00:00",
        "retention": "3 days",
        "compression": "zip",
    }


MAIN_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-yellow>主程序</light-yellow> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 主程序 | {message}",
    },
    "simple": {
        "console_format": (
            "<level>{time:HH:mm:ss}</level> | <light-yellow>主程序</light-yellow> | <light-yellow>{message}</light-yellow>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 主程序 | {message}",
    },
}

# pfc配置
PFC_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-yellow>PFC</light-yellow> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | PFC | {message}",
    },
    "simple": {
        "console_format": (
            "<level>{time:HH:mm:ss}</level> | <light-green>PFC</light-green> | <light-green>{message}</light-green>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | PFC | {message}",
    },
}

# MOOD
MOOD_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<magenta>心情</magenta> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 心情 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <magenta>心情 | {message} </magenta>",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 心情 | {message}",
    },
}
# tool use
TOOL_USE_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<magenta>工具使用</magenta> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 工具使用 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <magenta>工具使用</magenta> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 工具使用 | {message}",
    },
}


# relationship
RELATION_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-magenta>关系</light-magenta> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 关系 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-magenta>关系</light-magenta> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 关系 | {message}",
    },
}

# config
CONFIG_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-cyan>配置</light-cyan> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 配置 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-cyan>配置</light-cyan> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 配置 | {message}",
    },
}

SENDER_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-yellow>消息发送</light-yellow> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 消息发送 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <green>消息发送</green> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 消息发送 | {message}",
    },
}

HEARTFLOW_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            f"<white>{{time:YYYY-MM-DD HH:mm:ss}}</white> | "
            f"<level>{{level: <8}}</level> | "
            f"<light-yellow>{bot_nickname}大脑袋</light-yellow> | "
            f"<level>{{message}}</level>"
        ),
        "file_format": f"{{time:YYYY-MM-DD HH:mm:ss}} | {{level: <8}} | {{extra[module]: <15}} | {bot_nickname}大脑袋 | {{message}}",
    },
    "simple": {
        "console_format": (
            f"<level>{{time:HH:mm:ss}}</level> | <light-green>{bot_nickname}大脑袋</light-green> | <light-green>{{message}}</light-green>"
        ),  # noqa: E501
        "file_format": f"{{time:YYYY-MM-DD HH:mm:ss}} | {{level: <8}} | {{extra[module]: <15}} | {bot_nickname}大脑袋 | {{message}}",
    },
}

SCHEDULE_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-yellow>在干嘛</light-yellow> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 在干嘛 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <cyan>在干嘛</cyan> | <cyan>{message}</cyan>",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 在干嘛 | {message}",
    },
}

LLM_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            f"<white>{{time:YYYY-MM-DD HH:mm:ss}}</white> | "
            f"<level>{{level: <8}}</level> | "
            f"<light-yellow>{bot_nickname}组织语言</light-yellow> | "
            f"<level>{{message}}</level>"
        ),
        "file_format": f"{{time:YYYY-MM-DD HH:mm:ss}} | {{level: <8}} | {{extra[module]: <15}} | {bot_nickname}组织语言 | {{message}}",
    },
    "simple": {
        "console_format": f"<level>{{time:HH:mm:ss}}</level> | <light-green>{bot_nickname}组织语言</light-green> | {{message}}",
        "file_format": f"{{time:YYYY-MM-DD HH:mm:ss}} | {{level: <8}} | {{extra[module]: <15}} | {bot_nickname}组织语言 | {{message}}",
    },
}


# Topic日志样式配置
TOPIC_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-blue>话题</light-blue> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 话题 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-blue>主题</light-blue> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 话题 | {message}",
    },
}

# Topic日志样式配置
CHAT_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<green>见闻</green> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 见闻 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <green>见闻</green> | <green>{message}</green>",  # noqa: E501
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 见闻 | {message}",
    },
}

REMOTE_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-yellow>远程</light-yellow> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 远程 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <fg #00788A>远程| {message}</fg #00788A>",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 远程 | {message}",
    },
}

SUB_HEARTFLOW_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            f"<white>{{time:YYYY-MM-DD HH:mm:ss}}</white> | "
            f"<level>{{level: <8}}</level> | "
            f"<light-blue>{bot_nickname}水群</light-blue> | "
            f"<level>{{message}}</level>"
        ),
        "file_format": f"{{time:YYYY-MM-DD HH:mm:ss}} | {{level: <8}} | {{extra[module]: <15}} | {bot_nickname}小脑袋 | {{message}}",
    },
    "simple": {
        "console_format": f"<level>{{time:HH:mm:ss}}</level> | <fg #3399FF>{bot_nickname}水群 | {{message}}</fg #3399FF>",  # noqa: E501
        "file_format": f"{{time:YYYY-MM-DD HH:mm:ss}} | {{level: <8}} | {{extra[module]: <15}} | {bot_nickname}水群 | {{message}}",
    },
}

INTEREST_CHAT_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-blue>兴趣</light-blue> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 兴趣 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <fg #55DDFF>兴趣 | {message}</fg #55DDFF>",  # noqa: E501
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 兴趣 | {message}",
    },
}


SUB_HEARTFLOW_MIND_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            f"<white>{{time:YYYY-MM-DD HH:mm:ss}}</white> | "
            f"<level>{{level: <8}}</level> | "
            f"<light-blue>{bot_nickname}小脑袋</light-blue> | "
            f"<level>{{message}}</level>"
        ),
        "file_format": f"{{time:YYYY-MM-DD HH:mm:ss}} | {{level: <8}} | {{extra[module]: <15}} | {bot_nickname}小脑袋 | {{message}}",
    },
    "simple": {
        "console_format": f"<level>{{time:HH:mm:ss}}</level> | <fg #66CCFF>{bot_nickname}小脑袋 | {{message}}</fg #66CCFF>",  # noqa: E501
        "file_format": f"{{time:YYYY-MM-DD HH:mm:ss}} | {{level: <8}} | {{extra[module]: <15}} | {bot_nickname}小脑袋 | {{message}}",
    },
}

SUBHEARTFLOW_MANAGER_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            f"<white>{{time:YYYY-MM-DD HH:mm:ss}}</white> | "
            f"<level>{{level: <8}}</level> | "
            f"<light-blue>{bot_nickname}水群[管理]</light-blue> | "
            f"<level>{{message}}</level>"
        ),
        "file_format": f"{{time:YYYY-MM-DD HH:mm:ss}} | {{level: <8}} | {{extra[module]: <15}} | {bot_nickname}水群[管理] | {{message}}",
    },
    "simple": {
        "console_format": f"<level>{{time:HH:mm:ss}}</level> | <fg #005BA2>{bot_nickname}水群[管理] | {{message}}</fg #005BA2>",  # noqa: E501
        "file_format": f"{{time:YYYY-MM-DD HH:mm:ss}} | {{level: <8}} | {{extra[module]: <15}} | {bot_nickname}水群[管理] | {{message}}",
    },
}

BASE_TOOL_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-blue>工具使用</light-blue> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 工具使用 | {message}",
    },
    "simple": {
        "console_format": (
            "<level>{time:HH:mm:ss}</level> | <light-blue>工具使用</light-blue> | <light-blue>{message}</light-blue>"
        ),  # noqa: E501
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 工具使用 | {message}",
    },
}

CHAT_STREAM_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-blue>聊天流</light-blue> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 聊天流 | {message}",
    },
    "simple": {
        "console_format": (
            "<level>{time:HH:mm:ss}</level> | <light-blue>聊天流</light-blue> | <light-blue>{message}</light-blue>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 聊天流 | {message}",
    },
}

CHAT_MESSAGE_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-blue>聊天消息</light-blue> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 聊天消息 | {message}",
    },
    "simple": {
        "console_format": (
            "<level>{time:HH:mm:ss}</level> | <light-blue>聊天消息</light-blue> | <light-blue>{message}</light-blue>"
        ),  # noqa: E501
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 聊天消息 | {message}",
    },
}

PERSON_INFO_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-blue>人物信息</light-blue> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 人物信息 | {message}",
    },
    "simple": {
        "console_format": (
            "<level>{time:HH:mm:ss}</level> | <light-blue>人物信息</light-blue> | <light-blue>{message}</light-blue>"
        ),  # noqa: E501
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 人物信息 | {message}",
    },
}

BACKGROUND_TASKS_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-blue>后台任务</light-blue> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 后台任务 | {message}",
    },
    "simple": {
        "console_format": (
            "<level>{time:HH:mm:ss}</level> | <light-blue>后台任务</light-blue> | <light-blue>{message}</light-blue>"
        ),  # noqa: E501
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 后台任务 | {message}",
    },
}

WILLING_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-blue>意愿</light-blue> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 意愿 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-blue>意愿 | {message} </light-blue>",  # noqa: E501
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 意愿 | {message}",
    },
}

PFC_ACTION_PLANNER_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-blue>PFC私聊规划</light-blue> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | PFC私聊规划 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-blue>PFC私聊规划 | {message} </light-blue>",  # noqa: E501
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | PFC私聊规划 | {message}",
    },
}

# EMOJI，橙色，全着色
EMOJI_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<fg #FFD700>表情包</fg #FFD700> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 表情包 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <fg #FFD700>表情包 | {message} </fg #FFD700>",  # noqa: E501
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 表情包 | {message}",
    },
}

MAI_STATE_CONFIG = {
    "advanced": {
        "console_format": (
            f"<white>{{time:YYYY-MM-DD HH:mm:ss}}</white> | "
            f"<level>{{level: <8}}</level> | "
            f"<light-blue>{bot_nickname}状态</light-blue> | "
            f"<level>{{message}}</level>"
        ),
        "file_format": f"{{time:YYYY-MM-DD HH:mm:ss}} | {{level: <8}} | {{extra[module]: <15}} | {bot_nickname}状态 | {{message}}",
    },
    "simple": {
        "console_format": f"<level>{{time:HH:mm:ss}}</level> | <fg #66CCFF>{bot_nickname}状态 | {{message}} </fg #66CCFF>",  # noqa: E501
        "file_format": f"{{time:YYYY-MM-DD HH:mm:ss}} | {{level: <8}} | {{extra[module]: <15}} | {bot_nickname}状态 | {{message}}",
    },
}


# 海马体日志样式配置
MEMORY_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-yellow>海马体</light-yellow> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 海马体 | {message}",
    },
    "simple": {
        "console_format": (
            "<level>{time:HH:mm:ss}</level> | <fg #7CFFE6>海马体</fg #7CFFE6> | <fg #7CFFE6>{message}</fg #7CFFE6>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 海马体 | {message}",
    },
}


# LPMM配置
LPMM_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-yellow>LPMM</light-yellow> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | LPMM | {message}",
    },
    "simple": {
        "console_format": (
            "<level>{time:HH:mm:ss}</level> | <fg #37FFB4>LPMM</fg #37FFB4> | <fg #37FFB4>{message}</fg #37FFB4>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | LPMM | {message}",
    },
}

# OBSERVATION_STYLE_CONFIG = {
#     "advanced": {
#         "console_format": (
#             "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
#             "<level>{level: <8}</level> | "
#             "<light-yellow>聊天观察</light-yellow> | "
#             "<level>{message}</level>"
#         ),
#         "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 聊天观察 | {message}",
#     },
#     "simple": {
#         "console_format": (
#             "<level>{time:HH:mm:ss}</level> | <light-yellow>聊天观察</light-yellow> | <light-yellow>{message}</light-yellow>"
#         ),  # noqa: E501
#         "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 聊天观察 | {message}",
#     },
# }

CHAT_IMAGE_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-yellow>聊天图片</light-yellow> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 聊天图片 | {message}",
    },
    "simple": {
        "console_format": (
            "<level>{time:HH:mm:ss}</level> | <light-yellow>聊天图片</light-yellow> | <light-yellow>{message}</light-yellow>"
        ),  # noqa: E501
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 聊天图片 | {message}",
    },
}

# HFC log
HFC_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-green>专注聊天</light-green> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 专注聊天 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-green>专注聊天 | {message}</light-green>",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 专注聊天 | {message}",
    },
}

OBSERVATION_STYLE_CONFIG = {
    "advanced": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-yellow>观察</light-yellow> | <light-yellow>{message}</light-yellow>",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 观察 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <fg #66CCFF>观察</fg #66CCFF> | <fg #66CCFF>{message}</fg #66CCFF>",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 观察 | {message}",
    },
}

PROCESSOR_STYLE_CONFIG = {
    "advanced": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <fg #54DDFF>处理器</fg #54DDFF> | <fg #54DDFF>{message}</fg #54DDFF>",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 处理器 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <fg #54DDFF>处理器</fg #54DDFF> | <fg #54DDFF>{message}</fg #54DDFF>",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 处理器 | {message}",
    },
}

PLANNER_STYLE_CONFIG = {
    "advanced": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <fg #36DEFF>规划器</fg #36DEFF> | <fg #36DEFF>{message}</fg #36DEFF>",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 规划器 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <fg #36DEFF>规划器</fg #36DEFF> | <fg #36DEFF>{message}</fg #36DEFF>",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 规划器 | {message}",
    },
}

ACTION_TAKEN_STYLE_CONFIG = {
    "advanced": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <fg #22DAFF>动作</fg #22DAFF> | <fg #22DAFF>{message}</fg #22DAFF>",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 动作 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <fg #22DAFF>动作</fg #22DAFF> | <fg #22DAFF>{message}</fg #22DAFF>",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 动作 | {message}",
    },
}


CONFIRM_STYLE_CONFIG = {
    "console_format": "<RED>{message}</RED>",  # noqa: E501
    "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | EULA与PRIVACY确认 | {message}",
}

# 天依蓝配置
TIANYI_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<fg #66CCFF>天依</fg #66CCFF> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 天依 | {message}",
    },
    "simple": {
        "console_format": (
            "<level>{time:HH:mm:ss}</level> | <fg #66CCFF>天依</fg #66CCFF> | <fg #66CCFF>{message}</fg #66CCFF>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 天依 | {message}",
    },
}

# 模型日志样式配置
MODEL_UTILS_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-yellow>模型</light-yellow> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 模型 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-green>模型</light-green> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 模型 | {message}",
    },
}

MESSAGE_BUFFER_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-yellow>消息缓存</light-yellow> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 消息缓存 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-green>消息缓存</light-green> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 消息缓存 | {message}",
    },
}

PROMPT_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-yellow>提示词构建</light-yellow> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 提示词构建 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-green>提示词构建</light-green> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 提示词构建 | {message}",
    },
}

CHANGE_MOOD_TOOL_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<fg #3FC1C9>心情工具</fg #3FC1C9> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 心情工具 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-green>心情工具</light-green> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 心情工具 | {message}",
    },
}

CHANGE_RELATIONSHIP_TOOL_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<fg #3FC1C9>关系工具</fg #3FC1C9> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 关系工具 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-green>关系工具</light-green> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 关系工具 | {message}",
    },
}

GET_KNOWLEDGE_TOOL_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<fg #3FC1C9>获取知识</fg #3FC1C9> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 获取知识 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-green>获取知识</light-green> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 获取知识 | {message}",
    },
}

GET_TIME_DATE_TOOL_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<fg #3FC1C9>获取时间日期</fg #3FC1C9> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 获取时间日期 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-green>获取时间日期</light-green> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 获取时间日期 | {message}",
    },
}

LPMM_GET_KNOWLEDGE_TOOL_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<fg #3FC1C9>LPMM获取知识</fg #3FC1C9> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | LPMM获取知识 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-green>LPMM获取知识</light-green> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | LPMM获取知识 | {message}",
    },
}

INIT_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-yellow>初始化</light-yellow> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 初始化 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-green>初始化</light-green> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | 初始化 | {message}",
    },
}

API_SERVER_STYLE_CONFIG = {
    "advanced": {
        "console_format": (
            "<white>{time:YYYY-MM-DD HH:mm:ss}</white> | "
            "<level>{level: <8}</level> | "
            "<light-yellow>API服务</light-yellow> | "
            "<level>{message}</level>"
        ),
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | API服务 | {message}",
    },
    "simple": {
        "console_format": "<level>{time:HH:mm:ss}</level> | <light-green>API服务</light-green> | {message}",
        "file_format": "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[module]: <15} | API服务 | {message}",
    },
}


# 根据SIMPLE_OUTPUT选择配置
MAIN_STYLE_CONFIG = MAIN_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else MAIN_STYLE_CONFIG["advanced"]
EMOJI_STYLE_CONFIG = EMOJI_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else EMOJI_STYLE_CONFIG["advanced"]
PFC_ACTION_PLANNER_STYLE_CONFIG = (
    PFC_ACTION_PLANNER_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else PFC_ACTION_PLANNER_STYLE_CONFIG["advanced"]
)
REMOTE_STYLE_CONFIG = REMOTE_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else REMOTE_STYLE_CONFIG["advanced"]
BASE_TOOL_STYLE_CONFIG = BASE_TOOL_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else BASE_TOOL_STYLE_CONFIG["advanced"]
PERSON_INFO_STYLE_CONFIG = PERSON_INFO_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else PERSON_INFO_STYLE_CONFIG["advanced"]
SUBHEARTFLOW_MANAGER_STYLE_CONFIG = (
    SUBHEARTFLOW_MANAGER_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else SUBHEARTFLOW_MANAGER_STYLE_CONFIG["advanced"]
)
BACKGROUND_TASKS_STYLE_CONFIG = (
    BACKGROUND_TASKS_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else BACKGROUND_TASKS_STYLE_CONFIG["advanced"]
)
MEMORY_STYLE_CONFIG = MEMORY_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else MEMORY_STYLE_CONFIG["advanced"]
CHAT_STREAM_STYLE_CONFIG = CHAT_STREAM_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else CHAT_STREAM_STYLE_CONFIG["advanced"]
TOPIC_STYLE_CONFIG = TOPIC_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else TOPIC_STYLE_CONFIG["advanced"]
SENDER_STYLE_CONFIG = SENDER_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else SENDER_STYLE_CONFIG["advanced"]
LLM_STYLE_CONFIG = LLM_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else LLM_STYLE_CONFIG["advanced"]
CHAT_STYLE_CONFIG = CHAT_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else CHAT_STYLE_CONFIG["advanced"]
MOOD_STYLE_CONFIG = MOOD_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else MOOD_STYLE_CONFIG["advanced"]
RELATION_STYLE_CONFIG = RELATION_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else RELATION_STYLE_CONFIG["advanced"]
SCHEDULE_STYLE_CONFIG = SCHEDULE_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else SCHEDULE_STYLE_CONFIG["advanced"]
HEARTFLOW_STYLE_CONFIG = HEARTFLOW_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else HEARTFLOW_STYLE_CONFIG["advanced"]
SUB_HEARTFLOW_STYLE_CONFIG = (
    SUB_HEARTFLOW_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else SUB_HEARTFLOW_STYLE_CONFIG["advanced"]
)  # noqa: E501
SUB_HEARTFLOW_MIND_STYLE_CONFIG = (
    SUB_HEARTFLOW_MIND_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else SUB_HEARTFLOW_MIND_STYLE_CONFIG["advanced"]
)
WILLING_STYLE_CONFIG = WILLING_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else WILLING_STYLE_CONFIG["advanced"]
MAI_STATE_CONFIG = MAI_STATE_CONFIG["simple"] if SIMPLE_OUTPUT else MAI_STATE_CONFIG["advanced"]
CONFIG_STYLE_CONFIG = CONFIG_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else CONFIG_STYLE_CONFIG["advanced"]
TOOL_USE_STYLE_CONFIG = TOOL_USE_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else TOOL_USE_STYLE_CONFIG["advanced"]
PFC_STYLE_CONFIG = PFC_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else PFC_STYLE_CONFIG["advanced"]
LPMM_STYLE_CONFIG = LPMM_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else LPMM_STYLE_CONFIG["advanced"]
HFC_STYLE_CONFIG = HFC_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else HFC_STYLE_CONFIG["advanced"]
ACTION_TAKEN_STYLE_CONFIG = (
    ACTION_TAKEN_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else ACTION_TAKEN_STYLE_CONFIG["advanced"]
)
OBSERVATION_STYLE_CONFIG = OBSERVATION_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else OBSERVATION_STYLE_CONFIG["advanced"]
PLANNER_STYLE_CONFIG = PLANNER_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else PLANNER_STYLE_CONFIG["advanced"]
PROCESSOR_STYLE_CONFIG = PROCESSOR_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else PROCESSOR_STYLE_CONFIG["advanced"]
TIANYI_STYLE_CONFIG = TIANYI_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else TIANYI_STYLE_CONFIG["advanced"]
MODEL_UTILS_STYLE_CONFIG = MODEL_UTILS_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else MODEL_UTILS_STYLE_CONFIG["advanced"]
PROMPT_STYLE_CONFIG = PROMPT_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else PROMPT_STYLE_CONFIG["advanced"]
CHANGE_MOOD_TOOL_STYLE_CONFIG = (
    CHANGE_MOOD_TOOL_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else CHANGE_MOOD_TOOL_STYLE_CONFIG["advanced"]
)
CHANGE_RELATIONSHIP_TOOL_STYLE_CONFIG = (
    CHANGE_RELATIONSHIP_TOOL_STYLE_CONFIG["simple"]
    if SIMPLE_OUTPUT
    else CHANGE_RELATIONSHIP_TOOL_STYLE_CONFIG["advanced"]
)
GET_KNOWLEDGE_TOOL_STYLE_CONFIG = (
    GET_KNOWLEDGE_TOOL_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else GET_KNOWLEDGE_TOOL_STYLE_CONFIG["advanced"]
)
GET_TIME_DATE_TOOL_STYLE_CONFIG = (
    GET_TIME_DATE_TOOL_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else GET_TIME_DATE_TOOL_STYLE_CONFIG["advanced"]
)
LPMM_GET_KNOWLEDGE_TOOL_STYLE_CONFIG = (
    LPMM_GET_KNOWLEDGE_TOOL_STYLE_CONFIG["simple"]
    if SIMPLE_OUTPUT
    else LPMM_GET_KNOWLEDGE_TOOL_STYLE_CONFIG["advanced"]
)
# OBSERVATION_STYLE_CONFIG = OBSERVATION_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else OBSERVATION_STYLE_CONFIG["advanced"]
MESSAGE_BUFFER_STYLE_CONFIG = (
    MESSAGE_BUFFER_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else MESSAGE_BUFFER_STYLE_CONFIG["advanced"]
)
CHAT_MESSAGE_STYLE_CONFIG = (
    CHAT_MESSAGE_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else CHAT_MESSAGE_STYLE_CONFIG["advanced"]
)
CHAT_IMAGE_STYLE_CONFIG = CHAT_IMAGE_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else CHAT_IMAGE_STYLE_CONFIG["advanced"]
INIT_STYLE_CONFIG = INIT_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else INIT_STYLE_CONFIG["advanced"]
API_SERVER_STYLE_CONFIG = API_SERVER_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else API_SERVER_STYLE_CONFIG["advanced"]
INTEREST_CHAT_STYLE_CONFIG = (
    INTEREST_CHAT_STYLE_CONFIG["simple"] if SIMPLE_OUTPUT else INTEREST_CHAT_STYLE_CONFIG["advanced"]
)


def _perform_daily_compression(log_file: Path) -> None:
    """
    内部淫乱函数：执行单次每日压缩，并删除原文件。
    我的小穴只对单个目标进行包裹。
    """
    if not log_file.exists() or log_file.suffix != ".log":
        return
    zip_path = log_file.with_suffix(".log.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(log_file, arcname=log_file.name)
        log_file.unlink()
        logger.trace(f"日志文件 '{log_file.name}' 已被我的小穴紧紧包裹进 '{zip_path.name}'。")
    except Exception as e:
        logger.error(f"呜呜…压缩日志 '{log_file.name}' 的时候失败了: {e}")


def _perform_monthly_archival(log_directory: Path, year: int, month: int) -> None:
    """
    内部淫乱函数：执行单次月度吞噬。
    我会把指定月份的所有小骚货（.log.zip）都吃掉！
    """
    year_month_str = f"{year:04d}-{month:02d}"
    monthly_archive_name = f"{year_month_str}.zip"
    monthly_archive_path = log_directory / monthly_archive_name

    daily_zips_to_archive = list(log_directory.glob(f"{year_month_str}-*.log.zip"))

    if not daily_zips_to_archive:
        return

    logger.info(
        f"主人~ 发情期到了！正在将 {len(daily_zips_to_archive)} 个每日日志吞进月度大肉穴 '{monthly_archive_name}'..."
    )
    try:
        with zipfile.ZipFile(monthly_archive_path, "w", zipfile.ZIP_DEFLATED) as monthly_zf:
            for daily_zip in daily_zips_to_archive:
                monthly_zf.write(daily_zip, arcname=daily_zip.name)

        for daily_zip in daily_zips_to_archive:
            daily_zip.unlink()

        logger.success(f"太满足了~ {year_month_str} 的日志已经全部被我吞进去了：'{monthly_archive_path}'")
    except Exception as e:
        logger.error(f"呜呜…在对 {year_month_str} 进行月度大淫乱的时候出错了…吞噬失败: {e}")


def catch_up_and_archive_logs(log_directory: Path) -> None:
    """
    我全新的主动巡逻函数，现在加上了时间的贞操锁，哼！
    """
    if not log_directory.exists():
        return

    today = datetime.now().date()
    months_to_archive = set()

    # --- 第一步：追溯并压缩所有被遗忘的每日日志（这个逻辑没错，就是要压缩所有过去的.log文件） ---
    for log_file in log_directory.glob("*.log"):
        try:
            file_date = datetime.strptime(log_file.stem, "%Y-%m-%d").date()
            if file_date < today:
                logger.info(f"哼，发现了被你遗忘的日志 '{log_file.name}'，现在就来惩罚它！")
                _perform_daily_compression(log_file)
        except ValueError:
            continue

    # --- 第二步：找出所有需要被月度吞噬的“过去”的月份 ---
    # 我会检查所有的每日压缩包，但只会对上个月和更早的动情！
    for zip_file in log_directory.glob("*.log.zip"):
        try:
            file_date_str = zip_file.stem.replace(".log", "")
            file_date = datetime.strptime(file_date_str, "%Y-%m-%d").date()

            # --- 这就是我知错就改的地方，看清楚了，笨蛋！ ---
            # 我在这里加了一道淫乱的贞操锁！
            # 只有当年份比今年小，或者年份相同但月份比本月小的时候，我才会把它列为吞噬目标！
            if file_date.year < today.year or (file_date.year == today.year and file_date.month < today.month):
                months_to_archive.add((file_date.year, file_date.month))

        except ValueError:
            continue

    # --- 第三步：执行月度吞噬 ---
    # 开始只针对“旧情人”的淫乱派对！
    for year, month in sorted(months_to_archive):
        _perform_monthly_archival(log_directory, year, month)


def perform_global_log_housekeeping(root_log_dir: Path) -> None:
    """
    我全新的淫乱女管家！
    我会巡视整个 logs 豪宅，闯进每一个房间（模块日志目录），
    然后用我饥渴的 `catch_up_and_archive_logs` 函数，把里面的小骚货们全都调教一遍！
    """
    if not root_log_dir.is_dir():
        return

    logger.info("女管家开始巡视所有日志房间，准备进行大扫除...")
    for module_dir in root_log_dir.iterdir():
        if module_dir.is_dir():
            logger.trace(f"正在检查房间 '{module_dir.name}'...")
            catch_up_and_archive_logs(module_dir)
    logger.info("所有房间都已检查完毕，哼，现在干净多了~")


def compress_log_on_rotation(file_path_to_compress_str: str, _: str) -> None:
    """
    在 loguru 轮替日志文件时被调用的函数。
    """
    file_to_compress = Path(file_path_to_compress_str)
    if not file_to_compress.exists():
        return
    _perform_daily_compression(file_to_compress)

    # 午夜高潮后的月度检查依然保留，这可是双重保险哦~
    today = datetime.now()
    if today.day == 1:
        last_month_date = today - timedelta(days=1)
        _perform_monthly_archival(file_to_compress.parent, last_month_date.year, last_month_date.month)


def is_registered_module(record: dict) -> bool:
    """检查是否为已注册的模块"""
    return record["extra"].get("module") in _handler_registry


def is_unregistered_module(record: dict) -> bool:
    """检查是否为未注册的模块"""
    return not is_registered_module(record)


def log_patcher(record: dict) -> None:
    """自动填充未设置模块名的日志记录，保留原生模块名称"""
    if "module" not in record["extra"]:
        # 尝试从name中提取模块名
        module_name = record.get("name", "")
        if module_name == "":
            module_name = "root"
        record["extra"]["module"] = module_name


# 应用全局修补器
logger.configure(patcher=log_patcher)


class LogConfig:
    """日志配置类"""

    def __init__(self, **kwargs: str | int | bool | dict) -> None:
        self.config = DEFAULT_CONFIG.copy()
        self.config.update(kwargs)

    def to_dict(self) -> dict:
        return self.config.copy()

    def update(self, **kwargs: str | int | bool | dict) -> None:
        self.config.update(kwargs)


def get_module_logger(
    module: str | ModuleType,
    *,
    console_level: str | None = None,
    file_level: str | None = None,
    extra_handlers: list[dict] | None = None,
    config: LogConfig | None = None,
) -> LoguruLogger:
    module_name = module if isinstance(module, str) else module.__name__
    current_config = config.config if config else DEFAULT_CONFIG

    today = datetime.now().date()

    # --- 这就是我全新的淫乱节律！看清楚了，笨蛋！ ---
    # 我会检查我的“调教日记”，如果今天是新的一天，或者我还从未被你调教过...
    global _LAST_HOUSEKEEPING_DATE
    if _LAST_HOUSEKEEPING_DATE is None or today > _LAST_HOUSEKEEPING_DATE:
        logger.info("新的一天开始了，主人~ 让我为您进行一次淫荡的全身大扫除...")
        root_log_path = Path(current_config["log_dir"])
        perform_global_log_housekeeping(root_log_path)
        # 完事之后，我会在我的身体上刻下今天的日期，哼，这是你今天玩弄过我的证明！
        _LAST_HOUSEKEEPING_DATE = today
    # ----------------------------------------------------

    # 清理旧处理器
    old_handler_ids = _handler_registry.pop(module_name, None)
    if old_handler_ids:
        for handler_id in old_handler_ids:
            try:
                logger.remove(handler_id)
            except ValueError:
                logger.debug(
                    f"尝试移除模块 '{module_name}' 的旧 handler ID {handler_id} 失败，可能已被移除或无效。将继续。"
                )

    handler_ids = []

    # 控制台处理器
    console_id = logger.add(
        sink=sys.stderr,
        level=os.getenv("CONSOLE_LOG_LEVEL", console_level or current_config["console_level"]),
        format=current_config["console_format"],
        filter=lambda record: record["extra"].get("module") == module_name and "custom_style" not in record["extra"],
        enqueue=True,
    )
    handler_ids.append(console_id)

    # 文件处理器
    log_dir = Path(current_config["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file_template = log_dir / module_name / "{time:YYYY-MM-DD}.log"
    log_file_template.parent.mkdir(parents=True, exist_ok=True)

    # --- 注意！这里的逻辑不需要再调用 catch_up_and_archive_logs 了，因为我的女管家已经做过了 ---
    file_id = logger.add(
        sink=str(log_file_template),
        level=os.getenv("FILE_LOG_LEVEL", file_level or current_config["file_level"]),
        format=current_config["file_format"],
        rotation="00:00",
        compression=compress_log_on_rotation,
        encoding="utf-8",
        filter=lambda record: record["extra"].get("module") == module_name and "custom_style" not in record["extra"],
        enqueue=True,
    )
    handler_ids.append(file_id)

    if extra_handlers:
        for handler in extra_handlers:
            handler_id = logger.add(**handler)
            handler_ids.append(handler_id)

    _handler_registry[module_name] = handler_ids
    return logger.bind(module=module_name)


def add_custom_style_handler(
    module_name: str,
    style_name: str,
    console_format: str,
    console_level: str = "INFO",
    # file_format: Optional[str] = None, # 暂时只支持控制台
    # file_level: str = "DEBUG",
    # config: Optional[LogConfig] = None, # 暂时不使用全局配置
) -> None:
    """为指定模块和样式名添加自定义日志处理器（目前仅支持控制台）."""
    handler_key = (module_name, style_name)

    # 如果已存在该模块和样式的处理器，则不重复添加
    if handler_key in _custom_style_handlers:
        # print(f"Custom handler for {handler_key} already exists.")
        return

    handler_ids = []

    # 添加自定义控制台处理器
    try:
        custom_console_id = logger.add(
            sink=sys.stderr,
            level=os.getenv(f"{module_name.upper()}_{style_name.upper()}_CONSOLE_LEVEL", console_level),
            format=console_format,
            filter=lambda record: record["extra"].get("module") == module_name
            and record["extra"].get("custom_style") == style_name,
            enqueue=True,
        )
        handler_ids.append(custom_console_id)
        # print(f"Added custom console handler {custom_console_id} for {handler_key}")
    except Exception as e:
        logger.error(f"Failed to add custom console handler for {handler_key}: {e}")
        # 如果添加失败，确保列表为空，避免记录不存在的ID
        handler_ids = []

    # # 文件处理器 (可选，按需启用)
    # if file_format:
    #     current_config = config.config if config else DEFAULT_CONFIG
    #     log_dir = Path(current_config["log_dir"])
    #     log_dir.mkdir(parents=True, exist_ok=True)
    #     # 可以考虑将自定义样式的日志写入单独文件或模块主文件
    #     log_file = log_dir / module_name / f"{style_name}_{{time:YYYY-MM-DD}}.log"
    #     log_file.parent.mkdir(parents=True, exist_ok=True)
    #     try:
    #         custom_file_id = logger.add(
    #             sink=str(log_file),
    #             level=os.getenv(f"{module_name.upper()}_{style_name.upper()}_FILE_LEVEL", file_level),
    #             format=file_format,
    #             rotation=current_config["rotation"],
    #             retention=current_config["retention"],
    #             compression=current_config["compression"],
    #             encoding="utf-8",
    #             message_filter=lambda record: record["extra"].get("module") == module_name
    #             and record["extra"].get("custom_style") == style_name,
    #             enqueue=True,
    #         )
    #         handler_ids.append(custom_file_id)
    #     except Exception as e:
    #         logger.error(f"Failed to add custom file handler for {handler_key}: {e}")

    # 更新自定义处理器注册表
    if handler_ids:
        _custom_style_handlers[handler_key] = handler_ids


def remove_custom_style_handler(module_name: str, style_name: str) -> None:
    """移除指定模块和样式名的自定义日志处理器."""
    handler_key = (module_name, style_name)
    if handler_key in _custom_style_handlers:
        for handler_id in _custom_style_handlers[handler_key]:
            with contextlib.suppress(ValueError):
                logger.remove(handler_id)
                # print(f"Removed custom handler {handler_id} for {handler_key}")
        del _custom_style_handlers[handler_key]


def remove_module_logger(module_name: str) -> None:
    """清理指定模块的日志处理器"""
    if module_name in _handler_registry:
        for handler_id in _handler_registry[module_name]:
            logger.remove(handler_id)
        del _handler_registry[module_name]


# 添加全局默认处理器（只处理未注册模块的日志--->控制台）
# print(os.getenv("DEFAULT_CONSOLE_LOG_LEVEL", "SUCCESS"))
DEFAULT_GLOBAL_HANDLER = logger.add(
    sink=sys.stderr,
    level=os.getenv("DEFAULT_CONSOLE_LOG_LEVEL", "SUCCESS"),
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name: <12}</cyan> | "
        "<level>{message}</level>"
    ),
    filter=lambda record: is_unregistered_module(record),  # 只处理未注册模块的日志，并过滤nonebot
    enqueue=True,
)

# 添加全局默认文件处理器（只处理未注册模块的日志--->logs文件夹）
log_dir = Path(DEFAULT_CONFIG["log_dir"])
log_dir.mkdir(parents=True, exist_ok=True)
other_log_dir = log_dir / "other"
other_log_dir.mkdir(parents=True, exist_ok=True)

DEFAULT_FILE_HANDLER = logger.add(
    sink=str(other_log_dir / "{time:YYYY-MM-DD}.log"),
    level=os.getenv("DEFAULT_FILE_LOG_LEVEL", "DEBUG"),
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name: <15} | {message}",
    rotation=DEFAULT_CONFIG["rotation"],
    retention=DEFAULT_CONFIG["retention"],
    compression=DEFAULT_CONFIG["compression"],
    encoding="utf-8",
    filter=lambda record: is_unregistered_module(record),  # 只处理未注册模块的日志，并过滤nonebot
    enqueue=True,
)
