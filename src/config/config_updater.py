# filepath: i:\\github\\FengM\\AIcarusCore\\src\\config\\config_updater.py
# 嘿，我是 config_updater.py！(づ｡◕‿‿◕｡)づ ✨
# 我的工作就是确保你的配置文件永远是最新的、最棒的！
# 版本检查、内容合并、环境变量替换，这些魔法都交给我吧！

import os
from collections.abc import Callable
from typing import Any

import tomlkit  # TOML 文件处理，我的好帮手！

from src.common.custom_logging.logger_manager import get_logger

from .config_io import ConfigIOHandler  # 从隔壁 config_io 借工具人 ConfigIOHandler
from .config_paths import EXPECTED_CONFIG_VERSION  # 版本号标准得听 config_paths 的

logger = get_logger("AIcarusCore.config_updater")

# --- 配置合并与环境变量替换 ---


def _merge_configs_recursive(
    target_config: dict[str, Any] | tomlkit.items.Table,
    old_values_source: dict[str, Any] | tomlkit.items.Table,
) -> None:
    """
    悄悄地、递归地将旧配置 (old_values_source) 中的宝贝值合并到新配置 (target_config) 中。
    这样更新版本的时候，你辛辛苦苦设置的东西就不会丢啦！(๑•̀ㅂ•́)و✧
    """
    for key, old_value in old_values_source.items():
        # 'inner' 表比较特殊，通常我们不希望深层合并它，特别是里面的版本号
        if key == "inner":
            if (
                isinstance(old_value, dict | tomlkit.items.Table)
                and "version" in old_value
                and "inner" in target_config
                and isinstance(target_config.get("inner"), dict | tomlkit.items.Table)
                and "version" in target_config["inner"]  # type: ignore
            ):
                logger.debug(
                    f"  合并提示：新配置将使用自己的版本号: {target_config['inner']['version']} (旧版本是: {old_value['version']})"  # type: ignore
                )
            continue  # 'inner' 表就到此为止，不继续往里钻了

        if key in target_config:
            target_value = target_config[key]
            # 如果新旧配置里，这个键对应的值都是字典或表，那我们就得递归进去继续合并
            if isinstance(old_value, dict | tomlkit.items.Table) and isinstance(
                target_value, dict | tomlkit.items.Table
            ):
                _merge_configs_recursive(target_value, old_value)
            # 如果都不是字典/表 (比如是数字、字符串、列表)，就尝试用旧的覆盖新的
            elif not isinstance(old_value, dict | tomlkit.items.Table) and not isinstance(
                target_value, dict | tomlkit.items.Table
            ):
                try:
                    # 用 tomlkit.item 来创建/更新值，这样能更好地保留 TOML 的类型和格式哦
                    if isinstance(old_value, list):
                        # 列表的话，我们创建一个新的 TOML 数组，把旧列表里的东西一个个放进去
                        new_array = tomlkit.array()
                        for item in old_value:
                            new_array.append(tomlkit.item(item))
                        target_config[key] = new_array
                    else:
                        # 其他简单类型（包括 None），直接用 tomlkit.item 处理
                        target_config[key] = tomlkit.item(old_value)
                    logger.debug(
                        f"  合并值: [{key}] = {str(old_value)[:50]}{'...' if len(str(old_value)) > 50 else ''}"
                    )
                except Exception as e:
                    logger.warning(
                        f"  合并警告：合并键 '{key}' 的值 '{str(old_value)[:50]}' 时遇到小麻烦: {e}。只好保留模板里的值啦。"
                    )
            else:
                # 类型不匹配（比如，旧的是个简单值，新的是个表），通常我们会保留新模板的结构和值
                logger.debug(f"  合并提示：键 '{key}' 在新旧配置中类型不一样，听新模板的准没错！")
        else:
            # 如果旧配置里的某个键在新模板里找不到，那它可能是被时代抛弃了，忽略就好
            logger.debug(f"  合并提示：旧配置里的键 '{key}' 在新模板里失踪了，忽略它吧。")


def substitute_env_vars_recursive(  # 改为公开函数，因为 config_manager 可能也需要直接调用
    config_node: dict[str, Any] | list[Any] | tomlkit.items.Table | tomlkit.items.Array,
) -> None:
    """
    递归地扫描配置，把所有 "ENV_YOUR_VARIABLE" 这样的占位符替换成真正的环境变量值。
    就像一个勤劳的小蜜蜂，把花蜜（环境变量）采到配置的每个角落！🐝
    支持字典和列表的递归处理哦。
    """
    if isinstance(config_node, dict | tomlkit.items.Table):  # 如果是字典或 TOML 表
        # 用 list(config_node.items()) 是为了在迭代时也能安全地修改字典/表
        for key, value in list(config_node.items()):
            if isinstance(value, str) and value.startswith("ENV_"):
                env_var_name = value[4:]  # 把 "ENV_" 前缀去掉，得到真正的环境变量名
                env_value = os.getenv(env_var_name)  # 从系统环境里找找这个变量

                if env_value is not None:
                    # 找到了！现在尝试把它变成合适的类型
                    processed_value: Any
                    if env_value.lower() == "true":
                        processed_value = True
                    elif env_value.lower() == "false":
                        processed_value = False
                    else:
                        try:
                            processed_value = int(env_value)  # 试试看是不是整数
                        except ValueError:
                            try:
                                processed_value = float(env_value)  # 再试试是不是小数
                            except ValueError:
                                # 如果环境变量的值看起来像 TOML 列表或内联表，尝试用 tomlkit 解析
                                if (env_value.startswith("[") and env_value.endswith("]")) or (
                                    env_value.startswith("{") and env_value.endswith("}")
                                ):
                                    try:
                                        # 偷偷构造一个临时的 TOML 片段来解析
                                        parsed_item = tomlkit.parse(f"temp_key = {env_value}")
                                        processed_value = parsed_item["temp_key"]
                                    except Exception:  # 解析失败就保持原样吧
                                        processed_value = env_value
                                else:
                                    processed_value = env_value  # 其他情况，就当它是普通字符串
                    config_node[key] = processed_value  # 替换掉原来的占位符！
                    logger.debug(f"  环境变量替换：配置项 '{key}' 已从 '{env_var_name}' 加载。")
                else:
                    logger.warning(
                        f"  环境变量警告：配置想用环境变量 '{env_var_name}' (给 '{key}'用的), 但它好像没设置哦。只好用回原来的占位符 '{value}' 了。"
                    )
            elif isinstance(value, dict | tomlkit.items.Table | list | tomlkit.items.Array):
                # 如果值是嵌套的字典/表或列表/数组，那就要递归进去继续找！
                substitute_env_vars_recursive(value)
    elif isinstance(config_node, list | tomlkit.items.Array):  # 如果是列表或 TOML 数组
        for i, item in enumerate(config_node):
            if isinstance(item, str) and item.startswith("ENV_"):
                env_var_name = item[4:]
                env_value = os.getenv(env_var_name)
                if env_value is not None:
                    # 列表里的环境变量占位符也要变身！
                    processed_value: Any
                    if env_value.lower() == "true":
                        processed_value = True
                    elif env_value.lower() == "false":
                        processed_value = False
                    else:
                        try:
                            processed_value = int(env_value)
                        except ValueError:
                            try:
                                processed_value = float(env_value)
                            except ValueError:
                                processed_value = env_value
                    config_node[i] = processed_value  # type: ignore # 更新列表里的这个元素
                    logger.debug(f"  环境变量替换：列表里的一个元素已从 '{env_var_name}' 加载。")
                else:
                    logger.warning(
                        f"  环境变量警告：列表里有个元素想用环境变量 '{env_var_name}', 但它也没设置。只好保留原来的占位符了。"
                    )
            elif isinstance(item, dict | tomlkit.items.Table | list | tomlkit.items.Array):
                # 列表项是嵌套结构？递归进去！
                substitute_env_vars_recursive(item)


# --- 核心配置更新检查逻辑 ---


def perform_config_update_check(io_handler: ConfigIOHandler, prompt_user_and_exit_fn: Callable[[str], None]) -> bool:
    """
    执行配置文件的核心检查和更新流程。我可是专业的！
    返回一个布尔值，告诉你配置是不是刚刚新鲜出炉或者焕然一新了。
    """
    logger.info("开始仔细检查和更新配置文件，请稍等片刻...")

    config_was_created_or_updated: bool = False  # 先假设没有变化

    # 1. 模板文件是我们的生命线，必须存在！
    if not io_handler.template_exists():
        message = f"天哪！配置文件模板 '{io_handler.template_path}' 居然不见了！程序没法继续了，嘤嘤嘤..."
        logger.critical(message)  # 这是非常严重的问题！
        raise FileNotFoundError(message)  # 没有模板，直接罢工！

    # 2. 看看运行时配置文件在不在，或者是不是坏掉了
    if not io_handler.runtime_config_exists():
        logger.info(f"运行时配置文件 '{io_handler.runtime_path}' 好像还没出生，让我从模板创造一个吧！")
        if io_handler.copy_template_to_runtime():
            config_was_created_or_updated = True  # 新鲜出炉！
            logger.info("新的运行时配置文件已成功创建！撒花！✿✿ヽ(°▽°)ノ✿")
        else:
            message = f"糟糕！从模板创建运行时配置文件 '{io_handler.runtime_path}' 失败了！快检查下权限和路径吧。"
            logger.critical(message)
            prompt_user_and_exit_fn(message)  # 告诉用户然后溜了
        return config_was_created_or_updated  # 返回 True，因为需要用户检查

    # 运行时配置文件存在，加载它看看
    logger.debug(f"发现现有的运行时配置文件: '{io_handler.runtime_path}'，让我瞅瞅里面写了啥。")
    actual_config = io_handler.load_toml_file(io_handler.runtime_path)

    if actual_config is None:  # 文件在，但读不出来 (可能坏了)
        logger.warning(f"哎呀！现有的运行时配置文件 '{io_handler.runtime_path}' 可能坏掉了，读不出来。")
        io_handler.backup_runtime_config(prefix="broken_")  # 备份这个坏掉的，万一还有用呢
        if io_handler.copy_template_to_runtime():  # 从模板重新创建一个好的
            config_was_created_or_updated = True  # 也算是更新过了
            logger.info("已从模板重新创建了配置文件。你可能需要从那个标记为 'broken_' 的备份里找回你之前的设置哦。")
        else:
            message = f"雪上加霜！从模板重新创建损坏的配置文件 '{io_handler.runtime_path}' 也失败了！"
            logger.critical(message)
            prompt_user_and_exit_fn(message)
        return config_was_created_or_updated  # 返回 True

    # 3. 加载模板配置，准备比对版本号，看看是不是老古董了
    template_config = io_handler.load_toml_file(io_handler.template_path)
    if template_config is None:  # 模板文件这时候必须能读啊！
        message = f"致命错误！无法加载模板配置文件 '{io_handler.template_path}' 来比较版本！这不应该发生啊！"
        logger.critical(message)
        raise RuntimeError(message)  # 内部逻辑错误，直接抛异常

    # 获取版本号，要小心翼翼，万一没有呢
    current_template_version = str(template_config.get("inner", {}).get("version", EXPECTED_CONFIG_VERSION))
    actual_runtime_version = str(
        actual_config.get("inner", {}).get("version", "未知版本")
    )  # 如果没写版本，就当它是未知

    # 4. 版本大比拼！
    if actual_runtime_version == current_template_version:
        logger.info(
            f"版本一致！运行时配置 (v{actual_runtime_version}) 和模板 (v{current_template_version}) 是好朋友，不用更新啦。"
        )
        return False  # 版本相同，啥也不用干，返回 False

    logger.info(
        f"版本不一致！运行时配置 (v{actual_runtime_version}) 和模板 (v{current_template_version}) 版本对不上。准备更新..."
    )

    # 先把旧的运行时配置备份一下，安全第一！
    if io_handler.backup_runtime_config(prefix="pre_update_"):
        # 从模板复制一份新的作为基础
        if io_handler.copy_template_to_runtime():
            # 加载这个新鲜出炉的配置 (其实就是当前模板的内容)
            new_config_base = io_handler.load_toml_file(io_handler.runtime_path)
            if new_config_base:
                logger.info("开始施展魔法，把旧配置里的好东西合并到新模板结构中...")
                _merge_configs_recursive(new_config_base, actual_config)  # actual_config 是旧的、已加载的配置内容
                if io_handler.save_toml_file(io_handler.runtime_path, new_config_base):
                    logger.info(f"配置文件已成功更新并合并旧值到 '{io_handler.runtime_path}'！完美！")
                    config_was_created_or_updated = True  # 更新成功！
                else:
                    logger.error(
                        f"致命错误！保存合并后的配置文件 '{io_handler.runtime_path}' 失败了！程序可能要出问题了！"
                    )
                    # 这里可以考虑是不是要恢复备份，或者强烈建议用户手动检查
            else:
                logger.critical(f"致命错误！无法加载新复制的模板文件 '{io_handler.runtime_path}' 来进行合并！")
        else:
            logger.critical("致命错误！从模板复制新的配置文件基础失败了！更新任务中断！")
    else:
        logger.critical("致命错误！备份旧的运行时配置文件失败了！不敢继续更新了，怕弄丢东西！")

    return config_was_created_or_updated
