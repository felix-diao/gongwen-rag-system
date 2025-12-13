#!/bin/bash

# =====================================================
# 完整备份脚本 - 备份所有服务数据
# =====================================================

set -e  # 遇到错误立即退出

# 配置
BACKUP_ROOT="./backups"
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="$BACKUP_ROOT/full/$DATE"
KEEP_DAYS=30
LOG_FILE="$BACKUP_ROOT/backup.log"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 日志函数
log() {
    echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1" | tee -a "$LOG_FILE"
}

error() {
    echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1" | tee -a "$LOG_FILE"
}

warning() {
    echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')] WARNING:${NC} $1" | tee -a "$LOG_FILE"
}

# 创建备份目录
mkdir -p "$BACKUP_DIR"

log "=================================="
log "开始完整备份"
log "备份目录: $BACKUP_DIR"
log "=================================="

# 1. 备份 PostgreSQL (通过 pg_dump，热备份)
log "📦 备份 PostgreSQL 数据库..."
if docker exec postgres-db pg_dump -U gongwen_user gongwen_rag | gzip > "$BACKUP_DIR/postgres_backup.sql.gz"; then
    log "✅ PostgreSQL 备份成功 ($(du -sh "$BACKUP_DIR/postgres_backup.sql.gz" | cut -f1))"
else
    error "❌ PostgreSQL 备份失败"
    exit 1
fi

# 2. 备份 etcd (只备份快照文件，避免 WAL 文件变化问题)
log "📦 备份 etcd 配置..."
ETCD_SNAPSHOT_NAME="snapshot-$DATE.db"
if docker exec etcd-service etcdctl snapshot save /etcd/$ETCD_SNAPSHOT_NAME 2>/dev/null; then
    # 等待快照文件写入完成
    sleep 1
    # 直接复制快照文件
    if cp "./volumes/etcd/$ETCD_SNAPSHOT_NAME" "$BACKUP_DIR/etcd_snapshot.db" 2>/dev/null; then
        log "✅ etcd 备份成功 ($(du -sh "$BACKUP_DIR/etcd_snapshot.db" | cut -f1))"
        # 清理容器内的临时快照（可选，避免占用空间）
        find "./volumes/etcd" -name "snapshot-*.db" -mtime +7 -delete 2>/dev/null || true
    else
        warning "⚠️  etcd 快照创建成功，但复制失败"
    fi
else
    warning "⚠️  etcd 备份失败（可能服务未运行）"
fi

# 3. 备份 MinIO (对象存储)
log "📦 备份 MinIO 对象存储..."
if tar -czf "$BACKUP_DIR/minio_backup.tar.gz" -C ./volumes minio 2>/dev/null; then
    log "✅ MinIO 备份成功 ($(du -sh "$BACKUP_DIR/minio_backup.tar.gz" | cut -f1))"
else
    warning "⚠️  MinIO 备份失败"
fi

# 4. 备份 Milvus (向量数据库)
log "📦 备份 Milvus 向量数据..."
if tar -czf "$BACKUP_DIR/milvus_backup.tar.gz" -C ./volumes milvus 2>/dev/null; then
    log "✅ Milvus 备份成功 ($(du -sh "$BACKUP_DIR/milvus_backup.tar.gz" | cut -f1))"
else
    warning "⚠️  Milvus 备份失败"
fi

# 5. 备份 Docker Compose 配置
log "📦 备份配置文件..."
cp docker-compose.yml "$BACKUP_DIR/docker-compose.yml.backup"
if [ -f .env ]; then
    cp .env "$BACKUP_DIR/env.backup"
fi
log "✅ 配置文件备份成功"

# 6. 生成备份信息文件
cat > "$BACKUP_DIR/backup_info.txt" << EOF
=====================================
备份信息
=====================================
备份时间: $DATE
备份类型: 完整备份 (Full Backup)

备份内容:
- PostgreSQL 数据库 (gongwen_rag)
- etcd 快照 (时间点一致性备份)
- MinIO 对象存储数据
- Milvus 向量数据库
- Docker Compose 配置文件

系统信息:
- 主机名: $(hostname)
- 操作系统: $(uname -s)
- Docker 版本: $(docker --version)
- Docker Compose 版本: $(docker-compose --version 2>/dev/null || docker compose version 2>/dev/null || echo "未知")

容器状态:
$(docker-compose ps 2>/dev/null || docker compose ps 2>/dev/null || echo "无法获取容器状态")

备份文件列表:
$(ls -lh "$BACKUP_DIR" | tail -n +2)

恢复说明:
- PostgreSQL: gunzip < postgres_backup.sql.gz | docker exec -i postgres-db psql -U gongwen_user gongwen_rag
- etcd: 停止容器后，使用 etcdctl snapshot restore 命令恢复
- MinIO/Milvus: 停止容器后解压到 ./volumes 目录
=====================================
EOF

# 7. 计算总备份大小
TOTAL_SIZE=$(du -sh "$BACKUP_DIR" | cut -f1)
log "📊 本次备份总大小: $TOTAL_SIZE"

# 8. 清理旧备份
log "🧹 清理 $KEEP_DAYS 天前的备份..."
DELETED_COUNT=$(find "$BACKUP_ROOT/full" -maxdepth 1 -type d -name "202*" -mtime +$KEEP_DAYS 2>/dev/null | wc -l)
find "$BACKUP_ROOT/full" -maxdepth 1 -type d -name "202*" -mtime +$KEEP_DAYS -exec rm -rf {} \; 2>/dev/null || true
log "✅ 已删除 $DELETED_COUNT 个旧备份"

# 9. 显示当前备份列表
log "📋 当前保留的备份:"
ls -lht "$BACKUP_ROOT/full" | head -10 | tail -n +2

# 10. 汇总信息
log "=================================="
log "✅ 备份完成！"
log "备份位置: $BACKUP_DIR"
log "总大小: $TOTAL_SIZE"
log "日志文件: $LOG_FILE"
log "=================================="

# 可选：发送通知（取消注释以启用）
# curl -X POST "https://your-webhook-url.com" -d "Backup completed: $TOTAL_SIZE"