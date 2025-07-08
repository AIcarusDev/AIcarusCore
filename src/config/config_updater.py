# filepath: i:\\github\\FengM\\AIcarusCore\\src\\config\\config_updater.py

import os
from collections.abc import Callable, MutableMapping, MutableSequence
from typing import Any

import tomlkit
from src.common.custom_logging.logging_config import get_logger
from tomlkit.items import AoT, Array, Table
from tomlkit.items import Item as TomlItem

from .config_io import ConfigIOHandler
from .config_paths import EXPECTED_CONFIG_VERSION

logger = get_logger(__name__)


# --- 配置合并与环境变量替换 ---
def _sophisticated_merge_configs(
    new_template_base_doc: Table | TomlItem,
    old_user_config_doc: Table | TomlItem,
) -> None:
    """深入融合新模板和旧用户配置的核心逻辑.

    这个函数会遍历新模板的每个键，如果旧配置中有对应的键，
    则尝试将旧配置的值合并到新模板中，保留新模板的结构和版本号.

    Args:
        new_template_base_doc (Table | TomlItem): 新模板的基础文档，
        old_user_config_doc (Table | TomlItem): 旧用户配置的基础文档，
    """
    if not isinstance(new_template_base_doc, MutableMapping) or not isinstance(
        old_user_config_doc, MutableMapping
    ):
        return

    for key, template_value in new_template_base_doc.items():
        if key == "inner":
            if (
                isinstance(old_user_config_doc, dict | Table)
                and key in old_user_config_doc
                and isinstance(template_value, dict | Table)
                and isinstance(old_user_config_doc[key], dict | Table)
            ):
                # 保留新模板的 inner.version，但其他 inner 下的字段可以尝试从旧配置合并（如果需要）
                # 目前的逻辑是，新模板的 inner 内容优先
                template_inner = template_value
                old_inner = old_user_config_doc[key]
                if "version" in template_inner and "version" in old_inner:
                    logger.debug(
                        f"  合并提示：[inner] 表将使用新模板的版本号: {template_inner['version']} "
                        f"(旧版本是: {old_inner['version']})"
                    )

            continue

        if isinstance(old_user_config_doc, dict | Table) and key in old_user_config_doc:
            old_value = old_user_config_doc[key]
            if isinstance(template_value, Table) and isinstance(old_value, Table):
                _sophisticated_merge_configs(template_value, old_value)
            elif isinstance(template_value, AoT) and isinstance(old_value, AoT):
                new_aot_items_from_template = template_value.body
                old_aot_items_from_user = old_value.body

                final_aot_entries = []

                for i, template_aot_table_item in enumerate(new_aot_items_from_template):
                    if isinstance(template_aot_table_item, Table):
                        merged_aot_table_item = tomlkit.parse(
                            tomlkit.dumps(template_aot_table_item)
                        )

                        if i < len(old_aot_items_from_user):
                            user_aot_table_item = old_aot_items_from_user[i]
                            if isinstance(user_aot_table_item, Table):
                                _sophisticated_merge_configs(
                                    merged_aot_table_item, user_aot_table_item
                                )

                        final_aot_entries.append(merged_aot_table_item)
                    else:
                        final_aot_entries.append(
                            template_aot_table_item.copy()
                            if hasattr(template_aot_table_item, "copy")
                            else template_aot_table_item
                        )

                template_value.clear()
                for entry_table in final_aot_entries:
                    template_value.append(entry_table)
                logger.debug(f"  合并数组表 (AoT): '{key}' 已根据新模板结构融合旧值。")

            elif isinstance(template_value, Array) and isinstance(old_value, Array):
                new_template_base_doc[key] = old_value.copy()
                logger.debug(f"  合并普通数组: '{key}' 已使用旧配置中的值。")

            elif not isinstance(template_value, Table | AoT | Array) and not isinstance(
                old_value, Table | AoT | Array
            ):
                new_template_base_doc[key] = old_value
            else:
                logger.debug(
                    f"  合并提示：键 '{key}' 在新旧配置中类型严重不匹配（例如表与简单值），"
                    f"保留新模板的结构/值。模板类型: {type(template_value)}, "
                    f"旧值类型: {type(old_value)}"
                )
    if isinstance(old_user_config_doc, dict | Table):
        for old_key in old_user_config_doc:
            if old_key not in new_template_base_doc:
                logger.debug(f"  合并提示：旧配置中的键 '{old_key}' 在新模板中已“失宠”，将被忽略。")


def substitute_env_vars_recursive(  # 改为公开函数，因为 config_manager 可能也需要直接调用
    config_node: MutableMapping[str, Any] | MutableSequence[Any] | TomlItem,
) -> None:
    """递归地替换配置节点中的环境变量占位符.

    这个函数会遍历配置节点，如果发现值是以 "ENV_" 开头的字符串，
    则尝试从环境变量中获取对应的值，并替换掉占位符.

    Args:
        config_node (MutableMapping | MutableSequence | TomlItem): 配置节点，可以是字典、列表
            或其他 tomlkit 项目.
    """
    if isinstance(config_node, MutableMapping):  # 如果是字典或 TOML 表 (Table, InlineTable)
        # 用 list(config_node.items()) 是为了在迭代时也能安全地修改字典/表
        for key, value in list(config_node.items()):  # value 可能是 TomlItem
            if isinstance(value, str) and value.startswith("ENV_"):  # Python 原生 str
                env_var_name = value[4:]
                env_value = os.getenv(env_var_name)
                if env_value is not None:
                    processed_value: Any
                    if env_value.lower() == "true":
                        processed_value = tomlkit.boolean(True)
                    elif env_value.lower() == "false":
                        processed_value = tomlkit.boolean(False)
                    else:
                        try:
                            processed_value = tomlkit.integer(int(env_value))
                        except ValueError:
                            try:
                                processed_value = tomlkit.float_(float(env_value))
                            except ValueError:
                                if (env_value.startswith("[") and env_value.endswith("]")) or (
                                    env_value.startswith("{") and env_value.endswith("}")
                                ):
                                    try:
                                        parsed_item = tomlkit.parse(f"temp_key = {env_value}")
                                        processed_value = parsed_item[
                                            "temp_key"
                                        ]  # 这会是 tomlkit item
                                    except Exception:
                                        processed_value = tomlkit.string(env_value)
                                else:
                                    processed_value = tomlkit.string(env_value)
                    config_node[key] = processed_value
                    logger.debug(f"  环境变量替换：配置项 '{key}' 已从 '{env_var_name}' 加载。")
                else:
                    logger.warning(
                        f"  环境变量警告：配置项 '{key}' 想用环境变量 '{env_var_name}', "
                        f"  但它好像没设置哦。保留占位符 '{value}'。"
                    )
            elif isinstance(
                value, MutableMapping | MutableSequence | TomlItem
            ):  # 如果值是嵌套结构或者其他TomlItem
                # 对于 TomlItem, 如果它是 StringItem 且值为 "ENV_...", 也应处理
                if isinstance(value, tomlkit.items.String) and value.value.startswith("ENV_"):
                    env_var_name = value.value[4:]
                    env_value = os.getenv(env_var_name)
                    if env_value is not None:
                        processed_env_val: Any
                        if env_value.lower() == "true":
                            processed_env_val = tomlkit.boolean(True)
                        elif env_value.lower() == "false":
                            processed_env_val = tomlkit.boolean(False)
                        else:
                            try:
                                processed_env_val = tomlkit.integer(int(env_value))
                            except ValueError:
                                try:
                                    processed_env_val = tomlkit.float_(float(env_value))
                                except ValueError:
                                    if (env_value.startswith("[") and env_value.endswith("]")) or (
                                        env_value.startswith("{") and env_value.endswith("}")
                                    ):
                                        try:
                                            parsed_item = tomlkit.parse(f"temp_key = {env_value}")
                                            processed_env_val = parsed_item["temp_key"]
                                        except Exception:
                                            processed_env_val = tomlkit.string(env_value)
                                    else:
                                        processed_env_val = tomlkit.string(env_value)
                        config_node[key] = processed_env_val
                        logger.debug(
                            f"  环境变量替换：配置项 '{key}' (TomlString) 已从 "
                            f"'{env_var_name}' 加载。"
                        )
                    else:
                        logger.warning(
                            f"  环境变量警告：配置项 '{key}' (TomlString) 想用"
                            f"环境变量 '{env_var_name}', 但它好像没设置哦。保留原样。"
                        )
                else:  # 否则，如果是容器类型，递归进去
                    substitute_env_vars_recursive(value)

    elif isinstance(config_node, MutableSequence):  # 如果是列表或 TOML 数组 (Array)
        for i, item in enumerate(config_node):  # item 可能是 TomlItem
            if isinstance(item, str) and item.startswith("ENV_"):  # Python 原生 str
                env_var_name = item[4:]
                env_value = os.getenv(env_var_name)
                if env_value is not None:
                    processed_list_item: Any
                    if env_value.lower() == "true":
                        processed_list_item = tomlkit.boolean(True)  # 在数组中也用 tomlkit item
                    elif env_value.lower() == "false":
                        processed_list_item = tomlkit.boolean(False)
                    else:
                        try:
                            processed_list_item = tomlkit.integer(int(env_value))
                        except ValueError:
                            try:
                                processed_list_item = tomlkit.float_(float(env_value))
                            except ValueError:
                                processed_list_item = tomlkit.string(env_value)  # 默认是字符串
                    config_node[i] = processed_list_item
                    logger.debug(f"  环境变量替换：列表索引 {i} 的元素已从 '{env_var_name}' 加载。")
                else:
                    logger.warning(
                        f"  环境变量警告：列表索引 {i} 的元素想用环境变量 '{env_var_name}', "
                        f"但它也没设置。保留占位符。"
                    )
            elif isinstance(item, MutableMapping | MutableSequence | TomlItem):
                # 对于 TomlItem, 如果它是 StringItem 且值为 "ENV_...", 也应处理
                if isinstance(item, tomlkit.items.String) and item.value.startswith("ENV_"):
                    env_var_name = item.value[4:]
                    env_value = os.getenv(env_var_name)
                    if env_value is not None:
                        processed_list_env_item: Any
                        if env_value.lower() == "true":
                            processed_list_env_item = tomlkit.boolean(True)
                        elif env_value.lower() == "false":
                            processed_list_env_item = tomlkit.boolean(False)
                        else:
                            try:
                                processed_list_env_item = tomlkit.integer(int(env_value))
                            except ValueError:
                                try:
                                    processed_list_env_item = tomlkit.float_(float(env_value))
                                except ValueError:
                                    if (env_value.startswith("[") and env_value.endswith("]")) or (
                                        env_value.startswith("{") and env_value.endswith("}")
                                    ):
                                        try:
                                            parsed_item = tomlkit.parse(f"temp_key = {env_value}")
                                            processed_list_env_item = parsed_item["temp_key"]
                                        except Exception:
                                            processed_list_env_item = tomlkit.string(env_value)
                                    else:
                                        processed_list_env_item = tomlkit.string(env_value)
                        config_node[i] = processed_list_env_item
                        logger.debug(
                            f"  环境变量替换：列表索引 {i} (TomlString) 的元素"
                            f"已从 '{env_var_name}' 加载。"
                        )
                    else:
                        logger.warning(
                            f"  环境变量警告：列表索引 {i} (TomlString) 的元素想用"
                            f"环境变量 '{env_var_name}', 但它也没设置。保留原样。"
                        )
                else:  # 否则，如果是容器类型，递归进去
                    substitute_env_vars_recursive(item)
    # 如果 config_node 是一个单独的 TomlItem (例如 StringItem) 且不是容器
    elif isinstance(config_node, tomlkit.items.String) and config_node.value.startswith("ENV_"):
        # 这种情况通常在外层容器的迭代中处理，但作为一种保险或直接调用时的处理
        # 这里我们不能直接修改 config_node，因为它是被传入的，可能需要返回新值
        # 但我们约定此函数是原地修改，所以这种情况可能较少直接触发，更多是在容器内处理
        logger.warning(
            f"  环境变量替换：尝试替换独立的 TomlString "
            f"'{config_node.value}'，但这通常在容器内完成。"
        )


# --- 核心配置更新检查逻辑 ---
def perform_config_update_check(
    io_handler: ConfigIOHandler, prompt_user_and_exit_fn: Callable[[str], None]
) -> bool:
    """检查和更新配置文件的核心逻辑.

    这个函数会检查运行时配置文件是否存在，如果不存在则从模板创建一个.
    如果存在，则检查版本号是否匹配，如果不匹配则进行合并和更新操作.

    Args:
        io_handler (ConfigIOHandler): 用于处理配置文件的输入输出操作.
        prompt_user_and_exit_fn (Callable[[str], None]): 用于提示用户并退出程序的函数.

    Returns:
        bool: 如果配置文件被创建或更新，则返回 True，否则返回 False.
    """
    logger.debug("开始仔细检查和更新配置文件，请稍等片刻，小猫正在施展魔法...")

    config_was_created_or_updated: bool = False

    if not io_handler.template_exists():
        message = (
            f"天哪！配置文件模板 '{io_handler.template_path}' 居然不见了！程序没法继续了，嘤嘤嘤..."
        )
        logger.critical(message)
        raise FileNotFoundError(message)

    if not io_handler.runtime_config_exists():
        logger.info(
            f"运行时配置文件 '{io_handler.runtime_path}' 好像还没出生，让我从模板创造一个吧！"
        )
        if io_handler.copy_template_to_runtime():
            config_was_created_or_updated = True
            logger.info("新的运行时配置文件已成功创建！撒花！✿✿ヽ(°▽°)ノ✿")
            # 新创建的配置文件不需要合并，但可能需要环境变量替换
            newly_created_config = io_handler.load_toml_file(io_handler.runtime_path)
            if newly_created_config:
                logger.debug("对新创建的配置文件进行环境变量替换...")
                substitute_env_vars_recursive(newly_created_config)  # 确保对 tomlkit 文档操作
                io_handler.save_toml_file(io_handler.runtime_path, newly_created_config)
        else:
            message = f"从模板创建配置文件 '{io_handler.runtime_path}' 失败！请检查权限和路径。"
            logger.critical(message)
            prompt_user_and_exit_fn(message)
        return config_was_created_or_updated  # 返回 True，因为新创建了，用户可能需要检查

    logger.debug(f"发现现有的运行时配置文件: '{io_handler.runtime_path}'，让我瞅瞅里面写了啥。")
    # actual_config 是用户当前的、可能版本较低的配置
    actual_config_doc = io_handler.load_toml_file(io_handler.runtime_path)

    if actual_config_doc is None:
        logger.warning(
            f"哎呀！现有的运行时配置文件 '{io_handler.runtime_path}' 可能坏掉了，读不出来。"
        )
        io_handler.backup_runtime_config(prefix="broken_")
        if io_handler.copy_template_to_runtime():
            config_was_created_or_updated = True
            logger.info(
                "已从模板重新创建了配置文件。你可能需要从标记为 'broken_' 的备份里找回之前的设置。"
            )
            # 同样，对新创建的进行环境变量替换
            recreated_config = io_handler.load_toml_file(io_handler.runtime_path)
            if recreated_config:
                logger.debug("对重新创建的配置文件进行环境变量替换...")
                substitute_env_vars_recursive(recreated_config)
                io_handler.save_toml_file(io_handler.runtime_path, recreated_config)
        else:
            message = (
                f"雪上加霜！从模板重新创建损坏的配置文件 '{io_handler.runtime_path}' 也失败了！"
            )
            logger.critical(message)
            prompt_user_and_exit_fn(message)
        return config_was_created_or_updated

    # 加载模板配置，准备比对版本号
    template_config_doc = io_handler.load_toml_file(io_handler.template_path)
    if template_config_doc is None:
        message = f"致命错误！无法加载模板配置文件 '{io_handler.template_path}' ！"
        logger.critical(message)
        raise RuntimeError(message)

    current_template_version = str(
        template_config_doc.get("inner", {}).get("version", EXPECTED_CONFIG_VERSION)
    )
    actual_runtime_version = str(actual_config_doc.get("inner", {}).get("version", "未知版本"))

    if actual_runtime_version == current_template_version:
        logger.info(f"版本一致！配置文件 (v{actual_runtime_version}) 无需更新。")
        # 版本一致，但仍然需要处理环境变量占位符，以防上次启动时某些环境变量未设置
        logger.debug("即使版本一致，也检查一下运行时配置的环境变量占位符...")
        substitute_env_vars_recursive(actual_config_doc)  # 对用户当前的配置进行环境变量替换
        io_handler.save_toml_file(
            io_handler.runtime_path, actual_config_doc
        )  # 保存可能替换后的结果
        return False  # 版本相同，结构不更新，返回 False

    logger.info(
        f"版本不一致！运行时配置 (v{actual_runtime_version}) 和"
        f"模板 (v{current_template_version}) 版本对不上。准备更新..."
    )

    if io_handler.backup_runtime_config(prefix="pre_update_"):
        # 创建一个新配置的基础，它是当前模板的一个“深喉”副本
        # 我们将在这个副本上操作，然后用它覆盖运行时文件
        # 使用 tomlkit.dumps 和 tomlkit.parse 来实现深拷贝，确保所有 tomlkit 特性被保留
        new_config_base_doc = tomlkit.parse(tomlkit.dumps(template_config_doc))

        logger.info("开始施展魔法，把旧配置里的好东西用“爱液”融合到新模板结构中...")
        _sophisticated_merge_configs(
            new_config_base_doc, actual_config_doc
        )  # actual_config_doc 是旧的、已加载的配置内容

        logger.debug("融合完毕，现在对新的配置文档进行环境变量替换...")
        substitute_env_vars_recursive(new_config_base_doc)  # 在保存前替换环境变量

        if io_handler.save_toml_file(io_handler.runtime_path, new_config_base_doc):
            logger.info(f"配置文件已成功更新并融合旧值到 '{io_handler.runtime_path}'！完美！")
            config_was_created_or_updated = True
        else:
            logger.error(
                f"致命错误！保存融合后的配置文件 '{io_handler.runtime_path}' 失败了！"
                f"程序可能要出问题了！"
            )
            # 此时可以考虑是否要恢复备份，或者强烈建议用户手动检查
    else:
        logger.critical(
            "致命错误！备份旧的运行时配置文件失败了！不敢继续更新了，怕弄丢主人的宝贝！"
        )

    return config_was_created_or_updated
