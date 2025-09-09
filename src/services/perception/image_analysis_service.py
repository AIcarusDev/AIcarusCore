# src/message_processing/image_analysis_service.py
import asyncio
import base64
import contextlib
import hashlib
import io
import threading
from typing import Any

from PIL import Image
from sentence_transformers import SentenceTransformer
from src.common.custom_logging.logging_config import get_logger
from src.common.json_parser.json_parser import parse_llm_json_response
from src.config.aicarus_configs import FeatureFlags
from src.prompting.templates.image_analysis import IMAGE_ANALYSIS_PROMPT, STICKER_ANALYSIS_PROMPT
from src.services.action.components.llm_client_factory import LLMClientFactory
from src.services.database import CoreDBCollections, TypeDBConnectionManager
from src.services.database.services.media_cache_service import MediaCacheService
from src.services.llmrequest.llm_processor import Client as LLMProcessorClient

logger = get_logger(__name__)


# --- 为输出定义的 JSON Schema ---
STICKER_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "emotion": {"type": "string", "description": "表情包传达的主要情绪。"},
        "description": {
            "type": "string",
            "description": "对表情包内容的简洁描述，优先体现梗或含义。",
        },
    },
    "required": ["emotion", "description"],
}

IMAGE_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "primary_subject": {"type": "string", "description": "图片最主要的焦点或主体。"},
        "description": {
            "type": "string",
            "description": "对图片场景、物体、人物和事件的详细描述。",
        },
        "text_content": {
            "type": "string",
            "description": "图片中提取出的文字内容，如果没有则省略此字段。",
        },
    },
    "required": ["primary_subject", "description"],
}


class ImageAnalysisService:
    """一个后台服务，负责异步地分析事件中的图片内容."""

    CACHE_VERSION = "v1.0"
    CACHE_TTL_SECONDS = 7 * 24 * 3600

    def __init__(
        self,
        conn_manager: TypeDBConnectionManager,
        cache_service: MediaCacheService,
        feature_flags: FeatureFlags,
    ) -> None:
        self.conn_manager = conn_manager
        self.cache_service = cache_service
        self.feature_flags = feature_flags
        self.events_collection_name = CoreDBCollections.EVENTS
        self.task_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._start_lock = threading.Lock()

        # 等待机制的核心
        self._pending_analysis: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending_analysis_lock = asyncio.Lock()

        # 后台任务管理
        self._background_tasks: set[asyncio.Task] = set()

        self._clip_model: SentenceTransformer | None = None
        self._vision_llm_client: LLMProcessorClient | None = None
        logger.info("ImageAnalysisService 已初始化 (混合模式升级版)。")

    # 公共接口：获取分析结果（带等待机制）
    async def get_analysis_result(
        self, image_hash: str, base64_data: str, seg_data: dict
    ) -> dict[str, Any] | None:
        """获取单张图片的分析结果.

        此方法会优先检查缓存，如果未命中，则会检查是否有正在进行的分析任务.
        如果没有，则会启动一个新的分析任务并等待其完成.
        """
        try:
            # 1. 检查数据库缓存
            cached_result = await self.cache_service.get_analysis_by_hash(
                image_hash, version=self.CACHE_VERSION, ttl_seconds=self.CACHE_TTL_SECONDS
            )
            if cached_result:
                return cached_result

            # 2. 缓存未命中，进入等待或执行流程
            async with self._pending_analysis_lock:
                if image_hash in self._pending_analysis:
                    # 如果已经有其他任务在分析这张图，就一起等结果
                    logger.debug(f"图片 {image_hash[:10]}... 已有分析任务，加入等待队列。")
                    future = self._pending_analysis[image_hash]
                else:
                    # 如果是第一个来的，就创建 Future，启动分析任务
                    logger.debug(
                        f"图片 {image_hash[:10]}... 无分析任务，创建新的 Future 并启动分析。"
                    )
                    future = asyncio.Future()
                    self._pending_analysis[image_hash] = future
                    # 创建一个独立的任务去执行真正的分析，避免阻塞当前协程
                    task = asyncio.create_task(
                        self._execute_analysis_and_set_future(
                            image_hash, base64_data, seg_data, future
                        )
                    )
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)

            try:
                # 等待 Future 被设置结果
                return await future
            except Exception as e:
                logger.error(f"等待图片 {image_hash[:10]}... 分析结果时发生错误: {e}")
                return None
        except Exception as e:
            logger.error(f"获取图片 {image_hash[:10]}... 分析结果时发生缓存服务错误: {e}")
            return None

    async def _execute_analysis_and_set_future(
        self, image_hash: str, base64_data: str, seg_data: dict, future: asyncio.Future
    ) -> None:
        """一个包装器，执行实际的分析，并将结果或异常设置到 Future 上，最后清理等待室."""
        try:
            # 执行耗时的分析工作
            result = await self._analyze_single_image_core(base64_data, seg_data)
            future.set_result(result)
        except Exception as e:
            future.set_exception(e)
        finally:
            # 无论成功失败，都从等待室中移除
            async with self._pending_analysis_lock:
                self._pending_analysis.pop(image_hash, None)

    def _get_clip_model(self) -> SentenceTransformer:
        if self._clip_model is None:
            logger.info("正在加载 CLIP embedding 模型 (clip-ViT-B-32)...")
            self._clip_model = SentenceTransformer("clip-ViT-B-32")
            logger.info("CLIP embedding 模型加载完成。")
        return self._clip_model

    def _get_vision_llm_client(self) -> LLMProcessorClient:
        if self._vision_llm_client is None:
            logger.info("正在创建用于图像分析的 Vision LLM 客户端...")
            factory = LLMClientFactory()
            try:
                self._vision_llm_client = factory.create_client(purpose_key="image_analysis")
                logger.info("Vision LLM 客户端创建成功。")
            except RuntimeError as e:
                logger.critical(f"创建 Vision LLM 客户端失败: {e}。图像描述功能将不可用。")
                raise
        return self._vision_llm_client

    async def submit_event_for_analysis(self, event_doc: dict) -> None:
        """将一个已保存的事件文档提交到分析队列 (纯后台处理)."""
        await self.task_queue.put(event_doc)

    def _calculate_image_hash(self, base64_data: str) -> str:
        image_bytes = base64.b64decode(base64_data)
        return hashlib.sha256(image_bytes).hexdigest()

    async def _calculate_embedding(self, base64_data: str) -> list[float] | None:
        if not self.feature_flags.enable_vector_embedding:
            return None
        try:
            image_bytes = base64.b64decode(base64_data)
            image = Image.open(io.BytesIO(image_bytes))
            embedding = await asyncio.to_thread(self._get_clip_model().encode, image)
            return embedding.tolist()
        except Exception as e:
            logger.error(f"计算图片 embedding 失败: {e}")
            return None

    async def _generate_description(
        self, image_type: str, base64_data: str, mime_type: str
    ) -> dict[str, Any]:
        if not self.feature_flags.enable_image_to_text:
            return {"description": "Image to text feature is disabled."}
        try:
            system_prompt, schema = (
                (STICKER_ANALYSIS_PROMPT, STICKER_ANALYSIS_SCHEMA)
                if image_type == "sticker"
                else (IMAGE_ANALYSIS_PROMPT, IMAGE_ANALYSIS_SCHEMA)
            )
            data_uri = f"data:{mime_type};base64,{base64_data}"
            user_prompt_for_vision = "请分析这张图片。\n[图片_1]"

            response = await self._get_vision_llm_client().make_llm_request(
                prompt=user_prompt_for_vision,
                system_prompt=system_prompt,
                is_stream=False,
                is_multimodal=True,
                image_inputs=[data_uri],
                response_schema=schema,
            )

            raw_text = response.get("text") if response else None
            if raw_text and isinstance(raw_text, str):
                # 记录原始响应以便调试
                logger.debug(f"LLM 原始响应: {raw_text[:200]}...")

                parsed_json = parse_llm_json_response(raw_text)
                if isinstance(parsed_json, dict):
                    return parsed_json
                else:
                    logger.warning(f"JSON 解析结果不是字典: {type(parsed_json)}")
            return {"description": "分析失败或无返回"}
        except Exception as e:
            logger.error(f"生成图片描述失败: {e}", exc_info=True)
            return {"description": "分析时发生异常"}

    async def _analyze_single_image_core(self, base64_data: str, seg_data: dict) -> dict[str, Any]:
        """实际执行分析的核心逻辑，不再关心缓存."""
        image_type = "sticker" if seg_data.get("summary") == "sticker" else "image"
        mime_type = seg_data.get("mime_type", "image/jpeg")

        embedding_task = self._calculate_embedding(base64_data)
        description_task = self._generate_description(image_type, base64_data, mime_type)
        embedding_result, details_result = await asyncio.gather(embedding_task, description_task)

        analysis_result = {
            "type": image_type,
            "embedding": embedding_result,
            "details": details_result,
        }

        # 分析完成后，将结果存入缓存
        image_hash = self._calculate_image_hash(base64_data)
        await self.cache_service.save_analysis(
            image_hash, analysis_result, version=self.CACHE_VERSION
        )

        return analysis_result

    async def _update_event_with_analysis_results(
        self, event_id: str, results: list[dict[str, Any]]
    ) -> None:
        try:
            collection = await self.conn_manager.get_collection(self.events_collection_name)
            await collection.update({"_key": event_id, "image_analysis": results})
            logger.info(f"事件 '{event_id}' 的 {len(results)} 张图片分析完成并已存入数据库。")
        except Exception as e:
            logger.error(f"更新事件 '{event_id}' 的分析结果时失败: {e}", exc_info=True)

    async def _worker(self) -> None:
        """后台工作协程，处理纯后台的分析任务."""
        logger.info("图像分析后台 Worker 已启动，等待任务...")
        while True:
            event_doc = None
            try:
                event_doc = await self.task_queue.get()
                event_id = event_doc.get("_key")
                if not event_id:
                    continue

                logger.info(f"后台 Worker 开始分析事件 '{event_id}' 中的图片...")

                image_segments = [
                    seg
                    for seg in event_doc.get("content", [])
                    if isinstance(seg, dict)
                    and seg.get("type") == "image"
                    and seg.get("data", {}).get("base64")
                ]

                if not image_segments:
                    continue

                analysis_tasks = []
                for seg in image_segments:
                    seg_data = seg.get("data", {})
                    base64_data = seg_data.get("base64")
                    image_hash = self._calculate_image_hash(base64_data)
                    # 调用新的 get_analysis_result，它会自动处理缓存和并发
                    analysis_tasks.append(
                        self.get_analysis_result(image_hash, base64_data, seg_data)
                    )

                results = await asyncio.gather(*analysis_tasks)

                if valid_results := [res for res in results if res is not None]:
                    await self._update_event_with_analysis_results(event_id, valid_results)

            except asyncio.CancelledError:
                logger.info("图像分析 Worker 被取消。")
                break
            except Exception as e:
                event_id_for_log = event_doc.get("_key") if event_doc else "未知"
                logger.error(f"分析事件 '{event_id_for_log}' 时发生未知错误: {e}", exc_info=True)
            finally:
                if event_doc is not None:
                    self.task_queue.task_done()

    def start(self) -> None:
        """启动后台 Worker (线程/协程安全)."""
        with self._start_lock:
            if self._worker_task is None or self._worker_task.done():
                self._worker_task = asyncio.create_task(self._worker())
                logger.info("图像分析后台 Worker 任务已创建并启动。")
            else:
                logger.debug("图像分析后台 Worker 已在运行，无需重复启动。")

    async def stop(self) -> None:
        """停止后台 Worker."""
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None
            logger.info("图像分析后台 Worker 已停止。")
