import asyncio
import time
from typing import Any, Callable, Coroutine, TypeVar, Optional, Union
from src.common.custom_logging.logger_manager import get_logger # 确保路径正确

logger = get_logger("AIcarusCore.common.ProtectedRunner")

T = TypeVar('T') # 用于泛型返回类型

DEFAULT_POLLING_INTERVAL_SECONDS: float = 1.5
DEFAULT_OVERALL_TIMEOUT_SECONDS: float = 120.0

class TaskTimeoutError(asyncio.TimeoutError):
    """自定义超时错误，用于区分整体操作超时和单次轮询超时。"""
    def __init__(self, message: str):
        super().__init__(message)

class TaskCancelledByExternalEventError(asyncio.CancelledError):
    """自定义错误，表示任务因外部事件而被取消。"""
    def __init__(self, message: str):
        super().__init__(message)

async def execute_protected_task_with_polling(
    task_coro: Coroutine[Any, Any, T],
    task_description: str,
    overall_timeout_seconds: float = DEFAULT_OVERALL_TIMEOUT_SECONDS,
    polling_interval_seconds: float = DEFAULT_POLLING_INTERVAL_SECONDS,
    cancellation_event: Optional[asyncio.Event] = None,
) -> T:
    """
    以受保护的方式执行一个异步任务（通常是LLM调用），并进行轮询等待和中断检查。
    PFC (Plan-For-Continuous) 循环的核心思想是，即使LLM调用是耗时的，
    主循环也应该能够定期检查外部事件（如新消息、用户中断），并在必要时取消当前的LLM操作。
    这个函数通过 asyncio.shield 和轮询等待来实现类似的效果。
    """
    logger.info(f"开始执行受保护任务: '{task_description}' (总超时: {overall_timeout_seconds}s, 轮询间隔: {polling_interval_seconds}s)")
    
    # 创建原始任务
    original_task = asyncio.create_task(task_coro, name=f"protected_original_{task_description[:25]}")
    # 使用 shield 来保护原始任务不被 asyncio.wait_for 直接取消
    shielded_task = asyncio.shield(original_task)
    
    start_time = time.monotonic()

    while True:
        # 1. 检查外部中断信号
        if cancellation_event and cancellation_event.is_set():
            logger.warning(f"任务 '{task_description}': ProtectedRunner内部轮询检测到传入的cancellation_event被设置为True！准备取消。")
            logger.warning(f"任务 '{task_description}' 在轮询前检测到外部取消信号。")
            if not original_task.done(): # 取消原始任务
                original_task.cancel()
                try:
                    await shielded_task # 等待被shield的任务（即原始任务）完成取消
                except asyncio.CancelledError:
                    logger.info(f"任务 '{task_description}' 已成功因外部事件取消。")
                except Exception as e_cancel_early:
                     logger.error(f"任务 '{task_description}' 在尝试因外部事件取消时发生错误: {e_cancel_early}", exc_info=True)
            raise TaskCancelledByExternalEventError(f"任务 '{task_description}' 被外部事件取消。")

        # 2. 检查总体超时
        elapsed_time = time.monotonic() - start_time
        if elapsed_time >= overall_timeout_seconds:
            logger.error(f"任务 '{task_description}' 达到总体超时 ({overall_timeout_seconds}s)。")
            if not original_task.done(): # 取消原始任务
                original_task.cancel()
                try:
                    await shielded_task # 等待被shield的任务完成取消
                except asyncio.CancelledError:
                    logger.info(f"任务 '{task_description}' 已因总体超时而被取消。")
                except Exception as e_cancel_timeout:
                    logger.error(f"任务 '{task_description}' 在尝试因总体超时取消时发生错误: {e_cancel_timeout}", exc_info=True)
            raise TaskTimeoutError(f"任务 '{task_description}' 执行超过总体允许时间 {overall_timeout_seconds} 秒。")

        remaining_time_for_overall_timeout = overall_timeout_seconds - elapsed_time
        current_polling_timeout = min(polling_interval_seconds, remaining_time_for_overall_timeout)
        if current_polling_timeout <= 0:
            current_polling_timeout = 0.01 # 确保至少有一个非常小的正超时

        try:
            logger.debug(f"任务 '{task_description}': 等待受保护任务 {current_polling_timeout:.2f}s (剩余总时间: {remaining_time_for_overall_timeout:.2f}s)...")
            # 等待被 shield 的任务。如果超时，shielded_task (即 original_task) 不会被取消。
            await asyncio.wait_for(shielded_task, timeout=current_polling_timeout)
            
            # 如果 asyncio.wait_for 没有抛出 TimeoutError，说明 shielded_task (original_task) 在此轮询间隔内完成了。
            # 注意：由于 original_task 被 shield，即使 wait_for 完成，original_task 也可能尚未完成（如果它自己需要更长时间）。
            # 但通常情况下，如果 wait_for(shielded_task) 正常返回，意味着 shielded_task.done() 应该为 True。
            
            if original_task.done(): # 检查原始任务是否真的完成了
                if original_task.cancelled():
                    # 如果原始任务被取消了（可能是因为我们之前调用了 original_task.cancel()）
                    logger.warning(f"原始任务 '{task_description}' 已被取消。")
                    # 重新检查 cancellation_event，因为可能是它触发的
                    if cancellation_event and cancellation_event.is_set():
                        raise TaskCancelledByExternalEventError(f"任务 '{task_description}' 被外部事件取消。")
                    raise asyncio.CancelledError(f"原始任务 '{task_description}' 被取消。") # 标准取消错误
                
                exc = original_task.exception()
                if exc:
                    logger.error(f"原始任务 '{task_description}' 执行时发生异常: {exc}", exc_info=exc)
                    raise exc # 重新抛出原始任务的异常
                
                result: T = original_task.result()
                logger.info(f"任务 '{task_description}' 成功完成。")
                return result
            else:
                # 这种情况理论上不应该发生：wait_for(shielded_task) 返回了，但 original_task 还没 done。
                # 这可能意味着 original_task 内部有更复杂的异步结构。
                # 但对于大多数LLM调用（单个awaitable），这不太可能。
                logger.warning(f"任务 '{task_description}': asyncio.wait_for(shielded_task) 返回但原始任务未完成。将继续轮询。")
                # 这种情况，我们应该继续轮询，因为原始任务还没结束。
                # continue 会跳到下一次 while 循环。
        
        except asyncio.TimeoutError: # 这是 asyncio.wait_for(shielded_task, ...) 抛出的单次轮询超时
            logger.debug(f"任务 '{task_description}': 本次轮询超时 ({current_polling_timeout}s)。原始任务仍在后台运行 (受shield保护)。将继续下一轮轮询。")
            # 此时 original_task (被 shielded_task 保护) 并没有被取消，它仍在后台运行。
            # 我们只是结束了本次对它的等待。
            # 在这里可以检查其他外部事件，如果PFC参考那样
            # if external_event_check_callback and await external_event_check_callback():
            #    logger.info(f"任务 '{task_description}': 轮询超时后检测到外部事件，准备取消原始任务。")
            #    if not original_task.done():
            #        original_task.cancel()
            #        # ... (处理取消) ...
            #    raise TaskCancelledByExternalEventError(...)
            continue # 继续外层 while 循环进行下一次轮询

        except asyncio.CancelledError as e_shield_itself_cancelled:
            # 这通常意味着 shielded_task 本身被取消了，这比较少见，除非有代码直接取消了 shield 对象。
            # 更可能的是 original_task 被取消，然后 shielded_task 在被 await 时反映出这个取消。
            logger.warning(f"受保护的任务包装 (shielded_task for '{task_description}') 被取消: {e_shield_itself_cancelled}")
            # 检查原始任务的状态
            if original_task.done() and original_task.cancelled():
                if cancellation_event and cancellation_event.is_set():
                    raise TaskCancelledByExternalEventError(f"任务 '{task_description}' 因外部事件取消 (通过shield反映)。")
                raise asyncio.CancelledError(f"任务 '{task_description}' 被取消 (通过shield反映)。")
            # 如果原始任务没有被取消，但shield被取消了，这不寻常，但我们还是按取消处理
            raise # 重新抛出此处的 CancelledError

        except Exception as e_outer_loop: # 捕获其他意外错误
            logger.error(f"执行受保护任务 '{task_description}' 的轮询循环中发生意外错误: {e_outer_loop}", exc_info=True)
            if not original_task.done(): # 尝试取消原始任务
                original_task.cancel()
                try:
                    await shielded_task # 等待取消传播
                except: # pylint: disable=bare-except
                    pass # 忽略在清理过程中可能发生的错误
            raise # 将原始错误重新抛出

    # 如果循环因为某种原因（除了return或raise）退出，这本身就是逻辑错误。
    # 但作为最后的防御，如果真的到了这里：
    logger.critical(f"任务 '{task_description}': execute_protected_task_with_polling 的主循环意外退出。这是一个逻辑缺陷。")
    if original_task.done():
        if original_task.cancelled():
            raise asyncio.CancelledError(f"任务 '{task_description}' 在循环意外退出后发现已被取消。")
        exc = original_task.exception()
        if exc:
            raise exc
        return original_task.result() # type: ignore
    else:
        # 如果任务还没完成，强制取消并报错
        original_task.cancel()
        try:
            await shielded_task
        except asyncio.CancelledError:
            raise Exception(f"任务 '{task_description}' 在循环意外退出后被强制取消。")
        except Exception as e:
            raise Exception(f"任务 '{task_description}' 在循环意外退出后尝试取消时发生错误: {e}") from e
        raise Exception(f"任务 '{task_description}' 在循环意外退出时尚未完成。")