# src/main.py
import asyncio
import contextlib

from src.bootstrap.builder import ServiceBuilder
from src.bootstrap.lifecycle_manager import LifecycleManager
from src.bootstrap.wiring import wire_dependencies, wire_dynamic_dependencies
from src.cognitive_cycle import CognitiveCycle
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


async def start_core_system() -> None:
    """启动 AIcarus Core 系统的全新、优雅的入口."""
    container = None
    cognitive_cycle = None
    lifecycle_manager = None
    background_tasks = set()
    try:
        builder = ServiceBuilder()
        container = await builder.build_container()
        logger.info("所有服务实例已成功创建。")

        # --- 启动生命周期管理器 ---
        lifecycle_manager = LifecycleManager(container)
        interruption_message = lifecycle_manager.start() # 启动并获取提示信息

        # 将中断信息注入到 PromptBuilder
        if interruption_message and container.prompt_builder:
            container.prompt_builder._interruption_message = interruption_message
            logger.info(f"检测到中断事件，已注入提示: {interruption_message}")

        # 在连接依赖前，执行一次表情包垃圾回收
        if container.qq_sticker_service:
            await container.qq_sticker_service.run_garbage_collection()

        # 2. 连接静态依赖
        wire_dependencies(container)
        logger.info("核心服务依赖已成功连接。")

        # 初始化有状态的服务 (如 GoalManager)
        if container.state_manager:
            await container.state_manager.initialize()
            logger.info("状态管理器及其子组件 (如GoalManager) 已从数据库同步状态。")

        # 3. 实例化顶层协调者
        cognitive_cycle = CognitiveCycle(container)
        # 将协调者的触发器注入到需要它的地方 (例如慢思考服务)
        # 注意: 这需要在 builder.py 中为 deliberation_service 设置一个引用
        if container.deliberation_service and hasattr(
            container.deliberation_service, "set_cycle_trigger"
        ):
            container.deliberation_service.set_cycle_trigger(
                cognitive_cycle.trigger_immediate_thought_cycle
            )

        # 4. 启动核心服务
        # 启动WS服务器，它会开始接受连接并进行安检
        ws_task = asyncio.create_task(container.core_comm_layer.start(), name="CoreWSServer")
        background_tasks.add(ws_task)

        # 启动图像分析服务 (如果启用)
        if container.image_analysis_service:
            container.image_analysis_service.start()
            logger.info("后台图像分析服务已启动。")

        # 5. 在后台处理动态依赖的连接 (等待安检完成)
        dynamic_wiring_task = asyncio.create_task(
            wire_dynamic_dependencies(container), name="DynamicWiring"
        )
        background_tasks.add(dynamic_wiring_task)
        # 当任务自己完成后，就把它从集合里移除
        dynamic_wiring_task.add_done_callback(background_tasks.discard)

        logger.info("正在等待动态依赖连接完成...")
        await dynamic_wiring_task
        logger.info("动态依赖连接已完成。")

        # 6. 最后，启动认知周期循环
        logger.info("正在尝试启动认知周期...")
        logic_task = asyncio.create_task(cognitive_cycle.start(), name="CognitiveCycle")
        background_tasks.add(logic_task)

        # 7. 等待核心任务（WS服务和认知循环）中任意一个结束
        done, pending = await asyncio.wait(
            {ws_task, logic_task}, return_when=asyncio.FIRST_COMPLETED
        )

        for task in done:
            if exc := task.exception():
                logger.critical(f"核心任务 '{task.get_name()}' 异常终止: {exc!r}", exc_info=exc)
                raise exc  # 重新抛出以触发 finally
            else:
                logger.info(f"核心任务 '{task.get_name()}' 正常完成。")

        # 取消所有剩余的挂起任务
        for task in pending:
            task.cancel()

    except Exception as e:
        logger.critical(f"AIcarus Core 系统启动或运行遭遇致命错误: {e}", exc_info=True)
    finally:
        logger.info("--- AIcarus Core 系统正在进入关闭流程 ---")

        # --- 优雅地停止生命周期管理器 ---
        if lifecycle_manager:
            await lifecycle_manager.stop()

        # 优雅地关闭认知循环
        if cognitive_cycle:
            await cognitive_cycle.stop()

        if background_tasks:
            logger.info(f"正在取消 {len(background_tasks)} 个后台任务...")
            for task in background_tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await asyncio.gather(*background_tasks, return_exceptions=True)
            logger.info("所有后台任务已处理完毕。")

        if container:
            if container.core_comm_layer:
                await container.core_comm_layer.stop()
            if container.conn_manager:
                await container.conn_manager.close_client()
            if container.image_analysis_service:
                await container.image_analysis_service.stop()
            logger.info("核心服务已关闭。")

            # 关闭LLM客户端
            llm_clients_to_close = [
                container.main_consciousness_llm_client,
                container.web_search_agent_client,
                container.url_context_agent_client,
                container.deliberation_llm_client,
            ]
            close_tasks = [
                client.llm_client.close()
                for client in llm_clients_to_close
                if client and hasattr(client, "llm_client") and hasattr(client.llm_client, "close")
            ]
            if close_tasks:
                await asyncio.gather(*close_tasks, return_exceptions=True)
            logger.info("所有 LLM 客户端已处理完毕。")

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
