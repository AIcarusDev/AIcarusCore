# src/main.py
import asyncio

from src.bootstrap.builder import ServiceBuilder
from src.bootstrap.wiring import wire_dependencies, wire_dynamic_dependencies
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


async def start_core_system() -> None:
    """启动 AIcarus Core 系统的全新、优雅的入口."""
    container = None
    try:
        # 1. 构建服务容器，创建所有服务实例
        builder = ServiceBuilder()
        container = await builder.build_container()
        logger.info("所有服务实例已成功创建。")

        # 2. 连接静态依赖
        wire_dependencies(container)
        logger.info("核心服务依赖已成功连接。")

        # 3. 启动核心服务
        # 启动WS服务器，它会开始接受连接并进行安检
        ws_task = asyncio.create_task(container.core_comm_layer.start(), name="CoreWSServer")

        # 启动主思考循环
        logic_task = await container.core_logic.start_thinking_loop()

        # 启动侵入性思维后台线程 (如果启用)
        if container.intrusive_generator:
            container.intrusive_generator.start_background_generation()

        # 4. 在后台处理动态依赖的连接 (ChatSessionManager)
        # 这不会阻塞主服务运行
        background_tasks = set()
        dynamic_wiring_task = asyncio.create_task(
            wire_dynamic_dependencies(container), name="DynamicWiring"
        )
        background_tasks.add(dynamic_wiring_task)
        dynamic_wiring_task.add_done_callback(background_tasks.discard)

        # 5. 等待核心任务结束
        done, pending = await asyncio.wait(
            {ws_task, logic_task}, return_when=asyncio.FIRST_COMPLETED
        )

        for task in done:
            if exc := task.exception():
                logger.critical(f"核心任务 '{task.get_name()}' 异常终止: {exc!r}", exc_info=exc)
                raise exc  # 重新抛出异常以触发关闭

    except Exception as e:
        logger.critical(f"AIcarus Core 系统启动或运行遭遇致命错误: {e}", exc_info=True)
    finally:
        logger.info("--- AIcarus Core 系统正在进入关闭流程 ---")
        if container:
            # 优雅地关闭
            if container.core_logic:
                await container.core_logic.stop()
            if container.core_comm_layer:
                await container.core_comm_layer.stop()
            if container.conn_manager:
                await container.conn_manager.close_client()
        logger.info("AIcarus Core 系统关闭流程执行完毕。")


async def main() -> None:
    """AIcarus Core 的主入口函数."""
    try:
        await start_core_system()
    except KeyboardInterrupt:
        logger.info("AIcarus Core: 用户中断，正在退出...")
    except Exception as main_exc:
        logger.critical(f"AIcarus Core: 顶层执行异常: {main_exc}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
