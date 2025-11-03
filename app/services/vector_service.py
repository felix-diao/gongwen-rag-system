from pymilvus import connections, Collection, CollectionSchema, FieldSchema, DataType, utility
from typing import List, Dict, Optional
import time
from app.config import settings
from app.utils.logger import logger

class VectorService:
    """向量数据库服务"""
    
    def __init__(self):
        self.dim = settings.EMBEDDING_DIM
        self.connect()
        
    def connect(self):
        """连接 Milvus"""
        try:
            connections.connect(
                alias="default",
                host=settings.MILVUS_HOST,
                port=settings.MILVUS_PORT,
                user=settings.MILVUS_USER,
                password=settings.MILVUS_PASSWORD
            )
            logger.info("成功连接到 Milvus")
        except Exception as e:
            logger.error(f"连接 Milvus 失败: {e}")
            raise
    
    def create_collection_if_not_exists(self, collection_name: str, is_private: bool = False):
        """创建集合"""
        if utility.has_collection(collection_name):
            logger.info(f"集合 {collection_name} 已存在")
            return Collection(collection_name)
        
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=64, is_primary=True),
            FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="title", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="doc_type", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="filename", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="tags", dtype=DataType.VARCHAR, max_length=1000),
            FieldSchema(name="weight", dtype=DataType.FLOAT, default_value=1.0),
            FieldSchema(name="valid", dtype=DataType.BOOL, default_value=True),
            FieldSchema(name="created_at", dtype=DataType.INT64),
            FieldSchema(name="chunk_index", dtype=DataType.INT32),
            FieldSchema(name="chunk_content", dtype=DataType.VARCHAR, max_length=4000),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=self.dim)
        ]
        
        if is_private:
            fields.insert(1, FieldSchema(name="owner_id", dtype=DataType.VARCHAR, max_length=64))
        
        schema = CollectionSchema(fields=fields, description=f"{collection_name} schema")
        collection = Collection(name=collection_name, schema=schema)
        
        index_params = {
            "metric_type": "IP",
            "index_type": "IVF_FLAT",
            "params": {"nlist": 1024}
        }
        collection.create_index(field_name="embedding", index_params=index_params)
        logger.info(f"成功创建集合 {collection_name}")
        
        return collection
    
    def create_partition_if_not_exists(self, collection_name: str, partition_name: str):
        """创建分区"""
        collection = Collection(collection_name)
        
        if not collection.has_partition(partition_name):
            collection.create_partition(partition_name)
            logger.info(f"成功创建分区 {collection_name}/{partition_name}")
    
    def insert_documents(self, collection_name: str, data: List[Dict], partition_name: Optional[str] = None):
        """插入文档向量"""
        collection = Collection(collection_name)
        collection.load()
        
        entities = self._prepare_entities(data, collection.schema)
        
        if partition_name:
            mr = collection.insert(entities, partition_name=partition_name)
        else:
            mr = collection.insert(entities)
        
        collection.flush()
        logger.info(f"成功插入 {len(data)} 条记录到 {collection_name}")
        return mr
    
    def search(
        self,
        collection_name: str,
        query_vector: List[float],
        top_k: int = 10,
        partition_names: Optional[List[str]] = None,
        expr: Optional[str] = None,
        output_fields: Optional[List[str]] = None
    ) -> List[Dict]:
        """向量检索"""
        collection = Collection(collection_name)
        collection.load()
        
        search_params = {
            "metric_type": "IP",
            "params": {"nprobe": 10}
        }
        
        if output_fields is None:
            output_fields = ["doc_id", "title", "doc_type", "chunk_index", 
                           "chunk_content", "weight", "created_at", "tags"]
        
        results = collection.search(
            data=[query_vector],
            anns_field="embedding",
            param=search_params,
            limit=top_k,
            expr=expr,
            output_fields=output_fields,
            partition_names=partition_names
        )
        
        candidates = []
        for hits in results:
            for hit in hits:
                candidate = {
                    "id": hit.id,
                    "score": float(hit.score),
                    **{k: hit.entity.get(k) for k in output_fields}
                }
                candidates.append(candidate)
        
        return candidates
    
    def delete_by_doc_id(self, collection_name: str, doc_id: str, partition_name: Optional[str] = None):
        """删除文档的所有 chunks"""
        collection = Collection(collection_name)
        expr = f'doc_id == "{doc_id}"'
        
        if partition_name:
            collection.delete(expr=expr, partition_name=partition_name)
        else:
            collection.delete(expr=expr)
        
        collection.flush()
        logger.info(f"删除文档 {doc_id} 的所有向量")
    
    def _prepare_entities(self, data: List[Dict], schema) -> List[List]:
        """准备插入的实体数据"""
        field_names = [field.name for field in schema.fields]
        entities = {name: [] for name in field_names}
        
        for item in data:
            for field_name in field_names:
                value = item.get(field_name)
                
                if field_name == "tags" and isinstance(value, list):
                    value = ",".join(value)
                
                entities[field_name].append(value)
        
        return [entities[name] for name in field_names]

vector_service = VectorService()