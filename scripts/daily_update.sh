#!/usr/bin/env bash
# 每日盘后自动更新脚本
# 在 qstock 虚拟环境中运行，需要网络代理
set -e

cd ~/limit-up-stats

# 加载环境变量
export $(grep -v '^#' ~/daily_stock_analysis/.env 2>/dev/null | xargs)
export http_proxy=http://127.0.0.1:7897
export https_proxy=http://127.0.0.1:7897

echo "[$(date)] 开始每日涨停数据更新..."

# 1. 从 qstock 获取今日数据（qstock需虚拟环境）
source ~/qstock_env/bin/activate
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.path.expanduser('~/limit-up-stats/scripts'))
import sqlite3, json
from datetime import datetime

os.environ.setdefault('http_proxy', 'http://127.0.0.1:7897')
os.environ.setdefault('https_proxy', 'http://127.0.0.1:7897')
import qstock as qs

DB_PATH = os.path.expanduser('~/limit-up-stats/db/limit_up.db')
DATA_DIR = os.path.expanduser('~/limit-up-stats/data')

today = datetime.now().strftime('%Y%m%d')
print(f'拉取 {today} 涨停数据...')

zt = qs.stock_zt_pool()
print(f'qstock: {len(zt)}只涨停')

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# 确保表存在
c.executescript('''
    CREATE TABLE IF NOT EXISTS limit_up (
        trade_date TEXT NOT NULL, ts_code TEXT NOT NULL,
        name TEXT NOT NULL, industry TEXT,
        pct_chg REAL, close REAL, limit_times INTEGER DEFAULT 1,
        amount REAL, fd_amount REAL, float_mv REAL, total_mv REAL,
        turnover_ratio REAL, up_stat TEXT, source TEXT DEFAULT 'qstock',
        PRIMARY KEY (trade_date, ts_code)
    );
    CREATE TABLE IF NOT EXISTS fetch_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date TEXT, source TEXT, count INTEGER,
        status TEXT, message TEXT, fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS stock_concept (
        ts_code TEXT NOT NULL, concept_name TEXT NOT NULL,
        PRIMARY KEY (ts_code, concept_name)
    );
''')

# 清除今天旧数据后重新插入
c.execute('DELETE FROM limit_up WHERE trade_date=?', (today,))

inserted = 0
for _, r in zt.iterrows():
    code = str(r['代码'])
    ts_code = f'{code}.SH' if code.startswith(('6','9')) else f'{code}.SZ'
    try:
        c.execute('''
            INSERT INTO limit_up
            (trade_date, ts_code, name, industry, pct_chg, close,
             limit_times, amount, fd_amount, float_mv, total_mv,
             turnover_ratio, up_stat, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            today, ts_code, r['名称'], r.get('所属行业',''),
            float(r['涨跌幅']), float(r['最新价']),
            int(r['连板数']),
            float(r.get('成交额(百万)',0))*1e6,
            float(r.get('封板资金(百万)',0))*1e6,
            float(r.get('流通市值(百万)',0))*1e6,
            float(r.get('总市值(百万)',0))*1e6,
            float(r.get('换手率',0)),
            r.get('涨停统计',''), 'qstock'
        ))
        inserted += 1
    except Exception as e:
        print(f'  插入失败 {ts_code}: {e}')

conn.commit()
c.execute('INSERT INTO fetch_log (trade_date, source, count, status) VALUES (?,?,?,?)',
          (today, 'qstock', inserted, 'ok'))
conn.commit()
conn.close()
print(f'✅ 入库: {inserted}条')
PYEOF
deactivate

echo "[$(date)] 数据入库完成"

# 2. 导出JSON（使用主环境）
python3 ~/limit-up-stats/scripts/export_json.py 2>/dev/null || true

# 3. git 提交并推送
export GIT_TOKEN="$(grep 'ghp_' ~/goal-plan-website/.git/config | sed 's/.*https:\/\///;s/@.*//')"

cd ~/limit-up-stats
git add data/
git diff --cached --quiet || git commit -m "📊 每日数据更新 $(date +%Y-%m-%d)"
git push origin master

echo "[$(date)] ✅ 每日更新完成！"
