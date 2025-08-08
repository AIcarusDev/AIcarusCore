# src/message_processing/image_analysis_service.py
import asyncio
import base64
import io

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
            "type": ["string", "null"],
            "description": "图片中提取出的文字内容，如果没有则为 null。",
        },
    },
    "required": ["primary_subject", "description", "text_content"],
}


class ImageAnalysisService:
    """一个后台服务，负责异步地分析事件中的图片内容."""

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        self.events_collection_name = CoreDBCollections.EVENTS
        self.task_queue = asyncio.Queue()
        self._worker_task = None

        # 延迟加载模型，避免启动时阻塞
        self._clip_model = None
        self._vision_llm_client = None
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
            # 我们复用 action 模块的工厂来创建一个客户端
            # 假设配置文件中有一个名为 'image_analysis' 的模型用途配置
            factory = LLMClientFactory()
            try:
                # !!重要!!: 你需要在 config_template.toml 中添加 [llm_models.image_analysis] 配置
                self._vision_llm_client = factory.create_client(purpose_key="image_analysis")
                logger.info("Vision LLM 客户端创建成功。")
            except RuntimeError as e:
                logger.critical(f"创建 Vision LLM 客户端失败: {e}。图像描述功能将不可用。")
                # 返回一个虚拟的客户端或直接抛出异常，这里选择让它失败
                raise
        return self._vision_llm_client

    async def submit_event_for_analysis(self, event_doc: dict) -> None:
        """将一个已保存的事件文档提交到分析队列."""
        await self.task_queue.put(event_doc)

    async def _worker(self) -> None:
        """后台工作协程，从队列中取出事件并进行分析."""
        logger.info("图像分析后台 Worker 已启动，等待任务...")
        while True:
            try:
                event_doc = await self.task_queue.get()
                event_id = event_doc.get("_key")

                logger.info(f"开始分析事件 '{event_id}' 中的图片...")
                analysis_results = []

                image_segments = [
                    seg
                    for seg in event_doc.get("content", [])
                    if isinstance(seg, dict) and seg.get("type") == "image"
                ]

                if not image_segments:
                    self.task_queue.task_done()
                    continue

                for seg in image_segments:
                    image_type = (
                        "sticker" if seg.get("data", {}).get("summary") == "sticker" else "image"
                    )
                    base64_data = seg.get("data", {}).get("base64")

                    if not base64_data:
                        continue

                    # 1. 计算 Embedding
                    try:
                        image_bytes = base64.b64decode(base64_data)
                        image = Image.open(io.BytesIO(image_bytes))
                        embedding = self._get_clip_model().encode(image).tolist()
                    except Exception as e:
                        logger.error(f"为事件 '{event_id}' 的一张图片计算 embedding 失败: {e}")
                        embedding = None

                    # 2. 生成描述
                    try:
                        # 根据类型选择 System Prompt
                        if image_type == "sticker":
                            system_prompt = STICKER_ANALYSIS_PROMPT
                            schema = STICKER_ANALYSIS_SCHEMA
                        else:
                            system_prompt = IMAGE_ANALYSIS_PROMPT
                            schema = IMAGE_ANALYSIS_SCHEMA
                        # 将 base64 数据转换为 Data URI
                        mime_type = seg.get("data", {}).get("mime_type", "image/jpeg")
                        data_uri = f"data:{mime_type};base64,{base64_data}"

                        # User Prompt 可以非常简洁，甚至为空，因为核心数据是图片本身
                        user_prompt_for_vision = "请分析这张图片。"

                        response = await self._get_vision_llm_client().make_llm_request(
                            prompt=user_prompt_for_vision,
                            system_prompt=system_prompt,
                            is_stream=False,
                            is_multimodal=True,
                            image_inputs=[data_uri],
                            response_schema=schema,
                        )

                        details = (
                            response.get("text")
                            if response and isinstance(response.get("text"), dict)
                            else {}
                        )

                    except Exception as e:
                        logger.error(f"为事件 '{event_id}' 的一张图片生成描述失败: {e}")
                        details = {"description": "分析失败"}

                    analysis_results.append(
                        {"type": image_type, "embedding": embedding, "details": details}
                    )

                # 3. 将分析结果更新回数据库
                if analysis_results:
                    collection = await self.conn_manager.get_collection(self.events_collection_name)
                    await collection.update({"_key": event_id, "image_analysis": analysis_results})
                    logger.info(
                        f"事件 '{event_id}' 的 {len(analysis_results)} 张图片"
                        f"分析完成并已存入数据库。"
                    )

                self.task_queue.task_done()

            except asyncio.CancelledError:
                logger.info("图像分析 Worker 被取消。")
                break  # 退出循环
            except Exception as e:
                event_id_for_log = event_doc.get("_key") if event_doc else "未知"
                logger.error(f"分析事件 '{event_id_for_log}' 时发生未知错误: {e}", exc_info=True)
            finally:
                # 无论成功、失败还是取消，只要我们从队列中取出了一个任务，就必须调用 task_done()
                if event_doc is not None:
                    self.task_queue.task_done()

    def start(self) -> None:
        """启动后台 Worker."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        """停止后台 Worker."""
        if self._worker_task:
            self.task_queue.put_nowait(None)  # 发送停止信号
            self._worker_task.cancel()
            import contextlib

            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            logger.info("图像分析后台 Worker 已停止。")
