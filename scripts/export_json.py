#!/usr/bin/env python3
"""从SQLite导出聚合统计数据供前端使用 - 无个股明细，纯统计"""
import sqlite3, json, os
from datetime import datetime, timedelta

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

# ============================================================
# 概念黑名单 - 过滤掉非行业概念
# ============================================================
CONCEPT_BLACKLIST = {
    # 融资融券/标的类
    '转融券标的', '融资融券', '融资标的股', '融券标的股',
    '标普道琼斯A股', '深股通', '沪股通', '港股通',
    # 指数/市场类
    'MSCI概念', 'MSCI中国', '沪深300', '中证500', '上证50',
    '科创50', '创业板指', '富时罗素概念', '央视50',
    # 新股次新
    '新股与次新股', '核准制次新股', '注册制次新股', '次新股',
    # 财务/业绩类
    '年报预增', '季报预增', '半年报预增', '业绩预增',
    '高送转', '高股息股', '破净资产', '低价股', '低市净率',
    # 股东/机构类
    '机构重仓', '证金持股', '汇金持股', '养老金持股',
    '保险重仓', '社保重仓', 'QFII重仓',
    '深股通重仓', '沪股通重仓',
    # 其他非产业概念
    'ST板块', '*ST板块',
    '创业板重组松绑', '重组股', '股权转让', '壳资源',
    '要约收购', '定增预案', '定增股',
    '回购计划', '回购股',
    '被举牌', '举牌概念',
    '参股新三板', '参股银行', '参股券商', '参股保险', '参股基金',
    '参股期货', '参股信托', '参股股权交易中心',
    '创投', '创投资金', '创投概念',
    '独角兽概念', '独角兽',
    '分拆上市', '分拆上市预期',
    '可转债', '可转债标的',
    '优先股', '优先股概念',
    '含H股', '含B股',
    '整体上市', '定向增发',
    '股东减持', '股东增持',
    '员工持股', '股权激励',
    '昨日涨停', '昨日连板', '昨日首板', '昨曾涨停',
    '近期强势', '近期新高',
    '拟减持', '拟增持',
    '大宗交易', '龙虎榜',
}

def is_meaningful_concept(name):
    """判断是否为有意义的行业/产业概念"""
    return name not in CONCEPT_BLACKLIST

# ============================================================
# 1. 汇总统计
# ============================================================
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

# ---- 每日趋势（涨跌停家数） ----
c.execute('''SELECT trade_date,
    SUM(CASE WHEN limit_type='U' THEN 1 ELSE 0 END) as zt_count,
    SUM(CASE WHEN limit_type='D' THEN 1 ELSE 0 END) as dt_count,
    MAX(CASE WHEN limit_type='U' THEN limit_times ELSE 0 END) as max_ban
FROM limit_up
GROUP BY trade_date
ORDER BY trade_date''')
daily_trend = []
for r in c.fetchall():
    daily_trend.append({
        'date': r['trade_date'],
        'zt': r['zt_count'],
        'dt': r['dt_count'],
        'max_ban': r['max_ban']
    })

# ---- 今日数据 ----
today_data = {}
today_zt_ind = {}  # 行业涨停分布
today_dt_ind = {}  # 行业跌停分布
if latest_date:
    c.execute('''SELECT limit_type, COUNT(*) FROM limit_up
                 WHERE trade_date=? GROUP BY limit_type''', (latest_date,))
    type_counts = {r[0]: r[1] for r in c.fetchall()}
    today_data['zt_count'] = type_counts.get('U', 0)
    today_data['dt_count'] = type_counts.get('D', 0)

    c.execute('''SELECT limit_times, COUNT(*) FROM limit_up
                 WHERE trade_date=? AND limit_type='U'
                 GROUP BY limit_times ORDER BY limit_times''', (latest_date,))
    today_data['ban_distribution'] = {str(r[0]): r[1] for r in c.fetchall()}

    c.execute('''SELECT MAX(limit_times) FROM limit_up
                 WHERE trade_date=? AND limit_type='U' ''', (latest_date,))
    today_data['max_ban'] = c.fetchone()[0] or 0

    # 今日行业涨跌停分布（板块情绪）
    c.execute('''SELECT industry, COUNT(*) as cnt FROM limit_up
                 WHERE trade_date=? AND limit_type='U' AND industry IS NOT NULL AND industry!=""
                 GROUP BY industry ORDER BY cnt DESC''', (latest_date,))
    today_zt_ind = {r[0]: r[1] for r in c.fetchall()}

    c.execute('''SELECT industry, COUNT(*) as cnt FROM limit_up
                 WHERE trade_date=? AND limit_type='D' AND industry IS NOT NULL AND industry!=""
                 GROUP BY industry ORDER BY cnt DESC''', (latest_date,))
    today_dt_ind = {r[0]: r[1] for r in c.fetchall()}

# ---- 板块情绪（行业涨跌停） ----
sector_mood = []
all_industries = set(list(today_zt_ind.keys()) + list(today_dt_ind.keys()))
for ind in sorted(all_industries, key=lambda x: today_zt_ind.get(x, 0) + today_dt_ind.get(x, 0), reverse=True):
    zt = today_zt_ind.get(ind, 0)
    dt = today_dt_ind.get(ind, 0)
    if zt + dt > 0:
        sector_mood.append({
            'name': ind,
            'zt': zt,
            'dt': dt,
            'total': zt + dt
        })

# ---- 行业总排行 ----
c.execute('''SELECT industry, COUNT(*) as cnt, AVG(limit_times) as avg_ban
             FROM limit_up WHERE industry IS NOT NULL AND industry!="" AND limit_type='U'
             GROUP BY industry ORDER BY cnt DESC LIMIT 30''')
industry_ranking = [{'name': r[0], 'count': r[1], 'avg_ban': round(r[2], 2)} for r in c.fetchall()]

# ---- 概念热度（过滤后） ----
if latest_date:
    c.execute('''SELECT sc.concept_name, COUNT(DISTINCT lu.ts_code) as cnt
                 FROM stock_concept sc
                 JOIN limit_up lu ON sc.ts_code=lu.ts_code
                 WHERE lu.trade_date=? AND lu.limit_type='U'
                 GROUP BY sc.concept_name ORDER BY cnt DESC LIMIT 50''', (latest_date,))
    all_concepts = [{'name': r[0], 'count': r[1]} for r in c.fetchall()]
    # 过滤
    concept_ranking = [c for c in all_concepts if is_meaningful_concept(c['name'])]
else:
    concept_ranking = []

# ---- 涨跌停连板晋级率（仅涨停） ----
c.execute('SELECT DISTINCT trade_date FROM limit_up ORDER BY trade_date')
all_dates = [r[0] for r in c.fetchall()]
prev_pairs = [(all_dates[i], all_dates[i+1]) for i in range(len(all_dates)-1)]
promotion_rates = {}
for ban_n in [1, 2, 3, 4, 5]:
    total = 0; promoted = 0
    for pd, nd in prev_pairs:
        cc = conn.cursor()
        cc.execute('SELECT ts_code FROM limit_up WHERE trade_date=? AND limit_times=? AND limit_type="U"', (pd, ban_n))
        prev_stocks = set(r[0] for r in cc.fetchall())
        cc.execute('SELECT ts_code FROM limit_up WHERE trade_date=? AND limit_type="U"', (nd,))
        next_stocks = set(r[0] for r in cc.fetchall())
        total += len(prev_stocks)
        promoted += len(prev_stocks & next_stocks)
    promotion_rates[f'{ban_n}→{ban_n+1}'] = round(promoted/total*100, 1) if total > 0 else 0

# ============ 写 summary.json ============
summary = {
    'total_records': total_records,
    'total_days': total_days,
    'total_stocks': total_stocks,
    'total_industries': total_industries,
    'latest_date': latest_date,
    'date_range': {'start': date_range[0], 'end': date_range[1]},
    'today': today_data,
    'daily_trend': daily_trend,
    'sector_mood': sector_mood,          # 板块情绪
    'industry_ranking': industry_ranking,
    'concept_ranking': concept_ranking,   # 过滤后的概念
    'promotion_rates': promotion_rates,
    'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
}
with open(os.path.join(DATA_DIR, 'summary.json'), 'w', encoding='utf-8') as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
print(f'summary.json ✅ (涨{daily_trend[-1]["zt"] if daily_trend else 0} 跌{daily_trend[-1]["dt"] if daily_trend else 0})')

# ============ 2. 每日聚合数据（无个股明细） ============
c.execute('SELECT DISTINCT trade_date FROM limit_up ORDER BY trade_date')
dates = [r[0] for r in c.fetchall()]
for trade_date in dates:
    # 涨停分布
    cc = conn.cursor()
    cc.execute('''SELECT industry, COUNT(*) as cnt FROM limit_up
                  WHERE trade_date=? AND limit_type='U' AND industry IS NOT NULL AND industry!=""
                  GROUP BY industry ORDER BY cnt DESC''', (trade_date,))
    zt_by_ind = [{'name': r[0], 'count': r[1]} for r in cc.fetchall()]

    # 跌停分布
    cc.execute('''SELECT industry, COUNT(*) as cnt FROM limit_up
                  WHERE trade_date=? AND limit_type='D' AND industry IS NOT NULL AND industry!=""
                  GROUP BY industry ORDER BY cnt DESC''', (trade_date,))
    dt_by_ind = [{'name': r[0], 'count': r[1]} for r in cc.fetchall()]

    # 涨停连板分布
    cc.execute('''SELECT limit_times, COUNT(*) FROM limit_up
                  WHERE trade_date=? AND limit_type='U'
                  GROUP BY limit_times ORDER BY limit_times''', (trade_date,))
    ban_dist = {str(r[0]): r[1] for r in cc.fetchall()}

    cc.execute('SELECT MAX(limit_times) FROM limit_up WHERE trade_date=? AND limit_type="U"', (trade_date,))
    max_ban = cc.fetchone()[0] or 0

    cc.execute('SELECT COUNT(*) FROM limit_up WHERE trade_date=? AND limit_type="U"', (trade_date,))
    zt_total = cc.fetchone()[0]

    cc.execute('SELECT COUNT(*) FROM limit_up WHERE trade_date=? AND limit_type="D"', (trade_date,))
    dt_total = cc.fetchone()[0]

    daily_data = {
        'date': trade_date,
        'zt_total': zt_total,
        'dt_total': dt_total,
        'max_ban': max_ban,
        'ban_distribution': ban_dist,
        'zt_by_industry': zt_by_ind,
        'dt_by_industry': dt_by_ind,
    }

    filepath = os.path.join(DATA_DIR, 'daily', f'{trade_date}.json')
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(daily_data, f, ensure_ascii=False, indent=2)

print(f'每日聚合数据 {len(dates)}天 ✅')
conn.close()
print(f'✅ 导出完成！')
