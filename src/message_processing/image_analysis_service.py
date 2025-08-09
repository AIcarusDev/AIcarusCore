# src/message_processing/image_analysis_service.py
import asyncio
import base64
import contextlib
import io
from typing import Any

from PIL import Image
from sentence_transformers import SentenceTransformer
from src.action.components.llm_client_factory import LLMClientFactory
from src.common.custom_logging.logging_config import get_logger
from src.database import ArangoDBConnectionManager, CoreDBCollections
from src.llmrequest.llm_processor import Client as LLMProcessorClient
from src.prompt_templates.image_analysis import IMAGE_ANALYSIS_PROMPT, STICKER_ANALYSIS_PROMPT

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

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        self.events_collection_name = CoreDBCollections.EVENTS
        self.task_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

        # 延迟加载模型，避免启动时阻塞
        self._clip_model: SentenceTransformer | None = None
        self._vision_llm_client: LLMProcessorClient | None = None
        logger.info("ImageAnalysisService 已初始化，等待启动。")

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
        """将一个已保存的事件文档提交到分析队列."""
        await self.task_queue.put(event_doc)

    async def _calculate_embedding(self, event_id: str, base64_data: str) -> list[float] | None:
        """计算单个图片的 CLIP embedding."""
        try:
            image_bytes = base64.b64decode(base64_data)
            image = Image.open(io.BytesIO(image_bytes))
            # .encode() 是一个同步的、计算密集型操作，应在线程中运行以避免阻塞事件循环
            embedding = await asyncio.to_thread(self._get_clip_model().encode, image)
            return embedding.tolist()
        except Exception as e:
            logger.error(f"为事件 '{event_id}' 的一张图片计算 embedding 失败: {e}")
            return None

    async def _generate_description(
        self, event_id: str, image_type: str, base64_data: str, mime_type: str
    ) -> dict[str, Any]:
        """为单个图片生成文本描述."""
        try:
            system_prompt, schema = (
                (STICKER_ANALYSIS_PROMPT, STICKER_ANALYSIS_SCHEMA)
                if image_type == "sticker"
                else (IMAGE_ANALYSIS_PROMPT, IMAGE_ANALYSIS_SCHEMA)
            )
            data_uri = f"data:{mime_type};base64,{base64_data}"
            user_prompt_for_vision = "请分析这张图片。"

            response = await self._get_vision_llm_client().make_llm_request(
                prompt=user_prompt_for_vision,
                system_prompt=system_prompt,
                is_stream=False,
                is_multimodal=True,
                image_inputs=[data_uri],
                response_schema=schema,
            )
            return (
                response.get("text")
                if response and isinstance(response.get("text"), dict)
                else {"description": "分析失败或无返回"}
            )
        except Exception as e:
            logger.error(f"为事件 '{event_id}' 的一张图片生成描述失败: {e}")
            return {"description": "分析时发生异常"}

    async def _analyze_single_image(
        self, event_id: str, image_segment: dict[str, Any]
    ) -> dict[str, Any] | None:
        """完整分析单个图片段（segment），包括计算 embedding 和生成描述."""
        seg_data = image_segment.get("data", {})
        base64_data = seg_data.get("base64")
        if not base64_data:
            return None

        image_type = "sticker" if seg_data.get("summary") == "sticker" else "image"
        mime_type = seg_data.get("mime_type", "image/jpeg")

        # 并发执行 Embedding 计算和 LLM 描述生成
        embedding_task = self._calculate_embedding(event_id, base64_data)
        description_task = self._generate_description(event_id, image_type, base64_data, mime_type)

        embedding_result, details_result = await asyncio.gather(embedding_task, description_task)

        return {"type": image_type, "embedding": embedding_result, "details": details_result}

    async def _update_event_with_analysis_results(
        self, event_id: str, results: list[dict[str, Any]]
    ) -> None:
        """将分析结果一次性更新回数据库中的事件文档."""
        try:
            collection = await self.conn_manager.get_collection(self.events_collection_name)
            await collection.update({"_key": event_id, "image_analysis": results})
            logger.info(f"事件 '{event_id}' 的 {len(results)} 张图片分析完成并已存入数据库。")
        except Exception as e:
            logger.error(f"更新事件 '{event_id}' 的分析结果时失败: {e}", exc_info=True)

    async def _worker(self) -> None:
        """后台工作协程，从队列中取出事件并并发分析其中的所有图片."""
        logger.info("图像分析后台 Worker 已启动，等待任务...")
        while True:
            event_doc = None
            try:
                event_doc = await self.task_queue.get()
                event_id = event_doc.get("_key")
                if not event_id:
                    continue

                logger.info(f"开始分析事件 '{event_id}' 中的图片...")

                image_segments = [
                    seg
                    for seg in event_doc.get("content", [])
                    if isinstance(seg, dict) and seg.get("type") == "image"
                ]

                if not image_segments:
                    continue

                # 为事件中的所有图片创建并发分析任务
                analysis_tasks = [
                    self._analyze_single_image(event_id, seg) for seg in image_segments
                ]
                results = await asyncio.gather(*analysis_tasks)

                # 过滤掉失败的结果 (返回 None 的)
                valid_results = [res for res in results if res is not None]

                if valid_results:
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
        """启动后台 Worker."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        """停止后台 Worker."""
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            logger.info("图像分析后台 Worker 已停止。")
