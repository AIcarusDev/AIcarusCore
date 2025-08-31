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

        # 在连接依赖前，执行一次表情包垃圾回收
        if container.sticker_service:
            await container.sticker_service.run_garbage_collection()

        # 2. 连接静态依赖
        wire_dependencies(container)
        logger.info("核心服务依赖已成功连接。")

        # 初始化有状态的服务 (如 GoalManager)
        if container.state_manager:
            await container.state_manager.initialize()
            logger.info("状态管理器及其子组件 (如GoalManager) 已从数据库同步状态。")

        # 3. 启动核心服务
        # 启动WS服务器，它会开始接受连接并进行安检
        ws_task = asyncio.create_task(container.core_comm_layer.start(), name="CoreWSServer")

        # 启动图像分析服务 (如果启用)
        # 这会在后台异步运行，处理提交的图片分析任务
        if container.image_analysis_service:
            container.image_analysis_service.start()
            logger.info("后台图像分析服务已启动。")


        # 4. 在后台处理动态依赖的连接 (QQChatSessionManager)
        # 这不会阻塞主服务运行
        dynamic_wiring_task = asyncio.create_task(
            wire_dynamic_dependencies(container), name="DynamicWiring"
        )
        background_tasks.add(dynamic_wiring_task)
        # 当任务自己完成后，就把它从集合里移除
        dynamic_wiring_task.add_done_callback(background_tasks.discard)

        logger.info("正在等待动态依赖连接完成...")
        await dynamic_wiring_task
        logger.info("动态依赖连接已完成。")

        # 最后，启动认知周期循环
        logger.info("正在尝试启动认知周期循环...")
        logic_task = await container.core_logic.start_thinking_loop()

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
            if container.chat_session_manager:
                container.chat_session_manager.shutdown()
            if container.core_logic:
                await container.core_logic.stop()  # 这会处理 intrusive_generator 的线程
            if container.core_comm_layer:
                await container.core_comm_layer.stop()
            if container.conn_manager:
                await container.conn_manager.close_client()
            if container.image_analysis_service:
                await container.image_analysis_service.stop()
            logger.info("AIcarus Core 系统关闭流程执行完毕。")
            # 关闭所有 LLM 客户端
            llm_clients_to_close = [
                container.main_consciousness_llm_client,
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
