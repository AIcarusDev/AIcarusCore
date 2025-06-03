import asyncio
import datetime
import json
import threading
from typing import TYPE_CHECKING, List, Optional

# 更改导入路径
from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import IntrusiveThoughtsSettings, PersonaSettings
from src.database.arangodb_handler import ArangoDBHandler
from src.llmrequest.llm_processor import Client as ProcessorClient

if TYPE_CHECKING:
    pass

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
    ) -> None:
        # 修改 logger 名称
        self.logger = get_logger("AIcarusCore.plugins.IntrusiveThoughtsGenerator") #
        self.llm_client = llm_client
        self.db_handler = db_handler
        self.persona_cfg = persona_cfg
        self.module_settings = module_settings
        self.stop_event = stop_event
        self._generation_thread: Optional[threading.Thread] = None
        self.logger.info(f"{self.__class__.__name__} instance created.") #

    async def _generate_new_intrusive_thoughts_async(self) -> list[str] | None:
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

        self.logger.info("正在生成一批侵入性思维")

        raw_text: str = ""
        try:
            response_data = await self.llm_client.make_llm_request(prompt=filled_prompt, is_stream=False)

            if response_data.get("error"):
                error_type = response_data.get("type", "UnknownError")
                error_msg = response_data.get("message", "Client返回了一个错误")
                self.logger.error(f"错误：Client调用失败 ({error_type}): {error_msg}")
                return None

            raw_text = response_data.get("text")
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
        generation_interval = self.module_settings.generation_interval_seconds
        self.logger.info(f"侵入性思维生成器后台线程已启动，每 {generation_interval} 秒向 ArangoDB 生成一次。")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while not self.stop_event.is_set():
            new_thoughts: list[str] | None = None
            try:
                new_thoughts = loop.run_until_complete(self._generate_new_intrusive_thoughts_async())
            except RuntimeError as e:
                if "cannot be called from a running event loop" in str(e):
                    self.logger.error(f"Asyncio 运行时错误: {e}. 后台线程可能存在问题。", exc_info=True)
                else:
                    self.logger.error(f"运行异步生成时出错: {e}", exc_info=True)
            except Exception as e_gen:
                self.logger.error(f"运行异步生成时发生未知错误: {e_gen}", exc_info=True)

            if new_thoughts:
                documents_to_insert = [
                    {
                        "text": thought,
                        "timestamp_generated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "used": False,
                    }
                    for thought in new_thoughts
                    if thought
                ]
                if documents_to_insert:
                    try:
                        loop.run_until_complete(self.db_handler.save_intrusive_thoughts_batch(documents_to_insert))
                    except Exception as e_save_batch:
                        self.logger.error(
                            f"调用 db_handler.save_intrusive_thoughts_batch 时出错: {e_save_batch}",
                            exc_info=True,
                        )
            elif new_thoughts is None:
                self.logger.debug("本轮未生成新的侵入性思维或生成失败。")

            wait_completed_or_interrupted = self.stop_event.wait(timeout=float(generation_interval))
            if wait_completed_or_interrupted:
                break
        loop.close()
        self.logger.info("侵入性思维生成器后台线程已停止。")

    def start_background_generation(self) -> threading.Thread | None:
        if not self.module_settings.enabled:
            self.logger.info("侵入性思维模块未启用，不启动后台生成。")
            return None
        if not self.llm_client:
            self.logger.error("侵入性思维LLM客户端未初始化，无法启动后台生成。")
            return None
        if not self.db_handler:
            self.logger.error("数据库处理器未初始化，无法启动后台生成。")
            return None

        if self._generation_thread and self._generation_thread.is_alive():
            self.logger.warning("侵入性思维后台生成线程已在运行，不再重复启动。")
            return self._generation_thread

        thread = threading.Thread(
            target=self._background_generator_thread_target,
            daemon=True,
            name="IntrusiveThoughtsGeneratorThread" # 给线程命名
        )
        thread.start()
        self._generation_thread = thread # 保存线程引用
        self.logger.info("侵入性思维后台生成线程已请求启动。")
        return thread

    def stop_generation_thread(self) -> None:
        """请求停止后台侵入性思维生成线程。"""
        self.logger.info("IntrusiveThoughtsGenerator 收到停止线程请求。")
        self.stop_event.set() # 设置事件来通知线程停止
        if self._generation_thread and self._generation_thread.is_alive():
            # 可以选择在这里等待线程结束，但通常对于daemon线程不是强制的，因为主程序退出时它会自动终止
            # self._generation_thread.join(timeout=5) # 等待最多5秒
            # if self._generation_thread.is_alive():
            #     self.logger.warning("侵入性思维生成线程未能优雅停止。")
            pass # 线程会在下一个循环检查到 stop_event.is_set() 后退出
        else:
            self.logger.info("侵入性思维生成线程未运行或已停止。")
