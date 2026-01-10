"""
日志系统
- 按日期自动轮转
- 自动清理过期日志
- 分级日志（所有/错误/业务）
- 彩色控制台输出
"""

import logging
import sys
import os
from logging.handlers import TimedRotatingFileHandler, RotatingFileHandler
from pathlib import Path
from datetime import datetime, timedelta
import threading
import time


class ColoredFormatter(logging.Formatter):
    """彩色日志格式化器（控制台）"""
    
    COLORS = {
        'DEBUG': '\033[36m',    # 青色
        'INFO': '\033[32m',     # 绿色
        'WARNING': '\033[33m',  # 黄色
        'ERROR': '\033[31m',    # 红色
        'CRITICAL': '\033[35m', # 紫色
    }
    RESET = '\033[0m'
    
    def format(self, record):
        levelname = record.levelname
        if levelname in self.COLORS:
            record.levelname = f"{self.COLORS[levelname]}{levelname}{self.RESET}"
        return super().format(record)


class BusinessLogFilter(logging.Filter):
    """业务日志过滤器"""
    
    KEYWORDS = [
        '用户', '创建', '删除', '更新', '上传', '下载',
        '登录', '注销', '备份', '恢复', '知识库', 
        '文档', '会议', '翻译', 'AI检测', '成功', '失败'
    ]
    
    def filter(self, record):
        message = record.getMessage()
        return any(kw in message for kw in self.KEYWORDS)


class LogCleaner:
    """日志清理器"""
    
    def __init__(self, log_dir: Path, keep_days: int = 30):
        self.log_dir = log_dir
        self.keep_days = keep_days
        self._start_cleanup_thread()
    
    def _start_cleanup_thread(self):
        """启动后台清理线程"""
        def cleanup_loop():
            while True:
                try:
                    # 每天凌晨3点执行清理
                    now = datetime.now()
                    next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
                    if next_run <= now:
                        next_run = next_run + timedelta(days=1)
                    
                    sleep_seconds = (next_run - now).total_seconds()
                    time.sleep(sleep_seconds)
                    
                    self.cleanup()
                except Exception as e:
                    print(f"[日志清理失败] {e}")
                    time.sleep(3600)  # 出错后1小时重试
        
        thread = threading.Thread(target=cleanup_loop, daemon=True, name="LogCleaner")
        thread.start()
    
    def cleanup(self):
        """清理过期日志"""
        cutoff_date = datetime.now() - timedelta(days=self.keep_days)
        deleted = 0
        
        try:
            for log_file in self.log_dir.glob("*.log.*"):
                try:
                    file_mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
                    if file_mtime < cutoff_date:
                        log_file.unlink()
                        deleted += 1
                        print(f"[日志清理] 删除过期日志: {log_file.name}")
                except Exception as e:
                    print(f"[日志清理] 删除失败 {log_file.name}: {e}")
            
            if deleted > 0:
                print(f"[日志清理] 完成，共删除 {deleted} 个文件")
        except Exception as e:
            print(f"[日志清理] 扫描失败: {e}")


class LogManager:
    """日志管理器"""
    
    def __init__(self):
        self.log_dir = Path("logs")
        self.log_dir.mkdir(exist_ok=True)
        
        # 日志保留天数
        self.keep_days = int(os.getenv("LOG_KEEP_DAYS", "30"))
        
        # 启动清理器
        self.cleaner = LogCleaner(self.log_dir, self.keep_days)
        
        # 根 logger
        self._setup_root_logger()
    
    def _setup_root_logger(self):
        """配置根 logger"""
        root_logger = logging.getLogger()
        
        # 避免重复配置
        if root_logger.handlers:
            return
        
        # 从环境变量读取日志级别
        debug_mode = os.getenv("DEBUG", "false").lower() == "true"
        level = logging.DEBUG if debug_mode else logging.INFO
        root_logger.setLevel(level)
        
        # 详细格式（文件）
        detailed_fmt = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - '
            '[%(filename)s:%(lineno)d] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # 简洁格式（控制台）
        console_fmt = ColoredFormatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # ========== 1. 控制台 Handler ==========
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(console_fmt)
        root_logger.addHandler(console_handler)
        
        # ========== 2. 全量日志文件（按日期轮转）==========
        all_handler = TimedRotatingFileHandler(
            filename=self.log_dir / "app.log",
            when='midnight',
            interval=1,
            backupCount=self.keep_days,
            encoding='utf-8'
        )
        all_handler.setLevel(logging.DEBUG)
        all_handler.setFormatter(detailed_fmt)
        all_handler.suffix = "%Y-%m-%d"
        root_logger.addHandler(all_handler)
        
        # ========== 3. 错误日志（按大小轮转）==========
        error_handler = RotatingFileHandler(
            filename=self.log_dir / "error.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=10,
            encoding='utf-8'
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(detailed_fmt)
        root_logger.addHandler(error_handler)
        
        # ========== 4. 业务日志（按日期轮转）==========
        business_handler = TimedRotatingFileHandler(
            filename=self.log_dir / "business.log",
            when='midnight',
            interval=1,
            backupCount=self.keep_days,
            encoding='utf-8'
        )
        business_handler.setLevel(logging.INFO)
        business_handler.setFormatter(detailed_fmt)
        business_handler.suffix = "%Y-%m-%d"
        business_handler.addFilter(BusinessLogFilter())
        root_logger.addHandler(business_handler)
        
        # 记录启动信息
        root_logger.info("=" * 60)
        root_logger.info("公文大模型RAG系统 - 日志系统初始化完成")
        root_logger.info(f"日志级别: {'DEBUG' if debug_mode else 'INFO'}")
        root_logger.info(f"日志保留天数: {self.keep_days}")
        root_logger.info(f"日志目录: {self.log_dir.absolute()}")
        root_logger.info("=" * 60)


# ========== 全局实例 ==========
_log_manager = LogManager()
logger = logging.getLogger("app")


# ========== 便捷函数 ==========
def get_logger(name: str) -> logging.Logger:
    """
    获取指定名称的 logger
    
    Usage:
        from app.utils.logger import get_logger
        logger = get_logger(__name__)
    """
    return logging.getLogger(name)