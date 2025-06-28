# src/common/custom_logging/logging_config.py (小懒猫·最终防线版)
import os
import sys
import threading  # <--- 把它请进来！
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from loguru._logger import Logger

# --- 核心配置 (不变) ---
LOG_DIR = Path(os.getcwd()) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

MODULE_CONFIG_MAP = {
    # 根模块
    "main": ("主程序", "white"),
    "__main__": ("主程序", "white"),
    # AIcarusCore 顶级模块
    "action": ("动作处理", "light-magenta"),
    "common": ("通用模块", "white"),
    "config": ("配置管理", "yellow"),
    "core_communication": ("核心通信", "yellow"),
    "core_logic": ("核心逻辑", "light-yellow"),
    "database": ("数据库", "light-cyan"),
    "focus_chat_mode": ("专注聊天", "light-green"),
    "llmrequest": ("LLM 请求", "light-blue"),
    "message_processing": ("消息处理", "magenta"),
    "plugins": ("插件", "purple"),
    "tools": ("工具箱", "blue"),
    # 动作处理
    "action.action_handler": ("动作处理", "light-magenta"),
    "action.providers.internal_tools_provider": ("内部工具提供", "magenta"),
    "action.components.action_decision_maker": ("动作决策", "magenta"),
    # 通用模块
    "common.custom_logging.logging_config": ("日志配置", "white"),
    "common.custom_logging.logger_manager": ("日志管理", "white"),
    "common.focus_chat_history_builder.chat_prompt_builder": ("聊天提示构建", "green"),
    "common.intelligent_interrupt_system.iis_main": ("智能中断", "green"),
    "common.summarization_observation.summarization_service": ("观察摘要", "light-black"),
    "common.utils": ("通用工具", "white"),
    "common.summarization_observation": ("观察摘要", "light-black"),
    # 配置
    "config.config_io": ("配置IO", "yellow"),
    "config.config_manager": ("配置管理", "yellow"),
    "config.config_updater": ("配置更新", "yellow"),
    # 核心通信
    "core_communication.action_sender": ("动作发送", "yellow"),
    "core_communication.core_ws_server": ("核心WS服务", "yellow"),
    "core_communication.event_receiver": ("事件接收", "yellow"),
    # 核心逻辑
    "core_logic.consciousness_flow": ("核心循环", "yellow"),
    "core_logic.context_builder": ("上下文构建", "yellow"),
    "core_logic.intrusive_thoughts": ("侵入思考", "light-red"),
    "core_logic.prompt_builder": ("提示词构建", "yellow"),
    "core_logic.state_manager": ("状态管理", "light-yellow"),
    "core_logic.thought_generator": ("思考生成", "yellow"),
    "core_logic.thought_persistor": ("思考持久化", "yellow"),
    "core_logic.unread_info_service": ("未读服务", "yellow"),
    # 数据库
    "database.core.connection_manager": ("数据库核心", "cyan"),
    "database.models": ("数据库模型", "cyan"),
    "database.services.action_log_storage_service": ("动作日志", "cyan"),
    "database.services.conversation_storage_service": ("会话存储", "cyan"),
    "database.services.event_storage_service": ("事件存储", "cyan"),
    "database.services.summary_storage_service": ("摘要存储", "cyan"),
    "database.services.thought_storage_service": ("思考存储", "cyan"),
    "database.services": ("数据库服务", "cyan"),
    # 专注聊天
    "focus_chat_mode.action_executor": ("动作执行", "green"),
    "focus_chat_mode.chat_session": ("专注会话", "light-green"),
    "focus_chat_mode.chat_session_manager": ("会话管理", "green"),
    "focus_chat_mode.focus_chat_cycler": ("专注循环", "green"),
    "focus_chat_mode.llm_response_handler": ("LLM响应处理", "green"),
    "focus_chat_mode.summarization_manager": ("摘要管理", "green"),
    # LLM & 工具
    "llmrequest.llm_processor": ("LLM 处理", "light-blue"),
    "llmrequest.utils_model": ("LLM 底层", "blue"),
    "tools.failure_reporter": ("失败报告", "blue"),
    "tools.platform_actions": ("平台动作", "blue"),
    "tools.web_searcher": ("网页搜索", "blue"),
    "tools.search": ("搜索工具", "blue"),
    # 消息处理
    "message_processing.default_message_processor": ("默认消息处理", "magenta"),
}

# --- Loguru 初始化 (不变) ---
logger.remove()

# --- 全局状态与锁 ---
_handlers_created = set()
_lock = threading.Lock()  # <--- 这就是我们的贞操锁！


def _perform_daily_compression(log_file: Path) -> None:
    """哼，就是把昨天的日志文件打包成zip。小事一桩。"""
    if not log_file.exists() or log_file.suffix != ".log":
        return
    zip_path = log_file.with_suffix(".log.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(log_file, arcname=log_file.name)
        log_file.unlink()  # 压缩完就把原来的删了，不占地方
        logger.trace(f"日志文件 '{log_file.name}' 已压缩至 '{zip_path.name}'。")
    except Exception as e:
        logger.error(f"压缩日志 '{log_file.name}' 时失败了: {e}")


def _perform_monthly_archival(log_directory: Path, year: int, month: int) -> None:
    """把指定月份的每日压缩包都吃掉，打包成一个月度大礼包。"""
    year_month_str = f"{year:04d}-{month:02d}"
    monthly_archive_name = f"{year_month_str}.zip"
    monthly_archive_path = log_directory / monthly_archive_name

    daily_zips_to_archive = list(log_directory.glob(f"{year_month_str}-*.log.zip"))

    if not daily_zips_to_archive:
        return

    logger.info(f"月初大扫除！正在将 {len(daily_zips_to_archive)} 个每日日志归档至 '{monthly_archive_name}'...")
    try:
        with zipfile.ZipFile(monthly_archive_path, "w", zipfile.ZIP_DEFLATED) as monthly_zf:
            for daily_zip in daily_zips_to_archive:
                monthly_zf.write(daily_zip, arcname=daily_zip.name)

        for daily_zip in daily_zips_to_archive:
            daily_zip.unlink()

        logger.success(f"{year_month_str} 的日志已成功归档至: '{monthly_archive_path}'")
    except Exception as e:
        logger.error(f"月度归档 {year_month_str} 失败: {e}")


def custom_log_rotation_handler(file_path_to_compress_str: str, _: str) -> None:
    """
    这就是我们给 Loguru 的新玩具！它会在半夜被叫醒。
    """
    # 1. 先把昨天的日志压缩了
    file_to_compress = Path(file_path_to_compress_str)
    _perform_daily_compression(file_to_compress)

    # 2. 看看今天是不是月初第一天
    today = datetime.now().date()
    if today.day == 1:
        # 如果是，就去处理上个月的日志
        last_month_date = today - timedelta(days=1)
        logger.info(f"检测到月初，开始对 {last_month_date.year}年{last_month_date.month}月 的日志进行月度归档...")
        _perform_monthly_archival(
            log_directory=file_to_compress.parent, year=last_month_date.year, month=last_month_date.month
        )


def get_logger(module_name: str) -> Logger:
    """
    获取一个为指定模块配置好的 logger 实例 (小懒猫·视觉居中完美版)。
    """
    # 找到最匹配的别名和颜色
    best_match_key = ""
    for prefix in MODULE_CONFIG_MAP:
        normalized_module_name = module_name.replace("AIcarusCore\\", "").replace("\\", ".")
        if normalized_module_name.endswith(prefix) and len(prefix) > len(best_match_key):
            best_match_key = prefix

    if best_match_key:
        alias, color = MODULE_CONFIG_MAP[best_match_key]
    else:
        alias = module_name.split(".")[-1]
        color = "white"

    handler_key = f"{alias}_{color}"

    # ✨✨✨ 终极魔法！这次是居中对齐！✨✨✨
    # 1. 计算最大显示宽度（考虑汉字占2个字符）
    max_width = 0
    for a, _ in MODULE_CONFIG_MAP.values():
        width = sum(2 if "\u4e00" <= char <= "\u9fff" else 1 for char in a)
        if width > max_width:
            max_width = width
            max_width -= 2  # ✨ 在这里手动减小总宽度！✨

    # 2. 计算当前别名的显示宽度
    current_alias_width = sum(2 if "\u4e00" <= char <= "\u9fff" else 1 for char in alias)

    # 3. 计算总共需要填充的空格数
    total_padding = max_width - current_alias_width

    # 4. 把空格一分为二，塞到两边
    left_padding = total_padding // 2
    right_padding = total_padding - left_padding

    # 5. 生成我们最终用于显示的、带两边空格的别名
    padded_alias = f"{' ' * left_padding}{alias}{' ' * right_padding}"
    # ✨✨✨ 魔法结束 ✨✨✨

    with _lock:
        if handler_key not in _handlers_created:
            # 格式化字符串现在变得超级简单！
            console_format = (
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <5}</level> | "
                # 直接使用我们处理好的 padded_alias
                f"<{color}><bold>{{extra[padded_alias]}}</bold></{color}> | "
                "<level>{message}</level>"
            )

            logger.add(
                sys.stderr,
                level=os.getenv("CONSOLE_LOG_LEVEL", "INFO").upper(),
                format=console_format,
                filter=lambda record: record["extra"].get("padded_alias") == padded_alias,
                colorize=True,
                enqueue=True,
            )

            log_file_path = LOG_DIR / alias / "{time:YYYY-MM-DD}.log"
            log_file_path.parent.mkdir(parents=True, exist_ok=True)

            # 文件日志也用同样的方式对齐
            file_format_str = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {extra[padded_alias]} | {message}"

            logger.add(
                sink=log_file_path,
                level=os.getenv("FILE_LOG_LEVEL", "DEBUG").upper(),
                format=file_format_str,
                rotation="00:00",
                retention="90 days",
                compression=custom_log_rotation_handler,
                encoding="utf-8",
                enqueue=True,
                filter=lambda record: record["extra"].get("padded_alias") == padded_alias,
            )

            _handlers_created.add(handler_key)
            logger.debug(f"已为别名 '{alias}' 创建专属日志处理器(视觉居中完美版)。")

    # 把我们处理好的带两边空格的别名，绑定到 extra 数据里！
    return logger.bind(padded_alias=padded_alias)
