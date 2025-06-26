# filepath: i:\\github\\FengM\\AIcarusCore\\src\\config\\config_updater.py
# 嘿，我是 config_updater.py！(づ｡◕‿‿◕｡)づ ✨
# 我的工作就是确保你的配置文件永远是最新的、最棒的！
# 版本检查、内容合并、环境变量替换，这些魔法都交给我吧！
# 小色猫注入了新的“淫力”，现在我更懂得如何“深入”和“融合”了哦~ ❤️

import os
from collections.abc import Callable, MutableMapping, MutableSequence
from typing import Any

import tomlkit  # TOML 文件处理，我的好帮手！现在用它来玩更刺激的play！
from tomlkit.items import AoT, Array, Table  # 导入这些销魂小组件
from tomlkit.items import Item as TomlItem

from src.common.custom_logging.logger_manager import get_logger

from .config_io import ConfigIOHandler  # 从隔壁 config_io 借工具人 ConfigIOHandler
from .config_paths import EXPECTED_CONFIG_VERSION  # 版本号标准得听 config_paths 的

logger = get_logger("AIcarusCore.config_updater")

# --- 配置合并与环境变量替换 ---


# 小猫的娇喘：这个函数被我用“爱液”彻底改造了哦~ 它现在非常懂得“深喉”的技巧！
def _sophisticated_merge_configs(
    new_template_base_doc: Table
    | TomlItem,  # 这是新模板的身体，我们要往里面注入灵魂 (tomlkit.Table 或更具体的 TomlItem)
    old_user_config_doc: Table | TomlItem,  # 这是主人你旧配置的精华 (tomlkit.Table 或更具体的 TomlItem)
) -> None:
    """
    以 new_template_base_doc (新模板结构) 为基础，递归地将 old_user_config_doc (旧用户配置值) 中的值合并进去。
    这个过程非常“深入”，会直接修改 new_template_base_doc 哦，主人要小心~
    特别强化了对数组表 (AoT) 和多行字符串的处理，确保它们既能保留用户数据，又能适应新模板的结构。
    """
    if not isinstance(new_template_base_doc, MutableMapping) or not isinstance(old_user_config_doc, MutableMapping):
        # 如果不是字典类的东西 (比如直接传了个 String 进去)，那就没法按键合并了，直接不处理
        # 这种情况通常发生在递归到叶子节点，而类型不匹配时，外层逻辑会处理
        return

    # 遍历新模板的每一个“敏感点” (键)
    for key, template_value in new_template_base_doc.items():
        # 'inner' 表是小猫的禁脔，要特殊对待，特别是里面的版本号，不能随便被旧的覆盖哦！
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
                        f"  合并提示：[inner] 表将使用新模板的版本号: {template_inner['version']} (旧版本是: {old_inner['version']})"
                    )
                # 如果需要合并 inner 表内其他字段，可以在这里添加逻辑
                # for inner_key, old_inner_value in old_inner.items():
                #    if inner_key != "version" and inner_key not in template_inner:
                #        template_inner[inner_key] = old_inner_value # 示例：添加旧配置 inner 中有而新模板 inner 中没有的字段
            continue  # 'inner' 表的特殊处理到此为止，不进行下面的通用合并逻辑

        if isinstance(old_user_config_doc, dict | Table) and key in old_user_config_doc:
            old_value = old_user_config_doc[key]

            # 姿势一：如果新旧两边都是“餐盘” (Table)，那就深入进去，继续“舔舐”里面的每一道菜
            if isinstance(template_value, Table) and isinstance(old_value, Table):
                _sophisticated_merge_configs(template_value, old_value)  # 递归深入，好刺激！

            # 姿势二：如果新旧两边都是“群P盛宴” (AoT - Array of Tables)，这是最销魂的部分！
            elif isinstance(template_value, AoT) and isinstance(old_value, AoT):
                # 我们以新模板的 AoT (template_value) 为蓝本，包括它的条目数量和每个条目的结构。
                # 然后，我们会按顺序从旧配置的 AoT (old_value) 中取出对应条目的“精华”，注入到新蓝本中。
                new_aot_items_from_template = template_value.body  # 这是模板AoT里的所有Table Item
                old_aot_items_from_user = old_value.body  # 这是用户旧AoT里的所有Table Item

                final_aot_entries = []  # 准备好承载我们融合后的“爱液”

                # 遍历模板AoT的每一个“原型”
                for i, template_aot_table_item in enumerate(new_aot_items_from_template):
                    if isinstance(template_aot_table_item, Table):
                        # 创建一个当前模板条目的“深喉”副本，作为融合的基础
                        merged_aot_table_item = tomlkit.parse(tomlkit.dumps(template_aot_table_item))

                        if i < len(old_aot_items_from_user):
                            user_aot_table_item = old_aot_items_from_user[i]
                            if isinstance(user_aot_table_item, Table):
                                # 用旧用户条目的数据来“滋养”这个模板条目的副本
                                _sophisticated_merge_configs(merged_aot_table_item, user_aot_table_item)
                        # else: 用户旧AoT中没有对应索引的条目了，merged_aot_table_item 保持为模板条目的样子

                        final_aot_entries.append(merged_aot_table_item)
                    else:
                        # 如果模板AoT里的东西不是Table（理论上不应该），就直接用模板的吧，安全第一
                        final_aot_entries.append(
                            template_aot_table_item.copy()
                            if hasattr(template_aot_table_item, "copy")
                            else template_aot_table_item
                        )

                # 用融合后的“爱液”们重新构建这个AoT，让它焕发新生！
                # 直接修改 template_value (它是原 new_template_base_doc 中 AoT 的引用)
                template_value.clear()  # 先把它吸干抹净！
                for entry_table in final_aot_entries:
                    template_value.append(entry_table)  # 再把新的精华一个个注入！
                logger.debug(f"  合并数组表 (AoT): '{key}' 已根据新模板结构融合旧值。")

            # 姿势三：如果新旧两边都是普通的“珍珠串” (Array)，小猫觉得主人的旧珍珠串更合心意
            elif isinstance(template_value, Array) and isinstance(old_value, Array):
                # 对于普通数组，我们直接用旧的覆盖新的，因为用户自定义的列表内容通常更重要。
                # tomlkit 的 Array 支持 copy()
                new_template_base_doc[key] = old_value.copy()
                logger.debug(f"  合并普通数组: '{key}' 已使用旧配置中的值。")

            # 姿势四：如果它们是简单类型（字符串、数字、布尔等），并且类型兼容，就用旧的“爱液”覆盖新的
            # 这里要确保 template_value 和 old_value 都是 tomlkit 的 Item 类型，或者能被 tomlkit.item() 正确处理
            elif not isinstance(template_value, Table | AoT | Array) and not isinstance(old_value, Table | AoT | Array):
                # old_value 已经是 tomlkit item 了 (因为它来自解析后的 tomlkit.TOMLDocument)
                # 直接赋值，tomlkit 会处理好类型和格式，包括多行字符串的风骚哦~
                new_template_base_doc[key] = old_value
                # logger.debug(f"  合并简单值: [{key}] = {str(old_value.unwrap() if hasattr(old_value, 'unwrap') else old_value)[:50]}") # 使用unwrap获取原始值再截断
            else:
                # 类型不匹配（比如，旧的是个简单值，新的是个表），这种“体位”太奇怪了！
                # 我们通常会保留新模板的结构和值，不乱来哦，主人~
                logger.debug(
                    f"  合并提示：键 '{key}' 在新旧配置中类型严重不匹配（例如表与简单值），保留新模板的结构/值。模板类型: {type(template_value)}, 旧值类型: {type(old_value)}"
                )
        # else: key 只在 new_template_base_doc 中存在（即新模板新增的），不在 old_user_config_doc 中，
        #       那么它会自然保留在 new_template_base_doc 中，无需操作。小猫的新玩具当然要留下啦！

    # 最后，检查一下旧配置里有没有新模板里已经“抛弃”的键，并友情提示一下主人
    if isinstance(old_user_config_doc, dict | Table):
        for old_key in old_user_config_doc:
            if old_key not in new_template_base_doc:
                logger.debug(f"  合并提示：旧配置中的键 '{old_key}' 在新模板中已“失宠”，将被忽略。")


def substitute_env_vars_recursive(  # 改为公开函数，因为 config_manager 可能也需要直接调用
    config_node: MutableMapping[str, Any] | MutableSequence[Any] | TomlItem,  # 接受更广泛的 tomlkit 类型
) -> None:
    """
    递归地扫描配置，把所有 "ENV_YOUR_VARIABLE" 这样的占位符替换成真正的环境变量值。
    就像一个勤劳的小蜜蜂，把花蜜（环境变量）采到配置的每个角落！🐝
    支持字典、列表、以及 tomlkit 的 Table 和 Array 的递归处理哦。
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
                                        processed_value = parsed_item["temp_key"]  # 这会是 tomlkit item
                                    except Exception:
                                        processed_value = tomlkit.string(env_value)
                                else:
                                    processed_value = tomlkit.string(env_value)
                    config_node[key] = processed_value
                    logger.debug(f"  环境变量替换：配置项 '{key}' 已从 '{env_var_name}' 加载。")
                else:
                    logger.warning(
                        f"  环境变量警告：配置项 '{key}' 想用环境变量 '{env_var_name}', 但它好像没设置哦。保留占位符 '{value}'。"
                    )
            elif isinstance(value, MutableMapping | MutableSequence | TomlItem):  # 如果值是嵌套结构或者其他TomlItem
                # 对于 TomlItem, 如果它是 StringItem 且值为 "ENV_...", 也应处理
                if isinstance(value, tomlkit.items.String) and value.value.startswith("ENV_"):
                    env_var_name = value.value[4:]
                    env_value = os.getenv(env_var_name)
                    if env_value is not None:
                        # ... (与上面 Python str 类似的环境变量处理逻辑, 结果是 tomlkit item)
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
                        logger.debug(f"  环境变量替换：配置项 '{key}' (TomlString) 已从 '{env_var_name}' 加载。")
                    else:
                        logger.warning(
                            f"  环境变量警告：配置项 '{key}' (TomlString) 想用环境变量 '{env_var_name}', 但它好像没设置哦。保留原样。"
                        )
                else:  # 否则，如果是容器类型，递归进去
                    substitute_env_vars_recursive(value)

    elif isinstance(config_node, MutableSequence):  # 如果是列表或 TOML 数组 (Array)
        for i, item in enumerate(config_node):  # item 可能是 TomlItem
            if isinstance(item, str) and item.startswith("ENV_"):  # Python 原生 str
                env_var_name = item[4:]
                env_value = os.getenv(env_var_name)
                if env_value is not None:
                    # ... (与上面 Python str 类似的环境变量处理逻辑, 结果是 tomlkit item 或 Python 类型)
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
                        f"  环境变量警告：列表索引 {i} 的元素想用环境变量 '{env_var_name}', 但它也没设置。保留占位符。"
                    )
            elif isinstance(item, MutableMapping | MutableSequence | TomlItem):
                # 对于 TomlItem, 如果它是 StringItem 且值为 "ENV_...", 也应处理
                if isinstance(item, tomlkit.items.String) and item.value.startswith("ENV_"):
                    env_var_name = item.value[4:]
                    env_value = os.getenv(env_var_name)
                    if env_value is not None:
                        # ... (与上面 Python str 类似的环境变量处理逻辑, 结果是 tomlkit item)
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
                        logger.debug(f"  环境变量替换：列表索引 {i} (TomlString) 的元素已从 '{env_var_name}' 加载。")
                    else:
                        logger.warning(
                            f"  环境变量警告：列表索引 {i} (TomlString) 的元素想用环境变量 '{env_var_name}', 但它也没设置。保留原样。"
                        )
                else:  # 否则，如果是容器类型，递归进去
                    substitute_env_vars_recursive(item)
    # 如果 config_node 是一个单独的 TomlItem (例如 StringItem) 且不是容器
    elif isinstance(config_node, tomlkit.items.String) and config_node.value.startswith("ENV_"):
        # 这种情况通常在外层容器的迭代中处理，但作为一种保险或直接调用时的处理
        # 这里我们不能直接修改 config_node，因为它是被传入的，可能需要返回新值
        # 但我们约定此函数是原地修改，所以这种情况可能较少直接触发，更多是在容器内处理
        logger.warning(f"  环境变量替换：尝试替换独立的 TomlString '{config_node.value}'，但这通常在容器内完成。")


# --- 核心配置更新检查逻辑 ---


def perform_config_update_check(io_handler: ConfigIOHandler, prompt_user_and_exit_fn: Callable[[str], None]) -> bool:
    """
    执行配置文件的核心检查和更新流程。小猫我可是专业的！而且现在更“淫荡”了！
    返回一个布尔值，告诉你配置是不是刚刚新鲜出炉或者焕然一新了。
    """
    logger.debug("开始仔细检查和更新配置文件，请稍等片刻，小猫正在施展魔法...")

    config_was_created_or_updated: bool = False

    if not io_handler.template_exists():
        message = f"天哪！配置文件模板 '{io_handler.template_path}' 居然不见了！程序没法继续了，嘤嘤嘤..."
        logger.critical(message)
        raise FileNotFoundError(message)

    if not io_handler.runtime_config_exists():
        logger.info(f"运行时配置文件 '{io_handler.runtime_path}' 好像还没出生，让我从模板创造一个吧！")
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
            message = f"糟糕！从模板创建运行时配置文件 '{io_handler.runtime_path}' 失败了！快检查下权限和路径吧。"
            logger.critical(message)
            prompt_user_and_exit_fn(message)
        return config_was_created_or_updated  # 返回 True，因为新创建了，用户可能需要检查

    logger.debug(f"发现现有的运行时配置文件: '{io_handler.runtime_path}'，让我瞅瞅里面写了啥。")
    # actual_config 是用户当前的、可能版本较低的配置
    actual_config_doc = io_handler.load_toml_file(io_handler.runtime_path)

    if actual_config_doc is None:
        logger.warning(f"哎呀！现有的运行时配置文件 '{io_handler.runtime_path}' 可能坏掉了，读不出来。")
        io_handler.backup_runtime_config(prefix="broken_")
        if io_handler.copy_template_to_runtime():
            config_was_created_or_updated = True
            logger.info("已从模板重新创建了配置文件。你可能需要从那个标记为 'broken_' 的备份里找回你之前的设置哦。")
            # 同样，对新创建的进行环境变量替换
            recreated_config = io_handler.load_toml_file(io_handler.runtime_path)
            if recreated_config:
                logger.debug("对重新创建的配置文件进行环境变量替换...")
                substitute_env_vars_recursive(recreated_config)
                io_handler.save_toml_file(io_handler.runtime_path, recreated_config)
        else:
            message = f"雪上加霜！从模板重新创建损坏的配置文件 '{io_handler.runtime_path}' 也失败了！"
            logger.critical(message)
            prompt_user_and_exit_fn(message)
        return config_was_created_or_updated

    # 加载模板配置，准备比对版本号
    template_config_doc = io_handler.load_toml_file(io_handler.template_path)
    if template_config_doc is None:
        message = f"致命错误！无法加载模板配置文件 '{io_handler.template_path}' 来比较版本！这不应该发生啊！"
        logger.critical(message)
        raise RuntimeError(message)

    current_template_version = str(template_config_doc.get("inner", {}).get("version", EXPECTED_CONFIG_VERSION))
    actual_runtime_version = str(actual_config_doc.get("inner", {}).get("version", "未知版本"))

    if actual_runtime_version == current_template_version:
        logger.info(
            f"版本一致！运行时配置 (v{actual_runtime_version}) 和模板 (v{current_template_version}) 是好朋友，不用大动干戈更新结构啦。"
        )
        # 版本一致，但仍然需要处理环境变量占位符，以防上次启动时某些环境变量未设置
        logger.debug("即使版本一致，也检查一下运行时配置的环境变量占位符...")
        substitute_env_vars_recursive(actual_config_doc)  # 对用户当前的配置进行环境变量替换
        io_handler.save_toml_file(io_handler.runtime_path, actual_config_doc)  # 保存可能替换后的结果
        return False  # 版本相同，结构不更新，返回 False

    logger.info(
        f"版本不一致！运行时配置 (v{actual_runtime_version}) 和模板 (v{current_template_version}) 版本对不上。准备用小猫的“深喉”技术更新..."
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
            logger.info(f"配置文件已成功更新并融合旧值到 '{io_handler.runtime_path}'！完美！小猫爽翻了！")
            config_was_created_or_updated = True
        else:
            logger.error(f"致命错误！保存融合后的配置文件 '{io_handler.runtime_path}' 失败了！程序可能要出问题了！")
            # 此时可以考虑是否要恢复备份，或者强烈建议用户手动检查
    else:
        logger.critical("致命错误！备份旧的运行时配置文件失败了！不敢继续更新了，怕弄丢主人的宝贝！")

    return config_was_created_or_updated
