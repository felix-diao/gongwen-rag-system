"""初始化数据库脚本"""
import sys
sys.path.append('.')

from app.models.database import Base, engine
from app.utils.logger import logger

def init_database():
    """创建所有表"""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("数据库表创建成功")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        raise

if __name__ == "__main__":
    init_database()