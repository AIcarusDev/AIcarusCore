# src/core_logic/intrusive_thoughts.py
import asyncio
import datetime
import json
import threading
from typing import TYPE_CHECKING, List # 确保 List 也导入了

from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import IntrusiveThoughtsSettings, PersonaSettings
# from src.database.arangodb_handler import ArangoDBHandler # 小猫咪把这个旧情人赶走了
from src.database.services.thought_storage_service import ThoughtStorageService # 迎来了新的性感尤物！
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
        thought_storage_service: ThoughtStorageService, # 亲爱的，这里换成了新的性感服务哦！
        persona_cfg: PersonaSettings,
        module_settings: IntrusiveThoughtsSettings,
        stop_event: threading.Event,
    ) -> None:
        """
        初始化侵入性思维生成器。

        :param llm_client: 用于生成思维的LLM客户端。
        :param thought_storage_service: ThoughtStorageService 实例用于数据库操作。 # 注释也更新了，多体贴！
        :param persona_cfg: 人格配置。
        :param module_settings: 侵入性思维模块的特定设置。
        :param stop_event: 用于停止后台生成线程的事件。
        """
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        self.llm_client = llm_client
        self.thought_storage_service = thought_storage_service # 实例变量也换成了新的性感服务！
        self.persona_cfg = persona_cfg
        self.module_settings = module_settings
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
        except AttributeError as e: # 确保 persona_cfg 对象及其属性有效
            self.logger.error(f"错误：persona_cfg 对象不完整或属性缺失: {e}")
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

            raw_text = response_data.get("text") # type: ignore
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
        self.logger.info(f"侵入性思维生成器后台线程已启动，每 {generation_interval} 秒向数据库深处播种一次。")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while not self.stop_event.is_set():
            new_thoughts: list[str] | None = None
            try:
                new_thoughts = loop.run_until_complete(self._generate_new_intrusive_thoughts_async())
            except RuntimeError as e:
                if "cannot be called from a running event loop" in str(e):
                    self.logger.error(f"Asyncio 运行时错误: {e}. 后台线程的小穴可能出问题了。", exc_info=True)
                else:
                    self.logger.error(f"运行异步生成时碰到敏感点了: {e}", exc_info=True)
            except Exception as e_gen:
                self.logger.error(f"运行异步生成时发生了意想不到的快感（错误）: {e_gen}", exc_info=True)

            if new_thoughts: # 确保 new_thoughts 不是 None 并且包含内容
                # 亲爱的，我们现在构造的文档列表，每个字典只需要包含 "text" 就好啦！
                # ThoughtStorageService 的小穴会帮我们处理 _key, timestamp_generated, used 这些细节的！
                documents_to_insert = [
                    {"text": thought}
                    for thought in new_thoughts
                    if thought and isinstance(thought, str) and thought.strip() # 确保 thought 是非空字符串
                ]
                if documents_to_insert:
                    try:
                        # 现在我们用新的性感服务来保存这些小骚货！
                        loop.run_until_complete(self.thought_storage_service.save_intrusive_thoughts_batch(documents_to_insert))
                    except Exception as e_save_batch:
                        self.logger.error(
                            f"调用 thought_storage_service.save_intrusive_thoughts_batch 射精时出错: {e_save_batch}",
                            exc_info=True,
                        )
            elif new_thoughts is None: # LLM 生成失败
                self.logger.debug("本轮一个骚点子都没射出来，或者生成失败了。")

            wait_completed_or_interrupted = self.stop_event.wait(timeout=float(generation_interval))
            if wait_completed_or_interrupted: # 如果事件被设置（即停止信号）
                self.logger.info("后台线程在贤者时间收到了主人的停止命令，不生成了。")
                break # 退出循环
        loop.close()
        self.logger.info("侵入性思维生成器的后台线程已经爽翻了，停止了。")

    def start_background_generation(self) -> threading.Thread | None:
        """启动后台侵入性思维生成线程。"""
        if not self.module_settings.enabled:
            self.logger.info("侵入性思维模块未启用，小猫咪不给主人生成骚点子。")
            return None
        if not self.llm_client:
            self.logger.error("侵入性思维LLM的肉棒未初始化，无法启动后台生成。")
            return None
        if not self.thought_storage_service: # 主人你看，这里也检查了新的性感服务！
            self.logger.error("思考存储服务 (ThoughtStorageService) 的小穴未初始化，无法启动后台生成。")
            return None

        thread = threading.Thread(
            target=self._background_generator_thread_target,
            daemon=True, # 设为守护线程，主程序退出时它也会退出，像个懂事的小母猫
        )
        thread.start()
        self.logger.info("侵入性思维的后台生成线程已经饥渴地请求启动了。")
        return thread