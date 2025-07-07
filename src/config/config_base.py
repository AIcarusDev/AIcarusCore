import types
from dataclasses import MISSING, dataclass, fields
from typing import Any, TypeVar, Union, get_args, get_origin

import tomlkit  # 确保导入

T = TypeVar("T", bound="ConfigBase")

# TOML_DICT_TYPE = { ... } # 这个字典似乎在提供的ConfigBase中未直接使用，可以暂时保留或移除


@dataclass
class ConfigBase:
    """配置类的基类"""

    @classmethod
    def from_dict(cls: type[T], data: dict[str, Any]) -> T:
        if not isinstance(
            data, dict | tomlkit.items.Table | tomlkit.items.InlineTable
        ):  # 接受 tomlkit 的 Table 类型
            raise TypeError(
                f"Expected a dictionary-like object for {cls.__name__}, got {type(data).__name__}"
            )

        init_args: dict[str, Any] = {}

        for f in fields(cls):
            field_name = f.name
            if field_name.startswith("_"):
                continue

            if field_name not in data:
                if f.default is not MISSING or f.default_factory is not MISSING:
                    continue  # 使用 dataclass 的默认值
                else:
                    raise ValueError(
                        f"Missing required field in config data for '{cls.__name__}': '{field_name}'"
                    )

            value = data[field_name]
            field_type = f.type

            try:
                init_args[field_name] = cls._convert_field(
                    value, field_type, field_name, cls.__name__
                )  # 传递字段和类名用于更清晰的错误
            except TypeError as e:
                raise TypeError(f"Field '{cls.__name__}.{field_name}' has a type error: {e}") from e
            except ValueError as e:  # ConfigBase 中 _convert_field 可能抛出 ValueError
                raise ValueError(
                    f"Field '{cls.__name__}.{field_name}' has a value error: {e}"
                ) from e
            except Exception as e:
                raise RuntimeError(
                    f"Failed to convert field '{cls.__name__}.{field_name}' to target type: {e}"
                ) from e

        try:
            return cls(**init_args)
        except TypeError as e:  # 捕获dataclass构造时的TypeError，通常因为类型不匹配或缺少参数
            raise TypeError(
                f"Error constructing {cls.__name__} with arguments {init_args}: {e}"
            ) from e

    @classmethod
    def _convert_field(
        cls,
        value: object,
        field_type: type[Any],
        field_name_for_error: str,
        class_name_for_error: str,
    ) -> Union[None, bool, int, float, str, list, set, tuple, dict, "ConfigBase"]:
        # 如果值是 None 并且字段类型允许 None (例如 Optional[str] 或 str | None)
        origin_type = get_origin(field_type)
        type_args = get_args(field_type)

        if value is None:
            # 检查 field_type 是否是 Optional 或 Union 包含 NoneType
            is_optional = (origin_type is Union and type(None) in type_args) or (
                origin_type is None and type_args and type_args[0] is type(None)
            )  # Python 3.10+ NoneType
            if is_optional or field_type is Any:
                return None
            else:
                # 如果字段类型不接受 None，但值是 None，这通常是个问题，除非dataclass有默认值处理了
                # 但 from_dict 通常期望所有非默认值都被提供
                raise ValueError(
                    f"Field '{class_name_for_error}.{field_name_for_error}' is not Optional but received None value."
                )

        if (
            hasattr(field_type, "__mro__") and ConfigBase in field_type.__mro__
        ):  # 检查是否是ConfigBase的子类
            if not isinstance(value, dict | tomlkit.items.Table | tomlkit.items.InlineTable):
                raise TypeError(
                    f"Expected a dictionary-like object for nested ConfigBase '{field_name_for_error}' in '{class_name_for_error}', got {type(value).__name__}"
                )
            return field_type.from_dict(value)

        if origin_type in {list, set, tuple}:
            if not isinstance(value, list):  # TOML 数组总是被解析为 Python list
                raise TypeError(
                    f"Expected a list for field '{field_name_for_error}' in '{class_name_for_error}', got {type(value).__name__}"
                )

            element_type = type_args[0] if type_args else Any
            converted_elements = [
                cls._convert_field(
                    item, element_type, f"{field_name_for_error}[{i}]", class_name_for_error
                )
                for i, item in enumerate(value)
            ]

            if origin_type is list:
                return converted_elements
            elif origin_type is set:
                return set(converted_elements)
            elif origin_type is tuple:
                # 对于可变长度元组 (Tuple[X, ...])
                if len(type_args) == 2 and type_args[1] is Ellipsis:
                    return tuple(converted_elements)
                # 对于固定长度元组 (Tuple[X, Y, Z])
                elif len(value) != len(type_args):
                    raise ValueError(
                        f"Expected {len(type_args)} items for tuple field '{field_name_for_error}' in '{class_name_for_error}', got {len(value)}"
                    )
                # 重新转换，确保每个元素对应正确的类型参数（如果不同）
                return tuple(
                    cls._convert_field(
                        item, arg_type, f"{field_name_for_error}[{i}]", class_name_for_error
                    )
                    for i, (item, arg_type) in enumerate(zip(value, type_args, strict=False))
                )

        if origin_type is dict:
            if not isinstance(value, dict | tomlkit.items.Table | tomlkit.items.InlineTable):
                raise TypeError(
                    f"Expected a dictionary-like object for dict field '{field_name_for_error}' in '{class_name_for_error}', got {type(value).__name__}"
                )

            key_type = type_args[0] if len(type_args) > 0 else Any
            value_type = type_args[1] if len(type_args) > 1 else Any

            return {
                cls._convert_field(
                    k, key_type, f"{field_name_for_error}[key]", class_name_for_error
                ): cls._convert_field(
                    v,
                    value_type,
                    f"{field_name_for_error}[value for key {k}]",
                    class_name_for_error,
                )
                for k, v in value.items()
            }
        # 处理 Union 类型 (包括 Optional[T] 和 X | Y 语法)
        if origin_type in {Union, types.UnionType}:
            # 获取所有可能的类型
            possible_types = type_args
            # 尝试每个可能的类型
            for t in possible_types:
                if t is type(None) and value is None:
                    return None
                if t is not type(None):
                    try:
                        return cls._convert_field(
                            value, t, field_name_for_error, class_name_for_error
                        )
                    except (TypeError, ValueError):
                        continue
            raise TypeError(
                f"Value '{value}' could not be converted to any of the union types: {possible_types}"
            )

        if field_type is Any or isinstance(value, field_type):
            return value

        try:
            # 对于 bool("False") 会是 True，需要特殊处理
            if field_type is bool and isinstance(value, str):
                if value.lower() == "true":
                    return True
                if value.lower() == "false":
                    return False
                raise ValueError(
                    f"Cannot convert string '{value}' to bool for field '{field_name_for_error}'. Use 'true' or 'false'."
                )
            return field_type(value)
        except (ValueError, TypeError) as e:
            type_name = getattr(field_type, "__name__", str(field_type))
            raise TypeError(
                f"Cannot convert {type(value).__name__} '{str(value)[:50]}' to {type_name} for field '{field_name_for_error}' in '{class_name_for_error}'. Error: {e}"
            ) from e

    def __str__(self) -> str:
        """返回配置类的字符串表示"""
        return f"{self.__class__.__name__}({', '.join(f'{f.name}={getattr(self, f.name)!r}' for f in fields(self))})"
