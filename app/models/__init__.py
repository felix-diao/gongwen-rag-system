"""模型包统一入口。"""

from . import database, schemas

# 兼容历史导入路径，后续可逐步移除
database1 = database
schemas1 = schemas
schemas2 = schemas

__all__ = ["database", "schemas", "database1", "schemas1", "schemas2"]
