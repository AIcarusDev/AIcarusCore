# src/core_logic/thought_persistor.py
import datetime
import uuid
from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logging_config import get_logger

if TYPE_CHECKING:
    from src.database import ThoughtStorageService

logger = get_logger(__name__)


class ThoughtPersistor:
    def __init__(self, thought_storage: "ThoughtStorageService") -> None:
        self.thought_storage = thought_storage
        logger.info("ThoughtPersistor 已初始化。")

    async def store_thought(
        self, thought_json: dict[str, Any], prompts: dict[str, Any], context: dict[str, Any]
    ) -> str | None:
        """
        处理并存储思考结果到数据库，这次我们用统一的、全新的字段名！
        """
        # 小色猫的低语：姐姐好厉害，一下子就找到了G点...
        initiated_action_data_for_db = None
        action_id_for_db = thought_json.get("action_id")
        action_payload = thought_json.get("action")  # 动作现在是嵌套的，得这么拿！

        # 只有当 action 字段存在，并且是个字典，才说明有行动意图
        if action_payload and isinstance(action_payload, dict):
            if not action_id_for_db:
                # 这是一个兜底，理论上不应该发生。如果发生了，说明上游逻辑有漏洞。
                logger.warning(
                    f"行动 '{action_payload}' 缺少 action_id，将在 ThoughtPersistor 中生成一个新的。请检查上游逻辑！"
                )
                action_id_for_db = str(uuid.uuid4())
                thought_json["action_id"] = action_id_for_db

            initiated_action_data_for_db = {
                # 这里我们直接把整个 action 对象存起来，省得以后再改
                "action_payload": action_payload,
                "action_id": action_id_for_db,
                "status": "PENDING",
                "result_seen_by_shimo": False,
                "initiated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        else:
            logger.debug("LLM未指定行动，不记录。")

        document_to_save = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "time_injected_to_prompt": prompts.get("current_time"),
            "system_prompt_sent": prompts.get("system"),
            "full_user_prompt_sent": prompts.get("user"),
            "intrusive_thought_injected": context.get("intrusive_thought"),
            "recent_contextual_information_input": context.get("recent_context"),
            "think": thought_json.get("think"),
            "mood": thought_json.get("mood"),
            "goal": thought_json.get("goal"),
            "action": initiated_action_data_for_db,
            "image_inputs_count": len(context.get("images", [])),
            "image_inputs_preview": [img[:100] for img in context.get("images", [])[:3]],
            "_llm_usage_info": thought_json.get("_llm_usage_info"),
        }

        try:
            saved_key = await self.thought_storage.save_main_thought_document(document_to_save)
            if not saved_key:
                logger.error("保存思考文档失败！可能是数据库操作异常。")
                return None
            logger.info(f"思考文档已成功保存，key: {saved_key}")
            return saved_key
        except Exception as e:
            logger.error(f"保存思考文档时发生意外错误: {e}", exc_info=True)
            return None
