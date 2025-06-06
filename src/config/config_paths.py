\
from pathlib import Path

# --- Path and Version Constants Definition ---
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent  # Points to AIcarusCore

TEMPLATE_DIR: Path = PROJECT_ROOT / "template"  # Template files directory
RUNTIME_CONFIG_DIR: Path = PROJECT_ROOT / "config"  # Runtime actual config files directory
OLD_CONFIG_BACKUP_DIR: Path = RUNTIME_CONFIG_DIR / "old"  # Old config backup directory

CONFIG_TEMPLATE_FILENAME: str = "config_template.toml"  # Template config filename
ACTUAL_CONFIG_FILENAME: str = "config.toml"  # Runtime actual config filename

# Expected config file structure version by Alcarus code.
# Increment when template/settings_template.toml structure changes significantly,
# and ensure [inner].version in settings_template.toml is also updated.
EXPECTED_CONFIG_VERSION: str = "0.0.1"  # Adjust according to your actual template version
