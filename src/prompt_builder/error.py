# src/prompt_builder/error.py
from typing import Any


class PromptBuilderError(Exception):
    """当在 Prompt 构建过程中发生不可恢复的错误时抛出."""

    def __init__(self, message: str, context: dict | None = None) -> None:
        super().__init__(message)
        self.context = context or {}
        self.message = message

    def __str__(self) -> str:  # noqa: D105
        if self.context:
            context_str = ", ".join(f"{k}={v}" for k, v in self.context.items())
            return f"{self.message} [Context: {context_str}]"
        return self.message


class SessionManagerError(PromptBuilderError):
    """当会话管理器相关错误发生时抛出."""

    def __init__(self, message: str, session_manager_status: str | None = None) -> None:
        context = {}
        if session_manager_status:
            context["session_manager_status"] = session_manager_status
        super().__init__(f"会话管理器错误: {message}", context)


class SessionNotFoundError(PromptBuilderError):
    """当找不到指定的会话时抛出."""

    def __init__(
        self, session_key: str, platform_id: str | None = None, conv_id: str | None = None
    ) -> None:
        context = {"session_key": session_key}
        if platform_id:
            context["platform_id"] = platform_id
        if conv_id:
            context["conv_id"] = conv_id

        super().__init__(f"找不到会话实体UID为 '{session_key}' 的活跃会话档案", context)


class InvalidConversationIdError(PromptBuilderError):
    """当会话ID格式无效时抛出."""

    def __init__(self, conv_id: str, expected_format: str = "type.id") -> None:
        super().__init__(
            f"无效的会话ID格式 '{conv_id}'。它必须是 '{expected_format}' 格式。",
            {"conv_id": conv_id, "expected_format": expected_format},
        )


class ExternalInfoBuildError(PromptBuilderError):
    """当构建外部信息块失败时抛出."""

    def __init__(
        self, level: str, platform_id: str | None = None, error_details: str | None = None
    ) -> None:
        context = {"level": level}
        if platform_id:
            context["platform_id"] = platform_id
        if error_details:
            context["error_details"] = error_details

        super().__init__(f"构建 {level} 级别外部信息块失败", context)


class SystemPromptBuildError(PromptBuilderError):
    """当构建系统提示块失败时抛出."""

    def __init__(self, component: str, error_details: str | None = None) -> None:
        context = {"component": component}
        if error_details:
            context["error_details"] = error_details

        super().__init__(f"构建系统提示组件 '{component}' 失败", context)


class UserPromptBuildError(PromptBuilderError):
    """当构建用户提示块失败时抛出."""

    def __init__(self, component: str, error_details: str | None = None) -> None:
        context = {"component": component}
        if error_details:
            context["error_details"] = error_details

        super().__init__(f"构建用户提示组件 '{component}' 失败", context)


class SchemaBuildError(PromptBuilderError):
    """当构建响应schema失败时抛出."""

    def __init__(
        self, level: str, platform_id: str | None = None, error_details: str | None = None
    ) -> None:
        context = {"level": level}
        if platform_id:
            context["platform_id"] = platform_id
        if error_details:
            context["error_details"] = error_details

        super().__init__(f"构建 {level} 级别响应schema失败", context)


class TemplateFormatError(PromptBuilderError):
    """当模板格式化失败时抛出."""

    def __init__(self, template_name: str, missing_keys: list[str] | None = None) -> None:
        context = {"template_name": template_name}
        if missing_keys:
            context["missing_keys"] = missing_keys

        message = f"模板 '{template_name}' 格式化失败"
        if missing_keys:
            message += f"，缺少键: {', '.join(missing_keys)}"

        super().__init__(message, context)


class DependencyError(PromptBuilderError):
    """当依赖服务不可用或配置错误时抛出."""

    def __init__(
        self, service_name: str, dependency_type: str = "service", error_details: str | None = None
    ) -> None:
        context = {"service_name": service_name, "dependency_type": dependency_type}
        if error_details:
            context["error_details"] = error_details

        super().__init__(f"{dependency_type} '{service_name}' 不可用或配置错误", context)


class ValidationError(PromptBuilderError):
    """当数据验证失败时抛出."""

    def __init__(
        self,
        field_name: str,
        expected_type: str,
        actual_value: Any,
        error_details: str | None = None,
    ) -> None:
        context = {
            "field_name": field_name,
            "expected_type": expected_type,
            "actual_value": str(actual_value),
        }
        if error_details:
            context["error_details"] = error_details

        super().__init__(
            f"字段 '{field_name}' 验证失败，期望类型: {expected_type}，实际值: {actual_value}",
            context,
        )


class ConfigurationError(PromptBuilderError):
    """当配置相关错误发生时抛出."""

    def __init__(
        self,
        config_key: str | None = None,
        config_value: Any | None = None,
        error_details: str | None = None,
    ) -> None:
        context = {}
        if config_key:
            context["config_key"] = config_key
        if config_value is not None:
            context["config_value"] = str(config_value)
        if error_details:
            context["error_details"] = error_details

        message = "配置错误"
        if config_key:
            message += f" - 键: {config_key}"
        if config_value is not None:
            message += f"，值: {config_value}"

        super().__init__(message, context)


class ResourceUnavailableError(PromptBuilderError):
    """当所需资源不可用时抛出."""

    def __init__(
        self, resource_type: str, resource_id: str | None = None, error_details: str | None = None
    ) -> None:
        context = {"resource_type": resource_type}
        if resource_id:
            context["resource_id"] = resource_id
        if error_details:
            context["error_details"] = error_details

        message = f"{resource_type} 资源不可用"
        if resource_id:
            message += f": {resource_id}"

        super().__init__(message, context)


# 导出所有错误类
__all__ = [
    "ConfigurationError",
    "DependencyError",
    "ExternalInfoBuildError",
    "InvalidConversationIdError",
    "PromptBuilderError",
    "ResourceUnavailableError",
    "SchemaBuildError",
    "SessionManagerError",
    "SessionNotFoundError",
    "SystemPromptBuildError",
    "TemplateFormatError",
    "UserPromptBuildError",
    "ValidationError",
]



