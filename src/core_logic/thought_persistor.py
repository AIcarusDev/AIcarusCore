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

        initiated_action_data_for_db = None
        if action_desc_from_llm and action_desc_from_llm.lower() != "null":
            # action_id 应该由 CoreLogic 在调用 ActionHandler 前生成并放入 thought_json
            # 或者由 ActionHandler 生成并更新回 thought_json (如果设计如此)
            # 这里我们假设 thought_json 中可能已经有 action_id (如果LLM决策了行动且CoreLogic已处理)
            # 或者如果这里是首次确定行动，则生成一个新的。
            # 为了与 CoreLogic._process_and_store_thought 的原逻辑保持一致，
            # 我们在这里检查 thought_json 中是否已有 action_id，如果没有，则不主动生成，
            # 因为原逻辑中 action_id 的生成和 initiated_action_data_for_db 的构建是耦合的。
            # 修正：原逻辑是如果 action_desc_from_llm 有效，就生成 action_id 并构建 initiated_action_data_for_db

            action_id_this_cycle = thought_json.get("action_id")  # 尝试获取已有的
            if not action_id_this_cycle:  # 如果LLM的原始输出没有action_id，但有action_to_take
                action_id_this_cycle = str(uuid.uuid4())
                # 这个 action_id 需要被回传或更新到 thought_json，以便后续 _dispatch_action 使用
                # 但这个方法只负责存储，不应该修改 thought_json 的原始结构给调用者
                # 所以，CoreLogic 在调用此方法前，如果LLM有action_to_take，就应该先生成action_id并放入thought_json
                # 这里我们遵循原 CoreLogic._process_and_store_thought 的逻辑：
                # 如果 action_to_take 有效，就创建 initiated_action_data_for_db，其中包含新生成的 action_id
                # 并且这个 action_id 也会被放入 thought_json["action_id"] (虽然这行代码在原方法中是隐式的)
                # 为了清晰，我们假设调用者 (CoreLogic) 会在调用 store_thought 之前，
                # 如果 thought_json["action_to_take"] 有效，就确保 thought_json["action_id"] 也存在。
                # 或者，更简单的是，如果 action_to_take 有效，这里就构建 initiated_action_data_for_db
                # CoreLogic 在调用 _dispatch_action 时，会从 thought_json 中取 action_id。
                # 所以，如果这里生成了，需要一种方式让 CoreLogic 知道。
                # 让我们简化：如果 action_to_take 有，就构建 action_attempted，并假设 action_id 已在 thought_json 中。
                # 如果 thought_json 中没有 action_id，那么 initiated_action_data_for_db 中的 action_id 就会是 None。
                # 这与原逻辑不符。原逻辑是：
                # if action_desc_from_llm and action_desc_from_llm.lower() != "null":
                #     action_id_this_cycle = str(uuid.uuid4())
                #     thought_json["action_id"] = action_id_this_cycle # 这行是推断的，原代码没有显式写
                #     initiated_action_data_for_db = { ... "action_id": action_id_this_cycle ... }
                # 为了保持一致，如果 action_to_take 有效，我们就构建 initiated_action_data_for_db
                # CoreLogic 在调用此方法后，如果 initiated_action_data_for_db 被构建了，
                # 它应该从返回的 saved_key 对应的文档中（或者通过其他方式）获取这个 action_id。
                # 或者，此方法可以返回 (saved_key, action_id_if_generated)。
                # 为了简单，我们先按原逻辑构建，并假设 CoreLogic 会处理 action_id 的传递。
                # 最直接的方式是，如果 action_to_take 存在，就构建 initiated_action_data_for_db，
                # CoreLogic 在调用 _dispatch_action 时，会从 thought_json 中获取 action_id。
                # 所以，如果 thought_json["action_to_take"] 存在，CoreLogic 应该在调用 store_thought 之前
                # 就已经为 thought_json["action_id"] 赋好值了。
                # 因此，这里我们只依赖 thought_json.get("action_id")

        action_id_for_db = thought_json.get("action_id")
        if not action_id_for_db and action_desc_from_llm and action_desc_from_llm.lower() != "null":
            action_id_for_db = str(uuid.uuid4())
            thought_json["action_id"] = action_id_for_db

        if action_id_for_db:  # 只有当 action_id 确实存在时才记录 action_attempted
            initiated_action_data_for_db = {
                "action_description": action_desc_from_llm,
                "action_motivation": action_motive_from_llm,
                "action_id": action_id_for_db,
                "status": "PENDING",  # 初始状态
                "result_seen_by_shimo": False,
                "initiated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        else:
            self.logger.debug(
                f"LLM未指定行动，不记录 action_attempted。(action_to_take: '{action_desc_from_llm}')"
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
