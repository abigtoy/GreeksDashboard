#!/bin/bash
DATE=$(date "+%Y-%m-%d %H:%M:%S")
cd /c/qproj || exit

git status --porcelain > /tmp/git_status_$$.txt
if [ -s /tmp/git_status_$$.txt ]; then
    echo "[$DATE] 有变更，开始备份..."
    git add -A
    git commit -m "Auto backup $DATE"
    git push 2>&1
    echo "[$DATE] 备份完成"
else
    echo "[$DATE] 无变更，跳过"
fi
rm -f /tmp/git_status_$$.txt
