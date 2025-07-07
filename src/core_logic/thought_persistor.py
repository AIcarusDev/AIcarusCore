# src/core_logic/thought_persistor.py (小懒猫·点打包员版)
import datetime
import uuid
from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logging_config import get_logger
from src.database.models import ThoughtChainDocument

if TYPE_CHECKING:
    from src.database import ThoughtStorageService

logger = get_logger(__name__)


class ThoughtPersistor:
    def __init__(self, thought_storage: "ThoughtStorageService") -> None:
        self.thought_storage = thought_storage
        logger.info("ThoughtPersistor 已初始化。")

    async def store_thought(
        self, thought_json: dict[str, Any], source_type: str, source_id: str | None = None
    ) -> str | None:
        """处理并存储思考结果到数据库，这次我们用统一的、全新的思想链！"""
        action_payload = thought_json.get("action")
        action_id = (
            str(uuid.uuid4()) if action_payload and isinstance(action_payload, dict) else None
        )

        # 1. 把思考结果打包成一颗新的“思想点”
        new_thought_pearl = ThoughtChainDocument(
            _key=str(uuid.uuid4()),  # 给点一个唯一的key
            timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
            mood=thought_json.get("mood", "平静"),
            think=thought_json.get("think", "我刚才好像走神了。"),
            goal=thought_json.get("goal"),
            source_type=source_type,
            source_id=source_id,
            action_id=action_id,
            action_payload=action_payload,
        )

        # 2. 把点交给存储服务去串起来
        try:
            # // 注意！我们现在调用的是改造后的 save_thought_and_link 方法！
            saved_key = await self.thought_storage.save_thought_and_link(new_thought_pearl)

            if not saved_key:
                logger.error("保存思想点失败！可能是数据库操作异常。")
                return None

            logger.info(f"思想点 '{saved_key}' 已打包并成功串入思想链。")
            return saved_key
        except Exception as e:
            logger.error(f"打包并保存思想点时发生意外错误: {e}", exc_info=True)
            return None
