"""在已存在的库中建表。

运行方式（在项目根目录）::
    python scripts/init_db.py

不要使用 ``python -m scripts.init_db.py``（-m 后面不能带 .py 后缀）。

说明：不会 CREATE DATABASE，仅执行 SQLAlchemy 建表；导入 ``app.models.database`` 时也会建表一次。
"""
import sys

sys.path.append(".")

from app.models.database import create_all_tables
from app.utils.logger import logger


def init_database():
    try:
        create_all_tables()
        logger.info("数据库表创建成功")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        raise


if __name__ == "__main__":
    init_database()