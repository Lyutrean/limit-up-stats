#!/usr/bin/env python3
"""从SQLite导出JSON数据供前端使用"""
import sqlite3, json, os
from datetime import datetime

DB_PATH = os.path.expanduser('~/limit-up-stats/db/limit_up.db')
DATA_DIR = os.path.expanduser('~/limit-up-stats/data')
os.makedirs(os.path.join(DATA_DIR, 'daily'), exist_ok=True)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute('SELECT COUNT(*) FROM limit_up')
if c.fetchone()[0] == 0:
    print('❌ 数据库为空')
    conn.close()
    exit(0)

# ============ 1. 汇总统计 ============
c.execute('SELECT COUNT(*) FROM limit_up')
total_records = c.fetchone()[0]
c.execute('SELECT COUNT(DISTINCT trade_date) FROM limit_up')
total_days = c.fetchone()[0]
c.execute('SELECT COUNT(DISTINCT ts_code) FROM limit_up')
total_stocks = c.fetchone()[0]
c.execute('SELECT COUNT(DISTINCT industry) FROM limit_up WHERE industry IS NOT NULL AND industry != ""')
total_industries = c.fetchone()[0]
c.execute('SELECT MIN(trade_date), MAX(trade_date) FROM limit_up')
date_range = c.fetchone()
c.execute('SELECT MAX(trade_date) FROM limit_up')
latest_date = c.fetchone()[0]

c.execute('SELECT limit_times, COUNT(*) as cnt FROM limit_up GROUP BY limit_times ORDER BY limit_times')
total_ban_dist = {str(r['limit_times']): r['cnt'] for r in c.fetchall()}

today_data = {}
if latest_date:
    c.execute('SELECT COUNT(*) FROM limit_up WHERE trade_date=?', (latest_date,))
    today_data['count'] = c.fetchone()[0]
    c.execute('SELECT limit_times, COUNT(*) FROM limit_up WHERE trade_date=? GROUP BY limit_times ORDER BY limit_times', (latest_date,))
    today_data['ban_distribution'] = {str(r[0]): r[1] for r in c.fetchall()}
    c.execute('SELECT MAX(limit_times) FROM limit_up WHERE trade_date=?', (latest_date,))
    today_data['max_ban'] = c.fetchone()[0] or 0
    c.execute('''SELECT industry, COUNT(*) as cnt FROM limit_up
                 WHERE trade_date=? AND industry IS NOT NULL AND industry!=""
                 GROUP BY industry ORDER BY cnt DESC LIMIT 10''', (latest_date,))
    today_data['top_industries'] = [{'name': r[0], 'count': r[1]} for r in c.fetchall()]

# 晋级率
c.execute('SELECT DISTINCT trade_date FROM limit_up ORDER BY trade_date')
all_dates = [r[0] for r in c.fetchall()]
prev_pairs = [(all_dates[i], all_dates[i+1]) for i in range(len(all_dates)-1)]
promotion_rates = {}
for ban_n in [1,2,3,4,5]:
    total = 0; promoted = 0
    for pd, nd in prev_pairs:
        cc = conn.cursor()
        cc.execute('SELECT ts_code FROM limit_up WHERE trade_date=? AND limit_times=?', (pd, ban_n))
        prev_stocks = set(r[0] for r in cc.fetchall())
        cc.execute('SELECT ts_code FROM limit_up WHERE trade_date=?', (nd,))
        next_stocks = set(r[0] for r in cc.fetchall())
        total += len(prev_stocks)
        promoted += len(prev_stocks & next_stocks)
    promotion_rates[f'{ban_n}→{ban_n+1}'] = round(promoted/total*100, 1) if total > 0 else 0

c.execute('SELECT trade_date, COUNT(*) as cnt, MAX(limit_times) as max_ban FROM limit_up GROUP BY trade_date ORDER BY trade_date')
daily_trend = [{'date': r[0], 'count': r[1], 'max_ban': r[2]} for r in c.fetchall()]

c.execute('''SELECT industry, COUNT(*) as cnt, AVG(limit_times) as avg_ban
             FROM limit_up WHERE industry IS NOT NULL AND industry!=""
             GROUP BY industry ORDER BY cnt DESC LIMIT 30''')
industry_ranking = [{'name': r[0], 'count': r[1], 'avg_ban': round(r[2],2)} for r in c.fetchall()]

c.execute('''SELECT concept_name, COUNT(*) as cnt
             FROM stock_concept sc JOIN limit_up lu ON sc.ts_code=lu.ts_code
             WHERE lu.trade_date=?
             GROUP BY concept_name ORDER BY cnt DESC LIMIT 20''', (latest_date,))
concept_ranking = [{'name': r[0], 'count': r[1]} for r in c.fetchall()]

c.execute('''SELECT
    CASE WHEN float_mv < 3e9 THEN '小盘(<30亿)'
         WHEN float_mv < 1e10 THEN '中盘(30-100亿)'
         ELSE '大盘(>100亿)' END as cap_range,
    COUNT(*) as cnt, AVG(limit_times) as avg_ban
FROM limit_up WHERE trade_date=? AND float_mv > 0
GROUP BY cap_range ORDER BY cnt DESC''', (latest_date,))
cap_stats = [{'range': r[0], 'count': r[1], 'avg_ban': round(r[2],2)} for r in c.fetchall()]

summary = {
    'total_records': total_records, 'total_days': total_days,
    'total_stocks': total_stocks, 'total_industries': total_industries,
    'latest_date': latest_date,
    'date_range': {'start': date_range[0], 'end': date_range[1]},
    'today': today_data, 'ban_distribution': total_ban_dist,
    'promotion_rates': promotion_rates, 'daily_trend': daily_trend,
    'industry_ranking': industry_ranking, 'concept_ranking': concept_ranking,
    'cap_stats': cap_stats,
    'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
}
with open(os.path.join(DATA_DIR, 'summary.json'), 'w', encoding='utf-8') as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
print(f'summary.json ✅')

# ============ 2. 每日数据 ============
c.execute('SELECT DISTINCT trade_date FROM limit_up ORDER BY trade_date')
dates = [r[0] for r in c.fetchall()]
for trade_date in dates:
    c.execute('''SELECT lu.*, GROUP_CONCAT(sc.concept_name, '|') as concepts
                 FROM limit_up lu LEFT JOIN stock_concept sc ON lu.ts_code=sc.ts_code
                 WHERE lu.trade_date=?
                 GROUP BY lu.ts_code
                 ORDER BY lu.limit_times DESC, lu.pct_chg DESC''', (trade_date,))
    stocks = []
    for r in c.fetchall():
        stocks.append({
            'code': r['ts_code'], 'name': r['name'],
            'industry': r['industry'] or '',
            'pct_chg': r['pct_chg'], 'close': r['close'],
            'ban': r['limit_times'],
            'amount': r['amount'], 'fd_amount': r['fd_amount'],
            'float_mv': r['float_mv'], 'total_mv': r['total_mv'],
            'turnover': r['turnover_ratio'],
            'up_stat': r['up_stat'] or '',
            'concepts': r['concepts'].split('|') if r['concepts'] else []
        })
    with open(os.path.join(DATA_DIR, 'daily', f'{trade_date}.json'), 'w', encoding='utf-8') as f:
        json.dump({'date': trade_date, 'total': len(stocks), 'stocks': stocks}, f, ensure_ascii=False, indent=2)
print(f'每日数据 {len(dates)}天 ✅')

# ============ 3. 个股索引 ============
c.execute('''SELECT ts_code, name, industry, COUNT(*) as total_zt,
             MAX(limit_times) as max_ban, AVG(limit_times) as avg_ban
             FROM limit_up GROUP BY ts_code ORDER BY total_zt DESC LIMIT 5000''')
stocks_idx = [{'code': r[0], 'name': r[1], 'industry': r[2] or '',
               'total_zt': r[3], 'max_ban': r[4], 'avg_ban': round(r[5],2)} for r in c.fetchall()]
with open(os.path.join(DATA_DIR, 'stocks_idx.json'), 'w', encoding='utf-8') as f:
    json.dump(stocks_idx, f, ensure_ascii=False, indent=2)
print(f'个股索引 {len(stocks_idx)}只 ✅')

conn.close()
print(f'✅ 导出完成！')
