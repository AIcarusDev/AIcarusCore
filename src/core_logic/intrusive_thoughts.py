# src/core_logic/intrusive_thoughts.py
import asyncio
import datetime
import json
import threading  # 保留 threading 用于后台线程
from typing import TYPE_CHECKING

from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import IntrusiveThoughtsSettings, PersonaSettings  # 明确导入所需配置
from src.database.arangodb_handler import ArangoDBHandler  # 导入封装后的数据库处理器
from src.llmrequest.llm_processor import Client as ProcessorClient  # 重命名以避免与 arango.ArangoClient 混淆

if TYPE_CHECKING:
    pass  # 保留类型检查的导入


class IntrusiveThoughtsGenerator:
    """
    负责生成和管理侵入性思维。
    """

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

    def __init__(
        self,
        llm_client: ProcessorClient,
        db_handler: ArangoDBHandler,
        persona_cfg: PersonaSettings,
        module_settings: IntrusiveThoughtsSettings,
        stop_event: threading.Event,
    ):
        """
        初始化侵入性思维生成器。

        :param llm_client: 用于生成思维的LLM客户端。
        :param db_handler: ArangoDBHandler 实例用于数据库操作。
        :param persona_cfg: 人格配置。
        :param module_settings: 侵入性思维模块的特定设置。
        :param stop_event: 用于停止后台生成线程的事件。
        """
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        self.llm_client = llm_client
        self.db_handler = db_handler
        self.persona_cfg = persona_cfg
        self.module_settings = module_settings  # 直接存储 IntrusiveThoughtsSettings 对象
        self.stop_event = stop_event
        self.logger.info(f"{self.__class__.__name__} instance created.")

    async def _generate_new_intrusive_thoughts_async(self) -> list[str] | None:
        """使用LLM异步生成一批新的侵入性思维。"""
        if not self.llm_client:
            self.logger.warning("LLM Client 实例未提供，无法生成侵入性思维。")
            return None

        try:
            filled_prompt = self.INTRUSIVE_PROMPT_TEMPLATE.format(
                bot_name=self.persona_cfg.bot_name,
                persona_description=self.persona_cfg.description,
                persona_profile=self.persona_cfg.profile,
            )
        except KeyError as e:
            self.logger.error(f"错误：填充侵入性思维Prompt模板时缺少键: {e}")
            return None
        except AttributeError as e:
            self.logger.error(f"错误：persona_cfg 对象不完整: {e}")
            return None

        # self.logger.info(
        #    f"--- 侵入性思维LLM接收到的完整Prompt (模型: {self.llm_client.llm_client.model_name}) ---\n{filled_prompt}\n--- Prompt结束 ---"
        # )
        self.logger.info("正在生成一批侵入性思维")

        raw_text: str = ""
        try:
            response_data = await self.llm_client.make_llm_request(prompt=filled_prompt, is_stream=False)

            if response_data.get("error"):
                error_type = response_data.get("type", "UnknownError")
                error_msg = response_data.get("message", "Client返回了一个错误")
                self.logger.error(f"错误：Client调用失败 ({error_type}): {error_msg}")
                return None

            raw_text = response_data.get("text")  # type: ignore
            if not raw_text:
                self.logger.error("错误：Client响应中缺少文本内容。")
                return None

            self.logger.debug(f"LLM API 原始响应: {raw_text[:300]}...")

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
            self.logger.info(f"成功生成 {len(generated_thoughts)} 条侵入性思维。")
            return generated_thoughts

        except json.JSONDecodeError as e:
            self.logger.error(f"错误：解析侵入性思维 JSON 响应失败: {e}")
            self.logger.error(f"未能解析的文本内容: {raw_text if 'raw_text' in locals() else 'N/A'}")
            return None
        except Exception as e:
            self.logger.error(f"错误：调用 Client 生成侵入性思维失败: {e}", exc_info=True)
            return None

    def _background_generator_thread_target(self) -> None:
        """后台线程的目标函数，定期生成并保存侵入性思维。"""
        generation_interval = self.module_settings.generation_interval_seconds
        self.logger.info(f"侵入性思维生成器后台线程已启动，每 {generation_interval} 秒向 ArangoDB 生成一次。")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # 集合名称通过 self.db_handler.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME 获取
        # 集合实例的获取和检查在 db_handler 的方法内部处理

        while not self.stop_event.is_set():
            new_thoughts: list[str] | None = None
            try:
                new_thoughts = loop.run_until_complete(self._generate_new_intrusive_thoughts_async())
            except RuntimeError as e:  # pylint: disable=broad-except
                if "cannot be called from a running event loop" in str(e):
                    self.logger.error(f"Asyncio 运行时错误: {e}. 后台线程可能存在问题。", exc_info=True)
                else:
                    self.logger.error(f"运行异步生成时出错: {e}", exc_info=True)
            except Exception as e_gen:  # pylint: disable=broad-except
                self.logger.error(f"运行异步生成时发生未知错误: {e_gen}", exc_info=True)

            if new_thoughts:
                documents_to_insert = [
                    {
                        "text": thought,
                        "timestamp_generated": datetime.datetime.now(datetime.UTC).isoformat(),
                        "used": False,
                    }
                    for thought in new_thoughts
                    if thought
                ]
                if documents_to_insert:
                    try:
                        # 使用 self.db_handler 的方法保存
                        loop.run_until_complete(self.db_handler.save_intrusive_thoughts_batch(documents_to_insert))
                    except Exception as e_save_batch:  # pylint: disable=broad-except
                        self.logger.error(
                            f"调用 db_handler.save_intrusive_thoughts_batch 时出错: {e_save_batch}",
                            exc_info=True,
                        )
            elif new_thoughts is None:  # 明确检查 None，因为空列表也是一种有效情况（例如LLM未返回任何思维）
                self.logger.debug("本轮未生成新的侵入性思维或生成失败。")

            wait_completed_or_interrupted = self.stop_event.wait(timeout=float(generation_interval))
            if wait_completed_or_interrupted:
                break
        loop.close()
        self.logger.info("侵入性思维生成器后台线程已停止。")

    def start_background_generation(self) -> threading.Thread | None:
        """启动后台侵入性思维生成线程。"""
        if not self.module_settings.enabled:
            self.logger.info("侵入性思维模块未启用，不启动后台生成。")
            return None
        if not self.llm_client:
            self.logger.error("侵入性思维LLM客户端未初始化，无法启动后台生成。")
            return None
        if not self.db_handler:
            self.logger.error("数据库处理器未初始化，无法启动后台生成。")
            return None

        thread = threading.Thread(
            target=self._background_generator_thread_target,
            daemon=True,
        )
        thread.start()
        self.logger.info("侵入性思维后台生成线程已请求启动。")
        return thread


# 示例如何从 CoreLogic 中调用 (假设 core_logic_instance 是 CoreLogic 的实例)
# if core_logic_instance.root_cfg and core_logic_instance.intrusive_thoughts_llm_client and core_logic_instance.db_handler:
#     intrusive_settings = core_logic_instance.root_cfg.intrusive_thoughts_module_settings
#     persona_settings = core_logic_instance.root_cfg.persona
#
#     intrusive_generator = IntrusiveThoughtsGenerator(
#         llm_client=core_logic_instance.intrusive_thoughts_llm_client,
#         db_handler=core_logic_instance.db_handler,
#         persona_cfg=persona_settings,
#         module_settings=intrusive_settings,
#         stop_event=core_logic_instance.stop_event # CoreLogic 管理全局停止事件
#     )
#     # 在 CoreLogic 的启动流程中调用
#     # intrusive_thread = intrusive_generator.start_background_generation()
#     # if intrusive_thread:
#     #     core_logic_instance.intrusive_thread = intrusive_thread # CoreLogic 可以持有对线程的引用以便管理
