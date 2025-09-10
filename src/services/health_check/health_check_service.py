import asyncio
import websockets
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)

async def check_ui_server_status(host: str = "localhost", port: int = 8088) -> bool:
    """
    尝试连接到 WebSocket 服务器以检查其是否正在运行。
    """
    uri = f"ws://{host}:{port}"
    try:
        async with asyncio.timeout(2):
            async with websockets.connect(uri) as websocket:
                # 如果连接成功，我们假设服务器正在运行
                logger.debug(f"成功连接到 WebSocket 服务器 {uri}")
                return True
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
        logger.debug(f"无法连接到 WebSocket 服务器 {uri}: {e}")
        return False
    except Exception as e:
        logger.error(f"检查 UI 服务器状态时发生意外错误: {e}", exc_info=True)
        return False
