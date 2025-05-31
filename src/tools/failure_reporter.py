# Core_thinking/tools/failure_reporter.py
import asyncio
from src.common.custom_logging.logger_manager import get_logger
logger = get_logger("AIcarusCore.failure_reporter")


async def report_action_failure(
    intended_action_description: str, intended_action_motivation: str, reason_for_failure_short: str | None = None
) -> str:
    """
    Generates a standardized message indicating an action failed.
    Args:
        intended_action_description (str): The original action Shuang intended to take.
        intended_action_motivation (str): Shuang's motivation for the action.
        reason_for_failure_short (str | None): A brief, optional reason for the failure.
    Returns:
        str: A formatted failure message.
    """
    logger.info(
        f"[FailureReporter] 报告动作失败: '{intended_action_description}', "
        f"原因: '{reason_for_failure_short or '未指定'}'"
    )

    if reason_for_failure_short:
        failure_message = (
            f"你尝试进行 '{intended_action_description}' 这个动作，但它没有成功，"
            f"看起来是因为：{reason_for_failure_short}。"
        )
    else:
        failure_message = f"你尝试进行 '{intended_action_description}' 这个动作，但它失败了，具体原因不太清楚。"

    await asyncio.sleep(0.01)
    return failure_message


if __name__ == "__main__":

    async def main_test() -> None:
        test_report1 = await report_action_failure(
            intended_action_description="给火星发送一封电子邮件",
            intended_action_motivation="想知道火星人最近怎么样",
            reason_for_failure_short="目前没有可以发送邮件到火星的工具",
        )
        logger.info(test_report1)

        test_report2 = await report_action_failure(
            intended_action_description="瞬间移动到月球", intended_action_motivation="想看看月球上的风景"
        )
        logger.info(test_report2)

    asyncio.run(main_test())
