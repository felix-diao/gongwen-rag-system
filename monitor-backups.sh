#!/bin/bash

# =====================================================
# 备份监控脚本 - 查看备份状态和空间占用
# =====================================================

# 颜色输出
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}=================================="
echo "📊 备份监控报告"
echo -e "==================================${NC}"
echo ""

# 1. PostgreSQL 自动备份状态
echo -e "${BLUE}━━━ PostgreSQL 自动备份 ━━━${NC}"

if [ -d "backups/postgres" ]; then
    # 总体占用
    echo -e "${GREEN}📦 总体占用:${NC}"
    du -sh backups/postgres/
    echo ""
    
    # 每日备份
    if [ -d "backups/postgres/daily" ]; then
        echo -e "${GREEN}📅 每日备份:${NC}"
        du -sh backups/postgres/daily/
        COUNT=$(ls backups/postgres/daily/*.gz 2>/dev/null | wc -l)
        echo "   文件数量: $COUNT"
        if [ $COUNT -gt 0 ]; then
            echo "   最新备份: $(ls -t backups/postgres/daily/*.gz 2>/dev/null | head -1 | xargs basename)"
            echo "   最旧备份: $(ls -t backups/postgres/daily/*.gz 2>/dev/null | tail -1 | xargs basename)"
        fi
        echo ""
    fi
    
    # 每周备份
    if [ -d "backups/postgres/weekly" ]; then
        echo -e "${GREEN}📆 每周备份:${NC}"
        du -sh backups/postgres/weekly/
        COUNT=$(ls backups/postgres/weekly/*.gz 2>/dev/null | wc -l)
        echo "   文件数量: $COUNT"
        echo ""
    fi
    
    # 每月备份
    if [ -d "backups/postgres/monthly" ]; then
        echo -e "${GREEN}📅 每月备份:${NC}"
        du -sh backups/postgres/monthly/
        COUNT=$(ls backups/postgres/monthly/*.gz 2>/dev/null | wc -l)
        echo "   文件数量: $COUNT"
        echo ""
    fi
else
    echo -e "${YELLOW}⚠️  未找到 PostgreSQL 备份目录${NC}"
    echo ""
fi

# 2. 完整备份状态
echo -e "${BLUE}━━━ 完整备份 (Full Backup) ━━━${NC}"

if [ -d "backups/full" ]; then
    du -sh backups/full/
    COUNT=$(ls -d backups/full/202* 2>/dev/null | wc -l)
    echo "备份数量: $COUNT"
    echo ""
    
    if [ $COUNT -gt 0 ]; then
        echo -e "${GREEN}最近的备份:${NC}"
        ls -lht backups/full/ | head -6 | tail -5 | awk '{print "  " $9 " - " $5}'
        echo ""
    fi
else
    echo -e "${YELLOW}⚠️  未找到完整备份目录${NC}"
    echo ""
fi

# 3. 磁盘空间
echo -e "${BLUE}━━━ 磁盘空间 ━━━${NC}"
df -h . | awk 'NR==1 {print $0} NR==2 {print $0; printf "使用率: %s\n", $5}'
echo ""

# 4. 备份容器状态
echo -e "${BLUE}━━━ 备份服务状态 ━━━${NC}"
if docker ps --filter "name=postgres-backup" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -q postgres-backup; then
    docker ps --filter "name=postgres-backup" --format "table {{.Names}}\t{{.Status}}"
    echo ""
    
    echo -e "${GREEN}最近的备份日志:${NC}"
    docker logs --tail 10 postgres-backup 2>/dev/null | grep -E "Starting backup|Backup completed|Deleted" || echo "  暂无备份日志"
else
    echo -e "${YELLOW}⚠️  postgres-backup 容器未运行${NC}"
fi
echo ""

# 5. 下次备份时间
echo -e "${BLUE}━━━ 备份计划 ━━━${NC}"
if docker exec postgres-backup cat /etc/crontabs/root 2>/dev/null; then
    SCHEDULE=$(docker exec postgres-backup printenv SCHEDULE 2>/dev/null || echo "未知")
    echo "定时配置: $SCHEDULE"
else
    echo -e "${YELLOW}⚠️  无法获取备份计划${NC}"
fi
echo ""

# 6. 建议
echo -e "${BLUE}━━━ 建议 ━━━${NC}"

# 检查最新备份时间
if [ -f "backups/postgres/daily/gongwen_rag-latest.sql.gz" ]; then
    LAST_BACKUP=$(stat -c %Y backups/postgres/daily/gongwen_rag-latest.sql.gz 2>/dev/null || stat -f %m backups/postgres/daily/gongwen_rag-latest.sql.gz 2>/dev/null)
    NOW=$(date +%s)
    HOURS_AGO=$(( ($NOW - $LAST_BACKUP) / 3600 ))
    
    if [ $HOURS_AGO -gt 48 ]; then
        echo -e "${YELLOW}⚠️  最后一次备份是 $HOURS_AGO 小时前，建议检查备份服务${NC}"
    else
        echo -e "${GREEN}✅ 最后一次备份是 $HOURS_AGO 小时前，运行正常${NC}"
    fi
else
    echo -e "${YELLOW}⚠️  未找到最新备份，建议手动触发一次备份${NC}"
fi

# 检查磁盘空间
DISK_USAGE=$(df . | awk 'NR==2 {print $5}' | sed 's/%//')
if [ $DISK_USAGE -gt 80 ]; then
    echo -e "${YELLOW}⚠️  磁盘使用率 ${DISK_USAGE}%，建议清理旧备份或增加磁盘空间${NC}"
fi

echo ""
echo -e "${CYAN}==================================${NC}"

# 7. 快捷操作提示
echo ""
echo -e "${CYAN}快捷操作:${NC}"
echo "  手动备份: docker exec postgres-backup /backup.sh"
echo "  查看日志: docker logs -f postgres-backup"
echo "  完整备份: ./backup-all.sh"
echo "  恢复数据: ./restore.sh backups/full/YYYYMMDD_HHMMSS"