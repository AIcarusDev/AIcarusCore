# src/core_logic/thought_persistor.py
import datetime
import uuid  # 确保导入 uuid
from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logger_manager import get_logger

if TYPE_CHECKING:
    from src.database.services.thought_storage_service import ThoughtStorageService

logger = get_logger("AIcarusCore.CoreLogic.ThoughtPersistor")


class ThoughtPersistor:
    def __init__(self, thought_storage: "ThoughtStorageService") -> None:
        self.thought_storage = thought_storage
        self.logger = logger
        self.logger.info("ThoughtPersistor 已初始化。")

    async def store_thought(
        self, thought_json: dict[str, Any], prompts: dict[str, Any], context: dict[str, Any]
    ) -> str | None:
        """
        处理并存储思考结果到数据库。
        """
        # --- DEBUG LOG START ---
        self.logger.info(
            f"[ThoughtPersistor DEBUG] store_thought received thought_json with action_id: {thought_json.get('action_id')}"
        )
        self.logger.info(
            f"[ThoughtPersistor DEBUG] store_thought received thought_json with action_to_take: {thought_json.get('action_to_take')}"
        )
        # --- DEBUG LOG END ---
        action_desc_from_llm = thought_json.get("action_to_take", "").strip()
        action_motive_from_llm = thought_json.get("action_motivation", "").strip()

        # 职责分离：此方法不应生成或修改 action_id。
        # action_id 的生成是 CoreLogicFlow 的责任，它应在调用此方法前就已将 action_id 放入 thought_json。
        action_id_for_db = thought_json.get("action_id")
        initiated_action_data_for_db = None

        # 仅当 action_to_take 和 action_id 都有效时，才创建 action_attempted 记录
        if action_desc_from_llm and action_desc_from_llm.lower() != "null":
            if action_id_for_db:
                initiated_action_data_for_db = {
                    "action_description": action_desc_from_llm,
                    "action_motivation": action_motive_from_llm,
                    "action_id": action_id_for_db,
                    "status": "PENDING",  # 初始状态
                    "result_seen_by_shimo": False,
                    "initiated_at": datetime.datetime.now(datetime.UTC).isoformat(),
                }
            else:
                # 如果LLM决定行动，但 CoreLogic 未提供 action_id，这是一个逻辑错误，需要记录警告。
                self.logger.warning(
                    f"LLM指定了行动 '{action_desc_from_llm}' 但 thought_json 中缺少 action_id。将不记录 action_attempted。"
                )

        document_to_save = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "time_injected_to_prompt": prompts.get("current_time"),  # 从 prompts 字典获取
            "system_prompt_sent": prompts.get("system"),
            "full_user_prompt_sent": prompts.get("user"),
            "intrusive_thought_injected": context.get("intrusive_thought"),
            "recent_contextual_information_input": context.get("recent_context"),
            "think_output": thought_json.get("think"),
            "emotion_output": thought_json.get("emotion"),
            "next_think_output": thought_json.get("next_think"),
            "to_do_output": thought_json.get("to_do", ""),
            "done_output": thought_json.get("done", False),
            "action_to_take_output": thought_json.get("action_to_take", ""),  # 记录LLM原始的action_to_take
            "action_motivation_output": thought_json.get("action_motivation", ""),  # 记录LLM原始的action_motivation
            "action_attempted": initiated_action_data_for_db,  # 这个可能为 None
            "image_inputs_count": len(context.get("images", [])),
            "image_inputs_preview": [img[:100] for img in context.get("images", [])[:3]],
            "_llm_usage_info": thought_json.get("_llm_usage_info"),
        }

        # --- DEBUG LOG START for action_attempted before saving ---
        self.logger.info(
            f"[ThoughtPersistor DEBUG] Document to save, action_attempted: {document_to_save.get('action_attempted')}"
        )
        # --- DEBUG LOG END ---

        try:
            saved_key = await self.thought_storage.save_main_thought_document(document_to_save)
            if not saved_key:
                self.logger.error("保存思考文档失败！(ThoughtStorageService.save_main_thought_document 返回了 None)")
                return None
            self.logger.info(f"思考文档已成功保存，key: {saved_key}")
            return saved_key
        except Exception as e:
            self.logger.error(f"保存思考文档时发生意外错误: {e}", exc_info=True)
            return None
