#!/usr/bin/env bash
# 每日盘后自动更新脚本
# 使用 fetch_data.py（Tushare主 + qstock备）
set -e

cd ~/limit-up-stats

export $(grep -v '^#' ~/daily_stock_analysis/.env 2>/dev/null | xargs)

echo "[$(date)] 开始每日涨跌停数据更新..."

# 1. 数据采集（Tushare主，qstock涨停备用）
python3 scripts/fetch_data.py --daily 2>&1 || true

echo "[$(date)] 数据入库完成"

# 2. 导出JSON
python3 scripts/export_json.py 2>&1 || true

# 3. git 提交并推送
cd ~/limit-up-stats
git add data/
git diff --cached --quiet || git commit -m "📊 每日涨跌停数据更新 $(date +%Y-%m-%d)"
git push origin master

echo "[$(date)] ✅ 每日更新完成！"
