# Core_thinking/tools/failure_reporter.py
"""
行动失败报告工具
"""

from src.common.custom_logging.logger_manager import get_logger

logger = get_logger("AIcarusCore.tools.failure_reporter")


async def report_action_failure(
    intended_action_description: str,
    intended_action_motivation: str,
    reason_for_failure_short: str | None = None
) -> str:
    """
    报告行动失败

    Args:
        intended_action_description: 原始行动描述
        intended_action_motivation: 行动动机
        reason_for_failure_short: 失败原因简述

    Returns:
        格式化的失败报告
    """
    try:
        logger.info(
            f"报告行动失败: {intended_action_description} - {reason_for_failure_short}"
        )

        if reason_for_failure_short:
            failure_report = (
                f"我原本想要'{intended_action_description}'，"
                f"动机是'{intended_action_motivation}'，"
                f"但是遇到了问题：{reason_for_failure_short}"
            )
        else:
            failure_report = (
                f"我原本想要'{intended_action_description}'，"
                f"动机是'{intended_action_motivation}'，"
                "但是遇到了问题，具体原因不明。"
            )

        return failure_report

    except Exception as e:
        logger.error(f"生成失败报告时发生错误: {e}")
        return f"我尝试执行'{intended_action_description}'时遇到了问题，但连失败报告都生成失败了。"


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
