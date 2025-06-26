# src/core_logic/thought_persistor.py
import datetime
import uuid
from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logger_manager import get_logger

if TYPE_CHECKING:
    from src.database.services.thought_storage_service import ThoughtStorageService

logger = get_logger("AIcarusCore.CoreLogic.ThoughtPersistor")


class ThoughtPersistor:
    def __init__(self, thought_storage: "ThoughtStorageService") -> None:
        self.thought_storage = thought_storage
        logger.info("ThoughtPersistor 已初始化。")

    async def store_thought(
        self, thought_json: dict[str, Any], prompts: dict[str, Any], context: dict[str, Any]
    ) -> str | None:
        """
        处理并存储思考结果到数据库。
        """
        # --- DEBUG LOG START ---
        logger.info(
            f"[ThoughtPersistor DEBUG] store_thought received thought_json with action_id: {thought_json.get('action_id')}"
        )
        logger.info(
            f"[ThoughtPersistor DEBUG] store_thought received thought_json with action_to_take: {thought_json.get('action_to_take')}"
        )
        # --- DEBUG LOG END ---

        # 就是这里！用我最淫荡的 (get() or "") 新姿势，再也不会软掉了！
        action_desc_from_llm = (thought_json.get("action_to_take") or "").strip()
        action_motive_from_llm = (thought_json.get("action_motivation") or "").strip()

        initiated_action_data_for_db = None

        action_id_for_db = thought_json.get("action_id")

        # 只有在明确有行动描述，并且该行动不是'null'时，才认为需要记录行动尝试
        if action_desc_from_llm and action_desc_from_llm.lower() != "null":
            if not action_id_for_db:
                # 这是一个兜底，理论上不应该发生。如果发生了，说明上游逻辑有漏洞。
                logger.warning(
                    f"行动 '{action_desc_from_llm}' 缺少 action_id，将在 ThoughtPersistor 中生成一个新的。请检查上游逻辑！"
                )
                action_id_for_db = str(uuid.uuid4())
                thought_json["action_id"] = action_id_for_db  # 更新一下，虽然对下游没用了，但保持数据一致性

            initiated_action_data_for_db = {
                "action_description": action_desc_from_llm,
                "action_motivation": action_motive_from_llm,
                "action_id": action_id_for_db,
                "status": "PENDING",
                "result_seen_by_shimo": False,
                "initiated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        else:
            logger.debug(f"LLM未指定行动，不记录 action_attempted。(action_to_take: '{action_desc_from_llm}')")

        document_to_save = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "time_injected_to_prompt": prompts.get("current_time"),
            "system_prompt_sent": prompts.get("system"),
            "full_user_prompt_sent": prompts.get("user"),
            "intrusive_thought_injected": context.get("intrusive_thought"),
            "recent_contextual_information_input": context.get("recent_context"),
            # 这里也用安全的姿势来获取，确保万无一失，让你的身体每个角落都充满我的爱液～
            "think_output": thought_json.get("think"),
            "emotion_output": thought_json.get("emotion"),
            "next_think_output": thought_json.get("next_think"),
            "to_do_output": thought_json.get("to_do"),
            "done_output": thought_json.get("done"),
            "action_to_take_output": thought_json.get("action_to_take"),
            "action_motivation_output": thought_json.get("action_motivation"),
            "action_attempted": initiated_action_data_for_db,
            "image_inputs_count": len(context.get("images", [])),
            "image_inputs_preview": [img[:100] for img in context.get("images", [])[:3]],
            "_llm_usage_info": thought_json.get("_llm_usage_info"),
        }

        # --- DEBUG LOG START for action_attempted before saving ---
        logger.info(
            f"[ThoughtPersistor DEBUG] Document to save, action_attempted: {document_to_save.get('action_attempted')}"
        )
        # --- DEBUG LOG END ---

        try:
            saved_key = await self.thought_storage.save_main_thought_document(document_to_save)
            if not saved_key:
                logger.error("保存思考文档失败！(ThoughtStorageService.save_main_thought_document 返回了 None)")
                return None
            logger.info(f"思考文档已成功保存，key: {saved_key}")
            return saved_key
        except Exception as e:
            logger.error(f"保存思考文档时发生意外错误: {e}", exc_info=True)
            return None
