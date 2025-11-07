# app/services/conversation_service.py
from sqlalchemy.orm import Session
from typing import List, Dict, Optional
import uuid
import time
from app.models.database import Conversation
from app.models.schemas import ConversationCreate, ConversationFeedback
from app.services.vector_service import vector_service
from app.services.embedding_service import embedding_service
from app.utils.logger import logger


class ConversationService:
    """历史会话管理服务"""
    
    def __init__(self):
        """
        初始化对话服务
        注意：不在这里创建集合，改为延迟初始化
        """
        self.collection_name = "conversations"
        self._collection_initialized = False
        logger.info("对话服务初始化完成（延迟加载模式）")
    
    def _ensure_collection(self):
        """
        确保集合已创建（延迟初始化）
        只在首次使用时创建集合
        """
        if self._collection_initialized:
            return
        
        try:
            logger.info(f"首次使用对话服务，检查并创建集合: {self.collection_name}")
            vector_service.create_collection_if_not_exists(
                self.collection_name, 
                is_private=True
            )
            self._collection_initialized = True
            logger.info(f"✓ 集合 {self.collection_name} 已就绪")
        except Exception as e:
            logger.error(f"✗ 初始化对话集合失败: {e}")
            raise RuntimeError(f"无法初始化对话服务: {e}")
    
    async def create_conversation(
        self,
        db: Session,
        conv_data: ConversationCreate
    ) -> Conversation:
        """创建会话记录"""
        # 确保集合已初始化
        self._ensure_collection()
        
        conv_id = f"conv_{uuid.uuid4().hex[:16]}"
        
        db_conv = Conversation(
            conv_id=conv_id,
            user_id=conv_data.user_id,
            query=conv_data.query,
            answer=conv_data.answer,
            weight=conv_data.weight,
            liked=conv_data.liked
        )
        db.add(db_conv)
        db.commit()
        db.refresh(db_conv)
        
        try:
            await self._ingest_conversation(db_conv)
        except Exception as e:
            logger.error(f"会话向量化失败: {e}")
            # 注意：这里可以选择回滚数据库事务或继续
            # 如果向量化失败但数据库记录保存成功，后续可以重试向量化
        
        return db_conv
    
    async def _ingest_conversation(self, conversation: Conversation):
        """
        会话向量化
        将对话内容转换为向量并存储到 Milvus
        """
        # 确保集合已初始化
        self._ensure_collection()
        
        # 构造要向量化的文本
        text = f"问题：{conversation.query}\n回答：{conversation.answer}"
        
        # 生成向量
        embeddings = await embedding_service.embed_texts([text])
        embedding = embeddings[0]
        
        # 准备向量数据
        timestamp = int(time.time())
        vector_data = [{
            "id": conversation.conv_id,
            "owner_id": conversation.user_id,
            "doc_id": "",
            "title": conversation.query[:50],  # 标题取前50字符
            "doc_type": "conversation",
            "filename": "",
            "tags": "",
            "weight": conversation.weight,
            "valid": conversation.valid,
            "created_at": timestamp,
            "chunk_index": 0,
            "chunk_content": text,
            "embedding": embedding
        }]
        
        # 为用户创建分区（如果不存在）
        partition_name = f"user_{conversation.user_id}"
        vector_service.create_partition_if_not_exists(
            self.collection_name, 
            partition_name
        )
        
        # 插入向量数据
        vector_service.insert_documents(
            self.collection_name, 
            vector_data, 
            partition_name
        )
        
        logger.info(f"✓ 会话 {conversation.conv_id} 向量化完成")
    
    async def search_conversations(
        self,
        user_id: str,
        query: str,
        query_vector: List[float],
        top_k: int = 3
    ) -> List[Dict]:
        """
        检索历史会话
        
        Args:
            user_id: 用户ID
            query: 查询文本（用于日志）
            query_vector: 查询向量
            top_k: 返回结果数量
        
        Returns:
            历史会话列表
        """
        # 确保集合已初始化
        self._ensure_collection()
        
        try:
            partition_name = f"user_{user_id}"
            
            # 检查分区是否存在
            if not vector_service.has_partition(self.collection_name, partition_name):
                logger.info(f"用户 {user_id} 没有历史会话分区")
                return []
            
            # 执行向量检索
            results = vector_service.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                top_k=top_k,
                partition_names=[partition_name],
                expr="valid == true",  # 只检索有效的会话
                output_fields=["id", "chunk_content", "weight", "created_at"]
            )
            
            # 解析结果
            conversations = []
            for result in results:
                content = result.get("chunk_content", "")
                
                # 分割问题和回答
                parts = content.split("\n回答：")
                query_text = parts[0].replace("问题：", "").strip()
                answer_text = parts[1].strip() if len(parts) > 1 else ""
                
                conversations.append({
                    "id": result["id"],
                    "query": query_text,
                    "answer": answer_text,
                    "score": result.get("score", 0.0),
                    "weight": result.get("weight", 1.0),
                    "created_at": result.get("created_at", 0)
                })
            
            logger.info(f"为用户 {user_id} 检索到 {len(conversations)} 条历史会话")
            return conversations
            
        except Exception as e:
            logger.error(f"✗ 检索历史会话失败: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def get_conversation(self, db: Session, conv_id: str) -> Optional[Conversation]:
        """
        获取单个会话
        
        Args:
            db: 数据库会话
            conv_id: 会话ID
        
        Returns:
            会话对象或 None
        """
        return db.query(Conversation)\
            .filter(Conversation.conv_id == conv_id)\
            .first()
    
    def list_conversations(
        self,
        db: Session,
        user_id: str,
        limit: int = 20,
        offset: int = 0
    ) -> List[Conversation]:
        """
        列出用户的历史会话
        
        Args:
            db: 数据库会话
            user_id: 用户ID
            limit: 返回数量
            offset: 偏移量
        
        Returns:
            会话列表
        """
        return db.query(Conversation)\
            .filter(
                Conversation.user_id == user_id, 
                Conversation.valid == True
            )\
            .order_by(Conversation.created_at.desc())\
            .offset(offset)\
            .limit(limit)\
            .all()
    
    def update_conversation(
        self,
        db: Session,
        conv_id: str,
        feedback: ConversationFeedback
    ) -> Optional[Conversation]:
        """
        更新会话反馈（点赞/权重调整）
        
        Args:
            db: 数据库会话
            conv_id: 会话ID
            feedback: 反馈信息
        
        Returns:
            更新后的会话对象或 None
        """
        conv = self.get_conversation(db, conv_id)
        if not conv:
            logger.warning(f"会话 {conv_id} 不存在")
            return None
        
        # 更新点赞状态
        if feedback.liked is not None:
            conv.liked = feedback.liked
            logger.info(f"会话 {conv_id} 点赞状态更新为: {feedback.liked}")
        
        # 更新权重
        if feedback.weight_delta is not None:
            new_weight = conv.weight + feedback.weight_delta
            # 限制权重范围 [0.1, 1.0]
            conv.weight = max(0.1, min(1.0, new_weight))
            logger.info(f"会话 {conv_id} 权重调整: {conv.weight - feedback.weight_delta:.2f} -> {conv.weight:.2f}")
        
        db.commit()
        db.refresh(conv)
        
        return conv
    
    def delete_conversation(self, db: Session, conv_id: str) -> bool:
        """
        删除会话（软删除）
        
        Args:
            db: 数据库会话
            conv_id: 会话ID
        
        Returns:
            是否删除成功
        """
        # 确保集合已初始化
        self._ensure_collection()
        
        conv = self.get_conversation(db, conv_id)
        if not conv:
            logger.warning(f"会话 {conv_id} 不存在")
            return False
        
        # 从向量库删除
        partition_name = f"user_{conv.user_id}"
        try:
            # 获取集合对象
            collection = vector_service.get_collection(self.collection_name)
            
            # 删除指定ID的向量
            collection.delete(
                expr=f'id == "{conv_id}"',
                partition_name=partition_name
            )
            collection.flush()
            logger.info(f"✓ 从向量库删除会话 {conv_id}")
            
        except Exception as e:
            logger.error(f"✗ 从向量库删除会话失败: {e}")
            # 向量删除失败不影响数据库软删除
        
        # 数据库软删除（标记为无效）
        conv.valid = False
        db.commit()
        
        logger.info(f"✓ 会话 {conv_id} 已标记为删除")
        return True
    
    def get_statistics(self, db: Session, user_id: str) -> Dict:
        """
        获取用户会话统计
        
        Args:
            db: 数据库会话
            user_id: 用户ID
        
        Returns:
            统计信息字典
        """
        total = db.query(Conversation)\
            .filter(Conversation.user_id == user_id, Conversation.valid == True)\
            .count()
        
        liked = db.query(Conversation)\
            .filter(
                Conversation.user_id == user_id,
                Conversation.valid == True,
                Conversation.liked == True
            )\
            .count()
        
        return {
            "total_conversations": total,
            "liked_conversations": liked,
            "like_rate": round(liked / total * 100, 2) if total > 0 else 0
        }


# 创建全局服务实例（不立即初始化集合）
conversation_service = ConversationService()