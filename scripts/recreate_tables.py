"""重建数据库表（仅用于开发环境）"""
from app.models.database import Base, engine, SessionLocal
from sqlalchemy import text
from app.utils.logger import logger
import sys


def drop_all_tables_cascade():
    """使用 CASCADE 删除所有表"""
    db = SessionLocal()
    try:
        logger.info("获取所有表名...")
        result = db.execute(text("""
            SELECT tablename 
            FROM pg_tables 
            WHERE schemaname = 'public'
        """))
        tables = [row[0] for row in result.fetchall()]
        
        logger.info(f"找到 {len(tables)} 个表: {tables}")
        
        if not tables:
            logger.info("没有表需要删除")
            return
        
        logger.info("开始删除所有表（使用 CASCADE）...")
        for table in tables:
            try:
                db.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
                logger.info(f"  ✓ 删除表: {table}")
            except Exception as e:
                logger.warning(f"  ✗ 删除表 {table} 失败: {e}")
        
        db.commit()
        logger.info("✅ 所有表删除完成")
        
    except Exception as e:
        db.rollback()
        logger.error(f"❌ 删除表失败: {e}")
        raise
    finally:
        db.close()


def recreate_tables():
    """删除并重建所有表"""
    try:
        # 使用 CASCADE 删除所有表
        drop_all_tables_cascade()
        
        # 重新创建表
        logger.info("开始创建新表...")
        Base.metadata.create_all(bind=engine)
        logger.info("✅ 表创建完成")
        
        # 验证
        db = SessionLocal()
        try:
            result = db.execute(text("""
                SELECT tablename 
                FROM pg_tables 
                WHERE schemaname = 'public'
                ORDER BY tablename
            """))
            tables = [row[0] for row in result.fetchall()]
            logger.info(f"当前数据库表: {tables}")
        finally:
            db.close()
        
        logger.info("🎉 数据库重建成功！")
        
    except Exception as e:
        logger.error(f"❌ 数据库重建失败: {e}")
        raise


if __name__ == "__main__":
    confirm = input("⚠️  警告：此操作将删除所有数据！是否继续？(yes/no): ")
    if confirm.lower() != "yes":
        print("操作已取消")
        sys.exit(0)
    
    recreate_tables()