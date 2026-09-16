#!/bin/bash
DATE=$(date "+%Y-%m-%d %H:%M:%S")
cd /c/qproj || exit

# 统计变更行数（新增+删除）
STAT=$(git diff --stat HEAD 2>/dev/null | tail -1)
ADDED=$(echo "$STAT" | grep -oP '\d+(?= insertion)' || echo 0)
DELETED=$(echo "$STAT" | grep -oP '\d+(?= deletion)' || echo 0)
LINES=$((ADDED + DELETED))

echo "[$DATE] 变更统计: +$ADDED -$DELETED (共 ${LINES} 行)"

# 变更超过300行，或有新增文件，则备份
HAS_NEW=$(git status --porcelain | grep "^?" | wc -l)

if [ "$LINES" -gt 300 ] || [ "$HAS_NEW" -gt 0 ]; then
    echo "[$DATE] 变更量 ${LINES} 行 > 300，或有 $HAS_NEW 个新文件，开始备份..."
    git add -A
    git commit -m "Auto backup $DATE (+$ADDED -$DELETED)"
    git push 2>&1
    echo "[$DATE] 备份完成"
else
    echo "[$DATE] 变更量 ${LINES} 行 ≤ 300，无新文件，跳过"
fi
