import httpx
from typing import List, Dict, Optional, AsyncGenerator
from app.config import settings
from app.utils.logger import logger


class LLMService:
    """大语言模型服务（独立解耦）"""
    
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=60.0)
        self.api_url = settings.LLM_API_URL
        self.api_key = settings.LLM_API_KEY
        self.default_model = settings.LLM_MODEL
    
    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        stream: bool = False
    ) -> str:
        """
        通用聊天接口
        
        Args:
            messages: 对话消息列表 [{"role": "user", "content": "..."}]
            model: 模型名称（默认使用配置的模型）
            temperature: 温度参数
            max_tokens: 最大 token 数
            stream: 是否流式输出
        
        Returns:
            生成的回复文本
        """
        try:
            response = await self.client.post(
                self.api_url,
                json={
                    "model": model or self.default_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": stream
                },
                headers={"Authorization": f"Bearer {self.api_key}"}
            )
            
            response.raise_for_status()
            result = response.json()
            answer = result["choices"][0]["message"]["content"]
            
            logger.info(f"LLM 调用成功，生成 {len(answer)} 字符")
            return answer
            
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            raise RuntimeError(f"LLM 服务错误: {str(e)}")
    
    async def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000
    ) -> AsyncGenerator[str, None]:
        """
        流式聊天接口
        
        Args:
            messages: 对话消息
            model: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
        
        Yields:
            逐个返回生成的文本片段
        """
        try:
            async with self.client.stream(
                "POST",
                self.api_url,
                json={
                    "model": model or self.default_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True
                },
                headers={"Authorization": f"Bearer {self.api_key}"}
            ) as response:
                response.raise_for_status()
                
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]  # 去掉 "data: " 前缀
                        
                        if data == "[DONE]":
                            break
                        
                        try:
                            import json
                            chunk = json.loads(data)
                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                delta = chunk["choices"][0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                        except json.JSONDecodeError:
                            continue
            
            logger.info("流式输出完成")
            
        except Exception as e:
            logger.error(f"流式调用失败: {e}")
            raise RuntimeError(f"LLM 流式服务错误: {str(e)}")
    
    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str
    ) -> str:
        """
        翻译文本
        
        Args:
            text: 待翻译文本
            source_lang: 源语言（如 zh-CN, en-US）
            target_lang: 目标语言
        
        Returns:
            翻译后的文本
        """
        # 语言代码映射
        lang_map = {
            'zh-CN': '中文',
            'en-US': '英文',
            'ja-JP': '日文',
            'ko-KR': '韩文',
            'fr-FR': '法文',
            'de-DE': '德文',
            'es-ES': '西班牙文',
            'ru-RU': '俄文'
        }
        
        source = lang_map.get(source_lang, source_lang)
        target = lang_map.get(target_lang, target_lang)
        
        prompt = f"""请将以下{source}文本翻译成{target}，保持原文的语气和风格：

原文：
{text}

翻译："""
        
        messages = [
            {
                "role": "system",
                "content": "你是一位专业的翻译助手，擅长准确、流畅地翻译各种语言。"
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
        
        try:
            result = await self.chat(messages, temperature=0.3)
            logger.info(f"翻译完成：{source} -> {target}")
            return result.strip()
        except Exception as e:
            logger.error(f"翻译失败: {e}")
            raise
    
    async def batch_translate(
        self,
        texts: List[str],
        source_lang: str,
        target_lang: str,
        batch_size: int = 5
    ) -> List[str]:
        """
        批量翻译
        
        Args:
            texts: 待翻译文本列表
            source_lang: 源语言
            target_lang: 目标语言
            batch_size: 批次大小
        
        Returns:
            翻译后的文本列表
        """
        results = []
        
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            
            # 并发翻译一个批次
            import asyncio
            tasks = [
                self.translate(text, source_lang, target_lang)
                for text in batch
            ]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 处理结果（失败的保留原文）
            for text, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.error(f"翻译失败，保留原文: {text[:50]}...")
                    results.append(text)
                else:
                    results.append(result)
            
            # 延迟避免频率限制
            if i + batch_size < len(texts):
                await asyncio.sleep(1)
        
        logger.info(f"批量翻译完成：{len(texts)} 条文本")
        return results
    
    async def summarize(
        self,
        text: str,
        max_length: int = 200,
        style: str = "concise"
    ) -> str:
        """
        文本摘要
        
        Args:
            text: 待摘要文本
            max_length: 摘要最大长度
            style: 摘要风格（concise/detailed/bullet_points）
        
        Returns:
            摘要文本
        """
        style_prompts = {
            "concise": "请用一句话概括以下内容的核心要点",
            "detailed": f"请用不超过{max_length}字详细总结以下内容",
            "bullet_points": "请用要点列表形式（使用 - 或数字）总结以下内容"
        }
        
        prompt = f"""{style_prompts.get(style, style_prompts['concise'])}：

{text}

摘要："""
        
        messages = [
            {
                "role": "system",
                "content": "你是一位专业的文本摘要助手，擅长提炼关键信息。"
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
        
        try:
            result = await self.chat(messages, temperature=0.5, max_tokens=max_length * 2)
            logger.info(f"摘要生成完成：{len(text)} -> {len(result)} 字符")
            return result.strip()
        except Exception as e:
            logger.error(f"摘要生成失败: {e}")
            raise
    
    async def generate_with_context(
        self,
        query: str,
        context: str,
        system_prompt: Optional[str] = None
    ) -> str:
        """
        基于上下文生成回答（RAG 专用）
        
        Args:
            query: 用户问题
            context: 检索到的上下文
            system_prompt: 自定义系统提示词
        
        Returns:
            生成的答案
        """
        default_system_prompt = """你是一位专业的公文助手，擅长分析和解答各类公文相关问题。

任务要求：
1. 基于提供的参考资料回答用户问题
2. 回答要准确、专业、规范，符合公文写作要求
3. 如果参考资料不足以回答问题，请如实说明
4. 引用参考资料时，请注明来源（如：根据参考资料1...）
5. 对于公文格式、规范等问题，给出明确的指导

回答风格：
- 语言正式、严谨
- 条理清晰、逻辑严密
- 重点突出、简洁明了"""

        user_prompt = f"""参考资料：
{context}

用户问题：{query}

请基于以上参考资料，回答用户问题。"""

        messages = [
            {"role": "system", "content": system_prompt or default_system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        return await self.chat(messages, temperature=0.7, max_tokens=2000)
    
    async def close(self):
        """关闭客户端连接"""
        await self.client.aclose()
        logger.info("LLM 服务客户端已关闭")


# 创建全局实例
llm_service = LLMService()