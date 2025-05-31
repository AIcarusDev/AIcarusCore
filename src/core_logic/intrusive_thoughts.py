import asyncio
import datetime
import json
from src.common.custom_logging.logger_manager import get_logger
from typing import TYPE_CHECKING

# ArangoDB 相关的导入移至 arangodb_handler
from arango.database import StandardDatabase  # 需要 StandardDatabase 来调用 get_random_intrusive_thought

from src.config.alcarus_configs import PersonaSettings

# --- 新增：导入新的数据库处理器函数 ---
from src.database import arangodb_handler

from src.llmrequest.llm_processor import Client

if TYPE_CHECKING:
    from arango.collection import StandardCollection  # 仅用于类型提示，如果直接操作集合对象

logger = get_logger("AIcarusCore.intrusive.thoughts") 

INTRUSIVE_PROMPT_TEMPLATE = """
你是{bot_name}；
{persona_description}

{persona_profile}

现在，你要为自己生成一些，像是突然冒出来的、可能有些无厘头、不合逻辑、荒谬、危险、离谱、甚至是成人的念头，或者是一闪而过的灵感、奇怪的联想。
这些念头是“侵入性思维”，突然出现，有可能基于你的人设，也有可能跟你人设毫无关联。
可能很简短，例如:突然想做爱/突然想死/突然想把手机摔了，也可能有一定细节，总得来说一般都是浅显的，通俗的，不会太复杂。
请确保思维的多样性。同时注意侵入性思维的随机性，避免都是过于正向的。

请严格按照json格式输出5条独立的侵入性思维：
{{
    "1": "string",
    "2": "string",
    "3": "string",
    "4": "string",
    "5": "string"
}}

输出JSON：
"""


async def generate_new_intrusive_thoughts_async(
    llm_client: Client, persona_cfg: PersonaSettings
) -> list[str] | None:  # 改为异步，接收client实例
    """使用 Client 生成一批新的侵入性思维 (异步)"""
    if not llm_client:
        logger.warning("(Intrusive) LLM Client 实例未提供，无法生成侵入性思维。")  # 修改：使用 logger
        return None

    # 使用 persona_cfg 填充 Prompt
    try:
        filled_prompt = INTRUSIVE_PROMPT_TEMPLATE.format(
            bot_name=persona_cfg.bot_name,
            persona_description=persona_cfg.description,
            persona_profile=persona_cfg.profile,
        )
    except KeyError as e:
        # logger.info(f"(Intrusive) 错误：填充侵入性思维Prompt模板时缺少键: {e}") # 可以考虑也用 logger
        logger.error(f"(Intrusive) 错误：填充侵入性思维Prompt模板时缺少键: {e}")
        return None
    except AttributeError as e:
        # logger.info(f"(Intrusive) 错误：persona_cfg 对象不完整: {e}") # 可以考虑也用 logger
        logger.error(f"(Intrusive) 错误：persona_cfg 对象不完整: {e}")
        return None

    # --- 新增日志打印 ---
    logger.info(
        f"--- 侵入性思维LLM接收到的完整Prompt (模型: {llm_client.llm_client.model_name}) ---\n{filled_prompt}\n--- Prompt结束 ---"
    )
    # --------------------

    logger.info(
        f"(Intrusive) 正在请求 {llm_client.llm_client.provider} API ({llm_client.llm_client.model_name}) 生成侵入性思维..."
    )
    # logger.info(f"(Intrusive) 使用的Prompt:\n{filled_prompt}") # 可选：调试时打印Prompt

    # 确保 raw_text 在 try 外部定义，以便 except 块可以访问
    raw_text: str = ""  # 初始化以避免引用前未赋值的警告
    try:
        response_data = await llm_client.make_llm_request(
            prompt=filled_prompt,  # <--- 使用填充后的Prompt
            is_stream=False,
        )

        if response_data.get("error"):
            error_type = response_data.get("type", "UnknownError")
            error_msg = response_data.get("message", "Client返回了一个错误")
            logger.error(f"(Intrusive) 错误：Client调用失败 ({error_type}): {error_msg}")  # 修改：使用 logger
            return None

        raw_text = response_data.get("text")
        if not raw_text:
            logger.error("(Intrusive) 错误：Client响应中缺少文本内容。")  # 修改：使用 logger
            return None

        logger.debug(f"(Intrusive) LLM API 原始响应: {raw_text[:300]}...")  # 修改：使用 logger

        if raw_text.strip().startswith("```json"):
            json_str = raw_text.strip()[7:-3].strip()
        elif raw_text.strip().startswith("```"):
            json_str = raw_text.strip()[3:-3].strip()
        else:
            json_str = raw_text.strip()

        thoughts_json = json.loads(json_str)
        generated_thoughts = [
            thoughts_json[key] for key in thoughts_json if thoughts_json[key] and thoughts_json[key].strip()
        ]
        logger.info(f"(Intrusive) 成功生成 {len(generated_thoughts)} 条侵入性思维。")  # 修改：使用 logger
        return generated_thoughts

    except json.JSONDecodeError as e:
        logger.error(f"(Intrusive) 错误：解析侵入性思维 JSON 响应失败: {e}")  # 修改：使用 logger
        logger.error(f"(Intrusive) 未能解析的文本内容: {raw_text if 'raw_text' in locals() else 'N/A'}")
        return None
    except Exception as e:
        logger.error(
            f"(Intrusive) 错误：调用 Client 生成侵入性思维失败: {e}", exc_info=True
        )  # 修改：使用 logger, 添加 exc_info
        return None


def background_intrusive_thought_generator(
    llm_client: Client,
    db_instance: StandardDatabase,  # <--- 修改：接收数据库对象
    collection_name: str,  # <--- 修改：接收集合名称
    module_settings: dict,
    stop_event: asyncio.Event,
    persona_cfg: PersonaSettings,  # <--- 新增参数
) -> None:
    generation_interval = module_settings.get("generation_interval_seconds", 600)
    logger.info(
        f"(BackgroundIntrusive) 侵入性思维生成器已启动，每 {generation_interval} 秒向 ArangoDB 生成一次。"
    )  # 修改：使用 logger

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 获取一次集合对象，因为集合名称在循环中不变
    intrusive_thoughts_collection: StandardCollection | None = None
    try:
        if db_instance.has_collection(collection_name):
            intrusive_thoughts_collection = db_instance.collection(collection_name)
        else:
            logger.error(
                f"(BackgroundIntrusive) 错误：集合 '{collection_name}' 在数据库 '{db_instance.name}' 中不存在，无法保存侵入性思维。"
            )
            # 可以在这里决定是否停止线程，或者让它在没有集合的情况下空转
            # return
    except Exception as e_coll:
        logger.error(f"(BackgroundIntrusive) 获取集合 '{collection_name}' 时出错: {e_coll}", exc_info=True)
        # return

    while not stop_event.is_set():
        new_thoughts: list[str] | None = None
        try:
            # 在这个线程的事件循环中运行异步函数
            new_thoughts = loop.run_until_complete(
                generate_new_intrusive_thoughts_async(llm_client, persona_cfg)  # <--- 传递 persona_cfg
            )
        except RuntimeError as e:
            if "cannot be called from a running event loop" in str(e):
                logger.error(f"(BackgroundIntrusive) Asyncio 运行时错误: {e}. 后台线程可能存在问题。", exc_info=True)
            else:
                logger.error(f"(BackgroundIntrusive) 运行异步生成时出错: {e}", exc_info=True)
        except Exception as e_gen:
            logger.error(f"(BackgroundIntrusive) 运行异步生成时发生未知错误: {e_gen}", exc_info=True)

        if new_thoughts and intrusive_thoughts_collection is not None:  # 确保集合对象有效
            documents_to_insert = [
                {"text": thought, "timestamp_generated": datetime.datetime.now(datetime.UTC).isoformat(), "used": False}
                for thought in new_thoughts
                if thought  # 确保 thought 不是 None 或空字符串
            ]
            if documents_to_insert:
                # 调用新的数据库处理器函数
                # 注意：save_intrusive_thoughts_batch 是异步的，但这里是在一个同步函数 (线程目标) 中
                # 因此，我们需要在 loop 中运行它
                try:
                    loop.run_until_complete(
                        arangodb_handler.save_intrusive_thoughts_batch(
                            intrusive_thoughts_collection, documents_to_insert
                        )
                    )
                except Exception as e_save_batch:
                    logger.error(
                        f"(BackgroundIntrusive) 调用 arangodb_handler.save_intrusive_thoughts_batch 时出错: {e_save_batch}",
                        exc_info=True,
                    )
        elif new_thoughts and intrusive_thoughts_collection is None:
            logger.warning("(BackgroundIntrusive) 生成了新的侵入性思维，但数据库集合未初始化，无法保存。")

        wait_completed_or_interrupted = stop_event.wait(timeout=float(generation_interval))
        if wait_completed_or_interrupted:
            break

    loop.close()
    logger.info("(BackgroundIntrusive) 侵入性思维生成器后台线程已停止。")
