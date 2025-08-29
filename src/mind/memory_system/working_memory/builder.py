# src/mind/memory_system/working_memory/builder.py
import json
from typing import Any

from src.config import config

# 导入 ThoughtChainDocument
from src.services.database.models import ThoughtChainDocument

from .models import MemoryFragment


class WorkingMemoryBuilder:
    """负责构建结构化的、带衰减机制的工作记忆."""

    def __init__(self) -> None:
        self._settings = config.working_memory
        self._clarity_levels = self._settings.clarity_levels

        # 预先计算边界，提高效率
        self._vivid_end = self._clarity_levels.vivid
        self._active_end = self._vivid_end + self._clarity_levels.active
        self._retained_end = self._active_end + self._clarity_levels.retained
        self._decaying_end = self._retained_end + self._clarity_levels.decaying

    def get_memory_depth(self) -> int:
        """返回配置的记忆深度."""
        return self._settings.depth

    def _get_memory_status(self, cycle_ago: int) -> str:
        """根据周期和配置决定记忆状态."""
        if cycle_ago <= self._vivid_end:
            return "vivid"
        if cycle_ago <= self._active_end:
            return "active"
        if cycle_ago <= self._retained_end:
            return "retained"
        return "decaying"

    def _extract_memory_content(
            self,
            thought: ThoughtChainDocument,
            status: str
        ) -> dict[str, Any] | None:
        """[核心] JSON 提取器，根据记忆状态对完整的思考文档进行衰减."""
        full_response = thought.action_payload
        if not full_response:
            return None

        if status in ["vivid", "active"]:
            return full_response

        if status == "retained":
            retained_content = {}
            if "internal_state" in full_response and "intent" in full_response["internal_state"]:
                retained_content[
                    "internal_state"
                    ] = {"intent": full_response["internal_state"]["intent"]}
            if "action" in full_response:
                retained_content["action"] = full_response["action"]
            return retained_content if retained_content else None

        if status == "decaying":
            decaying_content = {}
            if "action" in full_response:
                decaying_content["action"] = full_response["action"]
            elif "internal_state" in full_response and "intent" in full_response["internal_state"]:
                decaying_content[
                    "internal_state"
                    ] = {"intent": full_response["internal_state"]["intent"]}
            return decaying_content if decaying_content else None

        return None

    def build_fragments(
        self,
        recent_thoughts: list[ThoughtChainDocument],
        last_external_info_snapshot: str | None
    ) -> list[MemoryFragment]:
        """从数据库文档列表构建记忆片段列表."""
        fragments = []
        for i, thought in enumerate(recent_thoughts):
            cycle_ago = i + 1
            status = self._get_memory_status(cycle_ago)

            # 我们需要从 thought 文档中提取所有需要的信息
            content = self._extract_memory_content(thought, status)

            # 如果衰减后主要内容为空，则不创建此记忆片段
            if not content:
                continue

            # 只在 cycle_ago == 1 时，使用内存中的快照
            if cycle_ago == 1 and status == "vivid" and last_external_info_snapshot:
                content["_external_info_snapshot"] = last_external_info_snapshot

            if thought.action_result:
                content["_action_result"] = thought.action_result

            fragments.append(MemoryFragment(
                cycle_ago=cycle_ago,
                status=status,
                content=content
            ))
        return fragments

    def render_to_xml_string(self, fragments: list[MemoryFragment]) -> str:
        """将记忆片段列表渲染为最终的 XML 字符串."""
        if not fragments:
            return (
                '<working_memories scope="short_term_buffer" time_unit="cognitive_cycle" order="descending">\n'  # noqa: E501
                '  <memory cycle_ago="more" status="forgotten"/>\n'
                '</working_memories>'
            )

        lines = [
            '<working_memories scope="short_term_buffer" time_unit="cognitive_cycle" order="descending">',  # noqa: E501
        ]
        for frag in fragments:
            lines.append(f'  <memory cycle_ago="{frag.cycle_ago}" status="{frag.status}">')

            # 提取特殊内容
            external_info = frag.content.pop("_external_info_snapshot", None)
            action_result = frag.content.pop("_action_result", None)

            # 渲染 <external_info>
            if external_info:
                # 压缩到一行
                compressed_xml = " ".join(external_info.split())
                lines.append(f"    <external_info><![CDATA[{compressed_xml}]]></external_info>")

            # 渲染 <response>
            # frag.content 现在是纯粹的上一轮输出了
            content_json = json.dumps(frag.content, ensure_ascii=False, separators=(",", ":"))
            lines.append(f"    <response><![CDATA[{content_json}]]></response>")

            # 渲染 <additional_content> (用于慢思考等动作结果)
            if action_result:
                # 压缩到一行
                compressed_action_result = " ".join(action_result.split())
                lines.append(
                    f"    <additional_content><![CDATA[{compressed_action_result}]]></additional_content>"  # noqa: E501
                    )

            lines.append("  </memory>")

        # 添加 forgotten 标记
        if len(fragments) >= self.get_memory_depth():
            lines.append('  <memory cycle_ago="more" status="forgotten"/>')

        lines.append('</working_memories>')
        return "\n".join(lines)
