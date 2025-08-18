# src/common/custom_logging/logging_config.py
import os
import sys
import threading
import zipfile
from datetime import date, datetime
from pathlib import Path

from loguru import logger
from loguru._logger import Logger

# --- 核心配置 ---
LOG_DIR = Path(os.getcwd()) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 模块名称到显示别名和颜色的映射 (这个是我们的宝物，必须保留！)
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
    "common.utils": ("通用工具", "white"),
    "common.json_parser.json_parser": ("JSON 解析", "white"),
    # 配置
    "config.config_io": ("配置IO", "yellow"),
    "config.config_manager": ("配置管理", "yellow"),
    "config.config_updater": ("配置更新", "yellow"),
    "config.config_validator": ("配置验证", "yellow"),
    # 核心通信
    "core_communication.action_sender": ("动作发送", "yellow"),
    "core_communication.core_ws_server": ("核心WS服务", "yellow"),
    "core_communication.event_receiver": ("事件接收", "yellow"),
    "core_communication.event_sender": ("事件发送", "yellow"),
    "core_communication.message_receiver": ("消息接收", "yellow"),
    # 核心逻辑
    "core_logic.consciousness_flow": ("核心循环", "yellow"),
    "core_logic.context_builder": ("上下文构建", "yellow"),
    "core_logic.intrusive_thoughts": ("侵入思考", "light-red"),
    "core_logic.prompt_builder": ("提示词构建", "yellow"),
    "core_logic.state_manager": ("状态管理", "light-yellow"),
    "core_logic.thought_generator": ("思考生成", "yellow"),
    "core_logic.thought_persistor": ("思考持久化", "yellow"),
    "core_logic.unread_info_service": ("未读服务", "yellow"),
    # 插件
    # 数据库
    "database.core.connection_manager": ("数据库核心", "cyan"),
    "database.models": ("数据库模型", "cyan"),
    "database.services.action_log_storage_service": ("动作日志", "cyan"),
    "database.services.conversation_storage_service": ("会话存储", "cyan"),
    "database.services.event_storage_service": ("事件存储", "cyan"),
    "database.services.thought_storage_service": ("思考存储", "cyan"),
    "database.services": ("数据库服务", "cyan"),
    # 专注聊天
    "focus_chat_mode.action_executor": ("动作执行", "green"),
    "focus_chat_mode.chat_session": ("专注会话", "light-green"),
    "focus_chat_mode.chat_session_manager": ("会话管理", "green"),
    "focus_chat_mode.focus_chat_cycler": ("专注循环", "green"),
    "focus_chat_mode.llm_response_handler": ("LLM响应处理", "green"),
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
_lock = threading.Lock()
# 这个全局变量是我们的启动检查核心，必须留下！
_LAST_HOUSEKEEPING_DATE: date | None = None

# --- 日志归档逻辑 (完全保留，因为它们本身是完美的) ---


def _compress_log_file(log_file: Path) -> None:
    """哼，就是把一个.log文件打包成zip。小事一桩."""
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


def _archive_monthly_logs(log_directory: Path, year: int, month: int) -> None:
    """把指定月份的每日压缩包都吃掉，打包成一个月度大礼包."""
    year_month_str = f"{year:04d}-{month:02d}"
    monthly_archive_name = f"{year_month_str}.zip"
    monthly_archive_path = log_directory / monthly_archive_name

    daily_zips_to_archive = list(log_directory.glob(f"{year_month_str}-*.log.zip"))

    if not daily_zips_to_archive:
        return

    logger.info(
        f"月初大扫除！正在将 {len(daily_zips_to_archive)} 个每日日志归档至 '{monthly_archive_name}'"
    )
    try:
        with zipfile.ZipFile(monthly_archive_path, "w", zipfile.ZIP_DEFLATED) as monthly_zf:
            for daily_zip in daily_zips_to_archive:
                # 把每日zip包里的内容解出来再写进去，避免zip套zip
                with zipfile.ZipFile(daily_zip, "r") as daily_zf:
                    for item in daily_zf.infolist():
                        monthly_zf.writestr(item, daily_zf.read(item.filename))

        for daily_zip in daily_zips_to_archive:
            daily_zip.unlink()

        logger.success(f"{year_month_str} 的日志已成功归档至: '{monthly_archive_path}'")
    except Exception as e:
        logger.error(f"月度归档 {year_month_str} 失败: {e}")


def perform_log_housekeeping_on_startup(root_log_dir: Path) -> None:
    """在程序启动时进行一次性的日志追溯、压缩和归档.

    这就像游戏开始前加载资源一样，一次搞定，后面不愁!
    """
    if not root_log_dir.is_dir():
        return

    logger.info("启动程序，开始全局日志清理和归档检查...")
    today = datetime.now().date()

    for module_dir in root_log_dir.iterdir():
        if not module_dir.is_dir():
            continue

        logger.trace(f"正在检查模块目录 '{module_dir.name}'...")

        months_to_archive = set()

        # 1. 追溯并压缩所有被遗忘的 .log 文件
        for log_file in module_dir.glob("*.log"):
            try:
                file_date = datetime.strptime(log_file.stem, "%Y-%m-%d").date()
                if file_date < today:
                    logger.info(f"哼，发现了被你遗忘的日志 '{log_file.name}'，现在就来惩罚它！")
                    _compress_log_file(log_file)
            except ValueError:
                continue  # 文件名不是 YYYY-MM-DD 格式，不管它

        # 2. 找出需要进行月度归档的月份
        for zip_file in module_dir.glob("*.log.zip"):
            try:
                file_date_str = zip_file.stem.replace(".log", "")
                file_date = datetime.strptime(file_date_str, "%Y-%m-%d").date()
                if file_date.year < today.year or (
                    file_date.year == today.year and file_date.month < today.month
                ):
                    months_to_archive.add((file_date.year, file_date.month))
            except ValueError:
                continue

        # 3. 执行月度归档
        for year, month in sorted(months_to_archive):
            _archive_monthly_logs(module_dir, year, month)

    logger.info("全局日志清理和归档检查完成！程序可以色色地跑起来了~")


def get_logger(module_name: str) -> Logger:
    """获取一个为指定模块配置好的 logger 实例.

    它现在是完美的，集美观、健壮、高效于一身!
    """
    # 找到最匹配的别名和颜色，这个逻辑很棒，保留！
    best_match_key = ""
    # 兼容 Windows 和 Linux 的路径分隔符
    normalized_module_name = module_name.replace("AIcarusCore\\", "").replace("\\", ".")

    for prefix in MODULE_CONFIG_MAP:
        if normalized_module_name.endswith(prefix) and len(prefix) > len(best_match_key):
            best_match_key = prefix

    if best_match_key:
        alias, color = MODULE_CONFIG_MAP[best_match_key]
    else:
        alias = module_name.split(".")[-1]
        color = "white"

    handler_key = f"{alias}_{color}"

    # --- 视觉对齐的艺术，必须恢复！---
    # 1. 计算所有别名中的最大显示宽度（汉字算2，英文算1）
    max_width = 0
    for a, _ in MODULE_CONFIG_MAP.values():
        width = sum(2 if "\u4e00" <= char <= "\u9fff" else 1 for char in a)
        if width > max_width:
            max_width = width

    # 2. 计算当前别名的显示宽度
    current_alias_width = sum(2 if "\u4e00" <= char <= "\u9fff" else 1 for char in alias)

    # 3. 计算需要填充的总空格数
    total_padding = max_width - current_alias_width

    # 4. 把空格均匀地塞到两边，实现完美的居中！
    left_padding = total_padding // 2
    right_padding = total_padding - left_padding
    padded_alias = f"{' ' * left_padding}{alias}{' ' * right_padding}"

    with _lock:
        # ----------------------------------------------------
        # 天才般的每日一次启动检查！就在这里！
        global _LAST_HOUSEKEEPING_DATE
        today = datetime.now().date()
        if _LAST_HOUSEKEEPING_DATE is None:
            # 第一次调用get_logger时，执行全局清理
            perform_log_housekeeping_on_startup(LOG_DIR)
            _LAST_HOUSEKEEPING_DATE = today
        # ----------------------------------------------------

        if handler_key not in _handlers_created:
            # --- 控制台日志处理器 (恢复美学！) ---
            console_format = (
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <5}</level> | "
                # 直接使用我们精心计算好的、带居中空格的 padded_alias
                f"<{color}><bold>{{extra[padded_alias]}}</bold></{color}> | "
                "<level>{message}</level>"
            )
            logger.add(
                sys.stderr,  # 错误和日志信息默认输出到 stderr 是个好习惯
                level=os.getenv("CONSOLE_LOG_LEVEL", "INFO").upper(),
                format=console_format,
                filter=lambda record: record["extra"].get("padded_alias") == padded_alias,
                colorize=True,
                enqueue=True,  # 异步写入，不阻塞主线程，性能 up up!
            )

            # --- 文件日志处理器 (使用 loguru 内置的健壮功能) ---
            log_file_path = LOG_DIR / alias / "{time:YYYY-MM-DD}.log"

            # 文件日志也用对齐的别名，但是用普通alias，因为文件里不需要空格对齐
            file_format = (
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {extra[alias]: <"
                + str(max_width)
                + "} | "
                "{name}:{function}:{line} | {message}"
            )

            logger.add(
                sink=log_file_path,
                level=os.getenv("FILE_LOG_LEVEL", "DEBUG").upper(),
                format=file_format,
                rotation="00:00",  # 每天午夜，自动切割日志文件
                compression="zip",  # 切割后，自动压缩成 .zip，完美！
                retention="90 days",  # 保留90天的日志
                encoding="utf-8",
                enqueue=True,  # 同样异步写入
                backtrace=True,  # 发生异常时，记录完整的堆栈信息，超好用
                diagnose=True,  # 异常诊断信息更详细
                filter=lambda record: record["extra"].get("alias") == alias,
            )

            _handlers_created.add(handler_key)
            logger.debug(f"已为别名 '{alias}' 创建专属日志处理器(视觉居中完美最终版)！")

    # 绑定 padded_alias 用于控制台显示，绑定普通 alias 用于文件记录
    return logger.bind(padded_alias=padded_alias, alias=alias)
