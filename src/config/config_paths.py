from pathlib import Path

# --- 路径和版本常量定义 ---

# 项目根目录，指向 AIcarusCore
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent

# 模板文件目录
TEMPLATE_DIR: Path = PROJECT_ROOT / "template"
# 运行时实际配置文件目录
RUNTIME_CONFIG_DIR: Path = PROJECT_ROOT / "config"
# 旧配置文件备份目录
OLD_CONFIG_BACKUP_DIR: Path = RUNTIME_CONFIG_DIR / "old"

# 模板配置文件名
CONFIG_TEMPLATE_FILENAME: str = "config_template.toml"
# 运行时实际配置文件名
ACTUAL_CONFIG_FILENAME: str = "config.toml"

# Alcarus 代码期望的配置文件结构版本
# 当 template/settings_template.toml 结构发生重大变化时需要递增此版本号，
# 并确保 settings_template.toml 中的 [inner].version 也同步更新
EXPECTED_CONFIG_VERSION: str = "0.0.13"
