# src/main.py
import asyncio
import contextlib

from src.bootstrap.builder import ServiceBuilder
from src.bootstrap.wiring import wire_dependencies, wire_dynamic_dependencies
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


async def start_core_system() -> None:
    """启动 AIcarus Core 系统的全新、优雅的入口."""
    container = None
    background_tasks = set()
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
        # 它的关闭是由 stop_event (threading.Event) 控制的，所以不在这里管理
        if container.intrusive_generator:
            container.intrusive_generator.start_background_generation()

        # 4. 在后台处理动态依赖的连接 (ChatSessionManager)
        # 这不会阻塞主服务运行
        dynamic_wiring_task = asyncio.create_task(
            wire_dynamic_dependencies(container), name="DynamicWiring"
        )
        background_tasks.add(dynamic_wiring_task)
        # 当任务自己完成后，就把它从集合里移除
        dynamic_wiring_task.add_done_callback(background_tasks.discard)

        # 5. 等待核心任务结束
        # 核心任务是 ws_task 和 logic_task，它们决定了程序的生命周期
        main_tasks = {ws_task, logic_task}
        done, pending = await asyncio.wait(main_tasks, return_when=asyncio.FIRST_COMPLETED)

        # 检查是哪个核心任务先结束了，以及为什么
        for task in done:
            if exc := task.exception():
                logger.critical(f"核心任务 '{task.get_name()}' 异常终止: {exc!r}", exc_info=exc)
                # 重新抛出异常以触发下面的 finally 清理流程
                raise exc
            else:
                logger.info(f"核心任务 '{task.get_name()}' 正常完成。")

        # 如果一个核心任务结束了，我们也应该取消另一个，准备关机
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    except Exception as e:
        logger.critical(f"AIcarus Core 系统启动或运行遭遇致命错误: {e}", exc_info=True)
    finally:
        logger.info("--- AIcarus Core 系统正在进入关闭流程 ---")

        # 在关闭核心服务之前，先处理掉所有后台的“小弟”
        if background_tasks:
            logger.info(f"正在取消 {len(background_tasks)} 个后台任务...")
            for task in background_tasks:
                task.cancel()

            # 使用 gather 等待所有取消操作完成
            # return_exceptions=True 就像给它们买了保险，一个任务取消失败不会影响其他的
            await asyncio.gather(*background_tasks, return_exceptions=True)
            logger.info("所有后台任务已处理完毕。")

        if container:
            # 优雅地关闭核心服务
            if container.core_logic:
                await container.core_logic.stop()  # 这会处理 intrusive_generator 的线程
            if container.core_comm_layer:
                await container.core_comm_layer.stop()
            if container.conn_manager:
                await container.conn_manager.close_client()
            logger.info("AIcarus Core 系统关闭流程执行完毕。")
            # 关闭所有 LLM 客户端
            llm_clients_to_close = [
                container.main_consciousness_llm_client,
                container.summary_llm_client,
                container.intrusive_thoughts_llm_client,
                container.focused_chat_llm_client,
                container.web_search_agent_client,
                container.url_context_agent_client,
            ]
            for client in llm_clients_to_close:
                if client and hasattr(client, "llm_client") and hasattr(client.llm_client, "close"):
                    try:
                        # 注意：我们要关闭的是底层的 UnderlyingLLMClient 实例
                        await client.llm_client.close()
                    except Exception as e_close:
                        logger.error(f"关闭一个 LLM 客户端时出错: {e_close}")
            logger.info("所有 LLM 客户端已处理完毕。")


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
