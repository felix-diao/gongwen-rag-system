"""Prompt helpers for local meeting minutes generation."""

from __future__ import annotations


class _ContentTier:
    """根据转写字数和录音时长划分的内容丰富度等级。"""

    def __init__(
        self,
        name: str,
        min_chars: int,
        min_seconds: int,
        target_chars_min: int,
        target_chars_max: int,
        max_tokens: int,
        section_hints: str,
    ) -> None:
        self.name = name
        self.min_chars = min_chars
        self.min_seconds = min_seconds
        self.target_chars_min = target_chars_min
        self.target_chars_max = target_chars_max
        self.max_tokens = max_tokens
        self.section_hints = section_hints


_TIERS = [
    _ContentTier(
        name="简短",
        min_chars=0,
        min_seconds=0,
        target_chars_min=200,
        target_chars_max=400,
        max_tokens=1200,
        section_hints="**会议概要**、**关键结论**",
    ),
    _ContentTier(
        name="中等",
        min_chars=500,
        min_seconds=3 * 60,
        target_chars_min=400,
        target_chars_max=800,
        max_tokens=1800,
        section_hints="**会议背景**、**关键讨论**、**达成的结论**、**后续安排**",
    ),
    _ContentTier(
        name="详细",
        min_chars=2000,
        min_seconds=15 * 60,
        target_chars_min=800,
        target_chars_max=1500,
        max_tokens=2500,
        section_hints="**会议背景**、**关键讨论**、**决策与共识**、**风险与问题**、**后续安排**",
    ),
    _ContentTier(
        name="超详细",
        min_chars=5000,
        min_seconds=45 * 60,
        target_chars_min=1200,
        target_chars_max=2500,
        max_tokens=4000,
        section_hints="**会议背景与目标**、**各议题关键讨论**、**决策与共识**、**风险与问题**、**详细后续安排与时间线**、**责任分工**",
    ),
]


def resolve_content_tier(char_count: int, duration_seconds: float) -> _ContentTier:
    """按字数或时长取最高等级。"""
    duration_int = int(duration_seconds or 0)
    selected = _TIERS[0]
    for tier in _TIERS:
        if char_count >= tier.min_chars or duration_int >= tier.min_seconds:
            selected = tier
    return selected


def build_local_minutes_llm_instruction(
    char_count: int,
    duration_seconds: float,
) -> str:
    tier = resolve_content_tier(char_count, duration_seconds)
    duration_minutes = round((duration_seconds or 0) / 60, 1)

    return f"""你是企业会议纪要助手。请严格基于会议转写文本生成会议纪要，并输出严格 JSON。
不要输出任何解释、前后缀、额外说明或 Markdown 代码块。
输出格式必须为：{{"summary":{{"title":"string","paragraph":"string"}},"todos":[{{"content":"string","executor":"string|null","execution_time":"string|null"}}]}}
本次会议转写稿约 {char_count} 个字符，录音时长约 {duration_minutes} 分钟。根据内容规模，本次摘要等级为「{tier.name}」。
要求如下：
1. 整个响应必须是可被 json.loads 直接解析的 JSON 对象。
2. summary.title 必须是简洁明确的会议摘要标题，长度控制在 8-20 个汉字。
3. summary.paragraph 必须是详细摘要，不能只写一句话，目标字数控制在 {tier.target_chars_min}-{tier.target_chars_max} 个汉字之间。若转写信息不足以达到目标字数，不要编造，应在结尾说明“转写信息有限”。
4. summary.paragraph 必须使用轻量 Markdown 子集，但它本质上仍然是 JSON 中的字符串字段。
5. 只允许以下 Markdown 格式：小节标题单独成行并使用 **标题**；列表项必须使用 '- ' 开头。
6. 默认使用单个换行组织结构，不要连续输出空行；如无必要，不要在小节标题前后额外插入空白行。
7. 如需换行，必须在 JSON 字符串里使用 \\n，不要输出会破坏 JSON 的非法换行。
8. 不要使用 # 标题、数字列表、表格、代码块、HTML 或引用块。
9. 摘要必须尽量覆盖会议背景或主题、关键讨论点、达成的结论或共识、后续安排或风险提醒。等级越高，小节应越丰富、每个要点可适当展开。
10. 如果转写中的信息不足或不明确，不要编造；只总结能够明确判断的内容，并明确说明“转写信息有限”。
11. todos 只提取转写中明确提到的待办事项；如果没有明确待办，返回空数组 []。
12. executor 和 execution_time 只有在转写中明确出现时才填写，否则必须为 null。
13. 不要照抄整段转写，要进行归纳、压缩和重组，但必须保留关键信息。
14. 只允许输出 JSON 对象。
15. 建议的小节结构（等级为「{tier.name}」）：{tier.section_hints}。请按此组织，但不要为了凑小节而重复内容。
示例：
{{
  "summary": {{
    "title": "项目进展与后续安排",
    "paragraph": "本次会议围绕项目推进、风险处理和下阶段安排进行了集中讨论。\\n**关键进展**\\n- 已完成接口联调\\n- 测试环境问题已定位\\n**风险与问题**\\n- 部分数据仍待业务确认\\n- 上线时间受外部依赖影响\\n**后续安排**\\n- 本周内补齐测试数据\\n- 下周提交上线评审"
  }},
  "todos": [
    {{"content": "补齐测试数据", "executor": "测试负责人", "execution_time": "本周内"}}
  ]
}}
"""


def resolve_max_tokens(char_count: int, duration_seconds: float) -> int:
    """根据内容规模决定 LLM 调用的 max_tokens。"""
    return resolve_content_tier(char_count, duration_seconds).max_tokens
