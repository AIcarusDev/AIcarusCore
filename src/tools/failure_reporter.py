# Core_thinking/tools/failure_reporter.py
"""
行动失败报告工具
"""

import json

from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


async def report_action_failure(
    tool_name_that_failed: str | None = None,
    failure_reason: str | None = None,
    intended_action_description: str | None = None,
    intended_action_motivation: str | None = None,
    tool_arguments_used: dict | None = None,
    failed_action_id: str | None = None,
    reason_for_failure_short: str | None = None,  # 兼容旧的调用方式
    **kwargs: any,  # 增加kwargs以捕获所有其他参数，提高兼容性
) -> str:
    """
    报告一个详细的行动失败。所有参数都是可选的，以提高健壮性。

    Args:
        tool_name_that_failed: (可选) 失败的工具名称。
        failure_reason: (可选) 失败的详细原因。
        intended_action_description: (可选) 原始行动的描述。
        intended_action_motivation: (可选) 原始行动的动机。
        tool_arguments_used: (可选) 执行失败工具时使用的参数。
        failed_action_id: (可选) 失败的行动ID。
        reason_for_failure_short: (可选) 兼容旧版调用的失败原因简述。

    Returns:
        一个详细的、格式化的失败报告字符串。
    """
    try:
        # 优先使用更详细的 failure_reason，如果不存在则使用旧的 reason_for_failure_short
        final_reason = failure_reason or reason_for_failure_short or "原因未提供"

        # 记录日志
        log_message = (
            f"报告行动失败: [工具: {tool_name_that_failed or '未知'}] "
            f"[描述: {intended_action_description or '未知'}] "
            f"[原因: {final_reason}]"
        )
        logger.info(log_message)

        # 构建报告
        report_parts = []
        if intended_action_description:
            report_parts.append(f"我原本想要“{intended_action_description}”。")
        if intended_action_motivation:
            report_parts.append(f"我的目的是“{intended_action_motivation}”。")
        if tool_name_that_failed:
            report_parts.append(f"为了实现这个目标，我尝试使用工具“{tool_name_that_failed}”。")

        if tool_arguments_used:
            try:
                args_str = json.dumps(tool_arguments_used, ensure_ascii=False, indent=2)
                report_parts.append(f"我当时使用的参数是:\n```json\n{args_str}\n```")
            except Exception:
                report_parts.append(f"我当时使用的参数是: {str(tool_arguments_used)}")

        report_parts.append(f"但是，这个行动失败了，失败的原因是：{final_reason}。")

        if failed_action_id:
            report_parts.append(f"（相关行动ID: {failed_action_id}）")

        if not report_parts:
            return "报告了一个未知行动的未知失败。"

        failure_report = "\n".join(report_parts)
        return failure_report

    except Exception as e:
        # 如果连报告生成都失败了，提供一个健壮的回退
        logger.error(f"生成详细失败报告时发生严重错误: {e}", exc_info=True)
        # 此时的 intended_action_description 和 failure_reason 可能为 None
        desc = intended_action_description or "某个未知动作"
        reason = failure_reason or reason_for_failure_short or "一个未知问题"
        return f"我尝试执行“{desc}”时遇到了一个问题（{reason}），但连生成详细的失败报告都失败了。"


# The old test is no longer compatible with the new function signature.
# if __name__ == "__main__":
#     async def main_test() -> None:
#         # ... test cases for the new signature would go here ...
#         pass
#     asyncio.run(main_test())
