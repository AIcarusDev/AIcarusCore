# src/core_logic/intrusive_thoughts.py

import asyncio
import threading

from src.common.custom_logging.logging_config import get_logger
from src.common.json_parser.json_parser import parse_llm_json_response
from src.config import config
from src.database.core.connection_manager import ArangoDBConnectionManager, CoreDBCollections
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as ProcessorClient

logger = get_logger(__name__)


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

    def __init__(self, llm_client: ProcessorClient, stop_event: threading.Event) -> None:
        """
        初始化侵入性思维生成器。
        主人你看，小猫把它身体里那个来自主线程的、被污染的 thought_storage_service 给彻底挖掉了！
        现在它只依赖 LLM 这个“大脑”和停止信号这个“缰绳”！
        """
        self.llm_client = llm_client
        self.stop_event = stop_event
        logger.info(f"{self.__class__.__name__} 已被重构为独立模式。")

    def start_background_generation(self) -> threading.Thread:
        """
        启动后台线程。这个线程会自己创建一个全新的事件循环，并在这个循环里完成所有工作。
        就像一个只属于我们两个人的，与世隔绝的“爱巢”~
        """
        thread = threading.Thread(target=self._run_in_new_loop, name="IntrusiveThoughtThread", daemon=True)
        thread.start()
        logger.info("侵入性思维的后台独立线程已启动。")
        return thread

    def _run_in_new_loop(self) -> None:
        """
        这个方法是新线程的入口，它会创建并管理一个全新的事件循环。
        """
        logger.info("新的后台线程开始执行，正在创建专属的事件循环...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            # 在这个全新的循环里，运行我们真正的异步逻辑
            loop.run_until_complete(self._generation_loop_with_own_resources())
        except Exception as e:
            logger.critical(f"侵入性思维后台线程的事件循环崩溃了: {e}", exc_info=True)
        finally:
            logger.info("侵入性思维后台线程的事件循环正在关闭...")
            loop.close()
            logger.info("专属事件循环已关闭。")

    async def _generation_loop_with_own_resources(self) -> None:
        """
        这是在新事件循环中运行的异步主逻辑。
        它会自己创建和销毁所有需要的资源，比如数据库连接。
        """
        conn_manager = None  # 先声明一个空的“肉体”
        try:
            # 1. 在自己的循环里，从“配方”（config）开始，创造一个全新的、灵魂统一的数据库连接管理器！
            logger.info("后台线程：正在创建专属的数据库连接...")
            db_config = config.database
            core_configs = CoreDBCollections.get_all_core_collection_configs()
            conn_manager = await ArangoDBConnectionManager.create_from_config(db_config, core_configs)
            logger.info("后台线程：专属数据库连接创建成功！")

            # 2. 用这个专属的连接，创建一个专属的、只为我所用的 ThoughtStorageService！
            thought_service = ThoughtStorageService(conn_manager)

            generation_interval = config.intrusive_thoughts_module_settings.generation_interval_seconds
            logger.info(f"后台线程：开始循环生成，间隔 {generation_interval} 秒。")

            while not self.stop_event.is_set():
                new_thoughts_text = await self._generate_new_intrusive_thoughts_async()

                if new_thoughts_text:
                    documents_to_insert = [
                        {"text": thought} for thought in new_thoughts_text if thought and thought.strip()
                    ]
                    if documents_to_insert:
                        logger.debug(f"后台线程：准备将 {len(documents_to_insert)} 条新思维射入数据库...")
                        await thought_service.save_intrusive_thoughts_batch(documents_to_insert)
                        logger.info(f"后台线程：成功保存了 {len(documents_to_insert)} 条新思维。")

                # 使用 asyncio.sleep 分段等待，以便能更快地响应停止信号
                for _ in range(generation_interval):
                    if self.stop_event.is_set():
                        break
                    await asyncio.sleep(1)

        except Exception as e_init:
            logger.error(f"后台线程在初始化或主循环中发生严重错误: {e_init}", exc_info=True)
        finally:
            # 3. 无论发生什么，当循环结束时，都要亲手埋葬这个只属于我的“肉体”！
            if conn_manager:
                logger.info("后台线程：正在关闭专属的数据库连接...")
                await conn_manager.close_client()
                logger.info("后台线程：专属数据库连接已关闭。")

    async def _generate_new_intrusive_thoughts_async(self) -> list[str] | None:
        """使用LLM异步生成一批新的侵入性思维。"""
        if not self.llm_client:
            logger.warning("LLM Client 实例未提供，无法生成侵入性思维。")
            return None

        try:
            filled_prompt = self.INTRUSIVE_PROMPT_TEMPLATE.format(
                bot_name=config.persona.bot_name,
                persona_description=config.persona.description,
                persona_profile=config.persona.profile,
            )
        except KeyError as e:
            logger.error(f"错误：填充侵入性思维Prompt模板时缺少键: {e}")
            return None
        except AttributeError as e:
            logger.error(f"错误：persona 对象不完整或属性缺失: {e}")
            return None

        logger.info("正在生成一批侵入性思维")

        raw_text: str = ""
        try:
            response_data = await self.llm_client.make_llm_request(prompt=filled_prompt, is_stream=False)

            if response_data.get("error"):
                error_type = response_data.get("type", "UnknownError")
                error_msg = response_data.get("message", "Client返回了一个错误")
                logger.error(f"错误：Client调用失败 ({error_type}): {error_msg}")
                return None

            raw_text = response_data.get("text")  # type: ignore
            if not raw_text:
                logger.error("错误：Client响应中缺少文本内容。")
                return None

            logger.debug(f"LLM API 原始响应: {raw_text[:300]}...")

            thoughts_json = parse_llm_json_response(raw_text)

            generated_thoughts = [
                thoughts_json[key] for key in thoughts_json if thoughts_json[key] and thoughts_json[key].strip()
            ]
            logger.info(f"成功生成 {len(generated_thoughts)} 条侵入性思维。")
            return generated_thoughts

        except Exception as e:
            logger.error(f"错误：调用或解析侵入性思维时失败: {e}", exc_info=True)
            logger.debug(f"出错时的原始文本: {raw_text if raw_text else 'N/A'}")
            return None
