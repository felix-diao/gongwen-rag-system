from .client import LLMClient
from .config import LLMConfig
from .utils import map_doc_type, map_tone

_client = None
def get_client(cfg=None):
    global _client
    if _client is None:
        _client = LLMClient(cfg or LLMConfig())
    return _client

def generate_document(title: str, requirement: str) -> str:
    cli = get_client()
    if not cli.cfg.api_key:
        return f"{title}\n\n根据{requirement}要求，现制定如下通知：\n\n一、遵守规范格式；\n二、明确分工；\n三、认真落实。\n\n特此通知。"
    messages = [
        {"role": "system", "content": "你是一名资深公文写作助手。"},
        {"role": "user", "content": f"请写一份《{title}》，要求：{requirement}。"},
    ]
    return cli.chat(messages, max_tokens=1000)

def generate_document_by_prompt(prompt: str, document_type="article", tone="formal", language="zh") -> str:
    cli = get_client()
    msg = f"请用{ '中文' if language.startswith('zh') else '目标语言' }撰写一份{map_doc_type(document_type)}，语气偏向{map_tone(tone)}。需求如下：\n\n{prompt}"
    messages = [{"role": "system", "content": "你是一名中文写作助手。字数要在 1000 字以上"},
                {"role": "user", "content": msg}]
    return cli.chat(messages, max_tokens=1200)

# --- 新增优化类型映射 ---
OPTIMIZATION_MAP = {
    "grammar": "纠正语法错误和标点使用",
    "style": "优化文风，使表达更自然流畅",
    "clarity": "提升表达清晰度，避免歧义",
    "logic": "梳理逻辑，使结构更严谨有条理",
    "format": "规范文本格式，使排版更标准",
    "tone": "调整语气，使语气更正式或更符合语境",
    "all": "全面优化，包括语法、文风、逻辑、格式等各方面"
}


def optimize_document(content: str, optimization_type: str = "all", custom_instruction: str = None) -> str:
    """
    使用大模型对文本进行优化。
    如果有 custom_instruction，则优先使用自定义指令。
    否则根据 optimization_type 自动生成优化目标说明。
    """
    cli = get_client()
    
    # 构造提示语
    system_prompt = (
        "你是一名专业的中文文字编辑助手，擅长文字润色、语法修正、逻辑优化和格式规范。"
        "重要：你的输出应该只包含优化后的文本内容，不要添加任何说明、解释、分析或前缀后缀。"
        "直接输出优化后的完整文本即可。"
    )
    
    # 根据类型生成优化目标描述
    type_desc = OPTIMIZATION_MAP.get(optimization_type, "全面优化文本")
    
    # 构建优化要求
    if custom_instruction:
        # 结合优化类型和自定义指令，强调按自定义要求大胆改写
        optimization_requirement = (
            f"优化目标：{type_desc}\n\n"
            f"用户自定义要求（请重点关注并充分执行）：{custom_instruction}\n\n"
            f"注意：请根据用户的自定义要求进行充分的改写和优化，不要只做表面的微调。"
            f"如果用户要求语气更正式，就要大幅改写使之正式；"
            f"如果用户要求更生动，就要增加描述性语言和修辞手法；"
            f"如果用户要求更简洁，就要大胆删减冗余内容。"
            f"总之，要按照用户的具体指令进行实质性的改写，不要过于保守。"
        )
    else:
        # 只使用优化类型
        optimization_requirement = f"优化目标：{type_desc}"
    
    user_prompt = (
        f"{optimization_requirement}\n\n"
        f"原文：\n{content}\n\n"
        f"要求：直接输出优化后的文本，不要添加'以下是优化后的版本'、'优化结果如下'等说明文字。"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    
    result = cli.chat(messages, max_tokens=1000)
    
    # 后处理：移除常见的说明性前缀
    prefixes_to_remove = [
        "以下是优化后的版本：",
        "以下是优化后的文本：",
        "优化后的文本如下：",
        "优化结果如下：",
        "优化后：",
        "改写后：",
        "润色后：",
        "修改后的文本：",
        "修改后：",
    ]
    
    result_stripped = result.strip()
    for prefix in prefixes_to_remove:
        if result_stripped.startswith(prefix):
            result_stripped = result_stripped[len(prefix):].strip()
            break
    
    return result_stripped