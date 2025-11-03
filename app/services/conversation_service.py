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
        vector_service.create_collection_if_not_exists("conversations", is_private=True)
    
    async def create_conversation(
        self,
        db: Session,
        conv_data: ConversationCreate
    ) -> Conversation:
        """创建会话记录"""
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
        
        return db_conv
    
    async def _ingest_conversation(self, conversation: Conversation):
        """会话向量化"""
        text = f"问题：{conversation.query}\n回答：{conversation.answer}"
        
        embeddings = await embedding_service.embed_texts([text])
        embedding = embeddings[0]
        
        timestamp = int(time.time())
        vector_data = [{
            "id": conversation.conv_id,
            "owner_id": conversation.user_id,
            "doc_id": "",
            "title": conversation.query[:50],
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
        
        partition_name = f"user_{conversation.user_id}"
        vector_service.create_partition_if_not_exists("conversations", partition_name)
        
        vector_service.insert_documents("conversations", vector_data, partition_name)
        
        logger.info(f"会话 {conversation.conv_id} 向量化完成")
    
    async def search_conversations(
        self,
        user_id: str,
        query: str,
        query_vector: List[float],
        top_k: int = 3
    ) -> List[Dict]:
        """检索历史会话"""
        try:
            partition_name = f"user_{user_id}"
            
            results = vector_service.search(
                collection_name="conversations",
                query_vector=query_vector,
                top_k=top_k,
                partition_names=[partition_name],
                expr="valid == true",
                output_fields=["id", "chunk_content", "weight", "created_at"]
            )
            
            conversations = []
            for result in results:
                content = result.get("chunk_content", "")
                parts = content.split("\n回答：")
                query_text = parts[0].replace("问题：", "").strip()
                answer_text = parts[1].strip() if len(parts) > 1 else ""
                
                conversations.append({
                    "id": result["id"],
                    "query": query_text,
                    "answer": answer_text,
                    "score": result["score"],
                    "weight": result.get("weight", 1.0),
                    "created_at": result.get("created_at", 0)
                })
            
            return conversations
            
        except Exception as e:
            logger.error(f"检索历史会话失败: {e}")
            return []
    
    def get_conversation(self, db: Session, conv_id: str) -> Optional[Conversation]:
        """获取会话"""
        return db.query(Conversation).filter(Conversation.conv_id == conv_id).first()
    
    def list_conversations(
        self,
        db: Session,
        user_id: str,
        limit: int = 20,
        offset: int = 0
    ) -> List[Conversation]:
        """列出用户的历史会话"""
        return db.query(Conversation)\
            .filter(Conversation.user_id == user_id, Conversation.valid == True)\
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
        """更新会话反馈"""
        conv = self.get_conversation(db, conv_id)
        if not conv:
            return None
        
        if feedback.liked is not None:
            conv.liked = feedback.liked
        
        if feedback.weight_delta is not None:
            new_weight = conv.weight + feedback.weight_delta
            conv.weight = max(0.1, min(1.0, new_weight))
        
        db.commit()
        db.refresh(conv)
        
        return conv
    
    def delete_conversation(self, db: Session, conv_id: str) -> bool:
        """删除会话"""
        conv = self.get_conversation(db, conv_id)
        if not conv:
            return False
        
        partition_name = f"user_{conv.user_id}"
        try:
            collection = vector_service.Collection("conversations")
            collection.delete(expr=f'id == "{conv_id}"', partition_name=partition_name)
            collection.flush()
        except Exception as e:
            logger.error(f"从向量库删除会话失败: {e}")
        
        conv.valid = False
        db.commit()
        
        logger.info(f"会话 {conv_id} 已删除")
        return True

conversation_service = ConversationService()