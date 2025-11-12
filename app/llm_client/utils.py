def map_doc_type(tp: str) -> str:
    return {
        "article": "正式公文/通知",
        "report": "工作报告",
        "summary": "工作总结",
        "email": "正式邮件",
    }.get((tp or "").lower(), "正式公文/通知")

def map_tone(t: str) -> str:
    return {
        "formal": "正式",
        "professional": "专业",
        "casual": "自然",
        "friendly": "亲切",
        "concise": "简洁",
    }.get((t or "").lower(), "正式")
