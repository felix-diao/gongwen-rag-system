#!/bin/bash

# =====================================================
# 数据恢复脚本 - 从备份恢复所有服务
# =====================================================

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 日志函数
log() {
    echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"
}

error() {
    echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1"
}

warning() {
    echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')] WARNING:${NC} $1"
}

info() {
    echo -e "${BLUE}[$(date '+%Y-%m-%d %H:%M:%S')] INFO:${NC} $1"
}

# 检查参数
if [ -z "$1" ]; then
    error "使用方法: $0 <备份目录>"
    echo ""
    echo "示例:"
    echo "  $0 backups/full/20241213_020001"
    echo ""
    echo "可用的备份:"
    ls -lht backups/full/ 2>/dev/null | head -10 | tail -n +2 || echo "  无可用备份"
    exit 1
fi

BACKUP_DIR=$1

# 验证备份目录
if [ ! -d "$BACKUP_DIR" ]; then
    error "备份目录不存在: $BACKUP_DIR"
    exit 1
fi

# 显示备份信息
if [ -f "$BACKUP_DIR/backup_info.txt" ]; then
    info "备份信息:"
    cat "$BACKUP_DIR/backup_info.txt"
    echo ""
fi

# 确认操作
warning "⚠️  警告: 此操作将覆盖当前所有数据！"
warning "⚠️  当前数据将被 $BACKUP_DIR 中的备份替换"
echo ""
read -p "确认要继续吗? (yes/no): " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    info "操作已取消"
    exit 0
fi

log "=================================="
log "开始恢复数据"
log "备份源: $BACKUP_DIR"
log "=================================="

# 1. 停止所有服务
log "🛑 停止所有服务..."
docker-compose down
log "✅ 服务已停止"

# 2. 备份当前数据（以防万一）
SAFETY_BACKUP="backups/safety_backup_$(date +%Y%m%d_%H%M%S)"
log "💾 创建安全备份到: $SAFETY_BACKUP"
mkdir -p "$SAFETY_BACKUP"
cp -r volumes/ "$SAFETY_BACKUP/" 2>/dev/null || warning "⚠️  当前数据备份失败（可能目录为空）"

# 3. 恢复 etcd
if [ -f "$BACKUP_DIR/etcd_backup.tar.gz" ]; then
    log "📦 恢复 etcd..."
    rm -rf ./volumes/etcd
    tar -xzf "$BACKUP_DIR/etcd_backup.tar.gz" -C ./volumes/
    log "✅ etcd 恢复成功"
else
    warning "⚠️  未找到 etcd 备份文件"
fi

# 4. 恢复 MinIO
if [ -f "$BACKUP_DIR/minio_backup.tar.gz" ]; then
    log "📦 恢复 MinIO..."
    rm -rf ./volumes/minio
    tar -xzf "$BACKUP_DIR/minio_backup.tar.gz" -C ./volumes/
    log "✅ MinIO 恢复成功"
else
    warning "⚠️  未找到 MinIO 备份文件"
fi

# 5. 恢复 Milvus
if [ -f "$BACKUP_DIR/milvus_backup.tar.gz" ]; then
    log "📦 恢复 Milvus..."
    rm -rf ./volumes/milvus
    tar -xzf "$BACKUP_DIR/milvus_backup.tar.gz" -C ./volumes/
    log "✅ Milvus 恢复成功"
else
    warning "⚠️  未找到 Milvus 备份文件"
fi

# 6. 启动基础服务（为 PostgreSQL 恢复做准备）
log "🚀 启动基础服务..."
docker-compose up -d postgres-db
log "⏳ 等待 PostgreSQL 启动..."
sleep 10

# 等待 PostgreSQL 就绪
MAX_WAIT=60
WAITED=0
while ! docker exec postgres-db pg_isready -U gongwen_user -d gongwen_rag >/dev/null 2>&1; do
    if [ $WAITED -ge $MAX_WAIT ]; then
        error "PostgreSQL 启动超时"
        exit 1
    fi
    sleep 2
    WAITED=$((WAITED + 2))
    echo -n "."
done
echo ""
log "✅ PostgreSQL 已就绪"

# 7. 恢复 PostgreSQL
if [ -f "$BACKUP_DIR/postgres_backup.sql.gz" ]; then
    log "📦 恢复 PostgreSQL 数据库..."
    
    # 删除现有数据库并重建
    docker exec postgres-db psql -U gongwen_user -d postgres -c "DROP DATABASE IF EXISTS gongwen_rag;" 2>/dev/null || true
    docker exec postgres-db psql -U gongwen_user -d postgres -c "CREATE DATABASE gongwen_rag;" 2>/dev/null
    
    # 恢复数据
    gunzip < "$BACKUP_DIR/postgres_backup.sql.gz" | docker exec -i postgres-db psql -U gongwen_user -d gongwen_rag
    log "✅ PostgreSQL 恢复成功"
else
    warning "⚠️  未找到 PostgreSQL 备份文件"
fi

# 8. 启动所有服务
log "🚀 启动所有服务..."
docker-compose up -d
log "⏳ 等待所有服务启动..."
sleep 15

# 9. 检查服务状态
log "📊 检查服务状态..."
docker-compose ps

# 10. 验证服务健康
log "🏥 验证服务健康状态..."

# 检查 PostgreSQL
if docker exec postgres-db pg_isready -U gongwen_user -d gongwen_rag >/dev/null 2>&1; then
    log "✅ PostgreSQL 运行正常"
else
    error "❌ PostgreSQL 状态异常"
fi

# 检查 Milvus
if curl -sf http://localhost:9091/healthz >/dev/null 2>&1; then
    log "✅ Milvus 运行正常"
else
    warning "⚠️  Milvus 可能还在启动中"
fi

# 检查 MinIO
if curl -sf http://localhost:9000/minio/health/live >/dev/null 2>&1; then
    log "✅ MinIO 运行正常"
else
    warning "⚠️  MinIO 可能还在启动中"
fi

log "=================================="
log "✅ 数据恢复完成！"
log "安全备份位置: $SAFETY_BACKUP"
log "=================================="
log ""
info "建议: 请验证应用功能是否正常"
info "如果恢复有问题，可以从安全备份恢复: cp -r $SAFETY_BACKUP/volumes/* volumes/"