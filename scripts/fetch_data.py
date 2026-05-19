#!/usr/bin/env python3
"""
涨停板数据采集工具
支持多数据源（Tushare主 + qstock备），自动容错切换
"""

import sqlite3
import json
import os
import sys
import time
import argparse
import logging
from datetime import datetime, timedelta, date

import pandas as pd

# 项目根目录
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT_DIR, 'db', 'limit_up.db')
DATA_DIR = os.path.join(ROOT_DIR, 'data')

# 确保目录存在
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, 'daily'), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, 'stocks'), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)


# ============================================================
# 数据库管理
# ============================================================

class Database:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._init_schema()

    def _init_schema(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.executescript('''
            -- 每日涨跌停主表
            CREATE TABLE IF NOT EXISTS limit_up (
                trade_date  TEXT NOT NULL,
                ts_code     TEXT NOT NULL,
                name        TEXT NOT NULL,
                industry    TEXT,
                pct_chg     REAL,
                close       REAL,
                limit_times INTEGER DEFAULT 1,
                amount      REAL,
                fd_amount   REAL,
                float_mv    REAL,
                total_mv    REAL,
                turnover_ratio REAL,
                up_stat     TEXT,
                source      TEXT DEFAULT 'tushare',
                limit_type  TEXT DEFAULT 'U',
                PRIMARY KEY (trade_date, ts_code)
            );

            -- 股票概念映射
            CREATE TABLE IF NOT EXISTS stock_concept (
                ts_code      TEXT NOT NULL,
                concept_name TEXT NOT NULL,
                PRIMARY KEY (ts_code, concept_name)
            );

            -- 交易日历缓存
            CREATE TABLE IF NOT EXISTS trade_calendar (
                cal_date TEXT PRIMARY KEY,
                is_open  INTEGER
            );

            -- 采集日志
            CREATE TABLE IF NOT EXISTS fetch_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT,
                source     TEXT,
                count      INTEGER,
                status     TEXT,
                message    TEXT,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_limit_up_date ON limit_up(trade_date);
            CREATE INDEX IF NOT EXISTS idx_limit_up_code ON limit_up(ts_code);
            CREATE INDEX IF NOT EXISTS idx_limit_up_industry ON limit_up(industry);
            CREATE INDEX IF NOT EXISTS idx_limit_up_times ON limit_up(limit_times);
            CREATE INDEX IF NOT EXISTS idx_stock_concept_code ON stock_concept(ts_code);
        ''')
        conn.commit()
        conn.close()

    def get_conn(self):
        return sqlite3.connect(self.db_path)

    def date_has_data(self, trade_date):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM limit_up WHERE trade_date=?', (trade_date,))
        count = c.fetchone()[0]
        conn.close()
        return count > 0

    def save_limit_up(self, rows, source='tushare'):
        """批量保存涨跌停数据"""
        conn = self.get_conn()
        c = conn.cursor()
        inserted = 0
        for row in rows:
            try:
                c.execute('''
                    INSERT OR REPLACE INTO limit_up
                    (trade_date, ts_code, name, industry, pct_chg, close,
                     limit_times, amount, fd_amount, float_mv, total_mv,
                     turnover_ratio, up_stat, source, limit_type)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ''', (
                    row.get('trade_date'),
                    row.get('ts_code'),
                    row.get('name'),
                    row.get('industry'),
                    row.get('pct_chg'),
                    row.get('close'),
                    row.get('limit_times', 1),
                    row.get('amount'),
                    row.get('fd_amount'),
                    row.get('float_mv'),
                    row.get('total_mv'),
                    row.get('turnover_ratio'),
                    row.get('up_stat'),
                    source,
                    row.get('limit_type', 'U')
                ))
                inserted += 1
            except Exception as e:
                log.warning(f'  保存失败 {row.get("ts_code")}: {e}')
        conn.commit()
        conn.close()
        return inserted

    def save_fetch_log(self, trade_date, source, count, status, message=''):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('''
            INSERT INTO fetch_log (trade_date, source, count, status, message)
            VALUES (?,?,?,?,?)
        ''', (trade_date, source, count, status, message))
        conn.commit()
        conn.close()

    def get_trade_calendar(self, start_date, end_date):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('''
            SELECT cal_date FROM trade_calendar
            WHERE is_open=1 AND cal_date>=? AND cal_date<=?
            ORDER BY cal_date
        ''', (start_date, end_date))
        rows = [r[0] for r in c.fetchall()]
        conn.close()
        return rows

    def save_trade_calendar(self, rows):
        conn = self.get_conn()
        c = conn.cursor()
        c.executemany(
            'INSERT OR REPLACE INTO trade_calendar (cal_date, is_open) VALUES (?,?)',
            rows
        )
        conn.commit()
        conn.close()

    def save_concepts(self, mapping):
        """保存股票→概念映射"""
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('DELETE FROM stock_concept')
        rows = []
        for ts_code, concepts in mapping.items():
            for concept in concepts:
                rows.append((ts_code, concept))
        c.executemany(
            'INSERT OR REPLACE INTO stock_concept (ts_code, concept_name) VALUES (?,?)',
            rows
        )
        conn.commit()
        conn.close()
        log.info(f'概念映射已保存: {len(rows)}条')


# ============================================================
# Tushare 数据源
# ============================================================

class TushareSource:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get('TUSHARE_API_KEY')
        self.pro = None
        if self.api_key:
            import tushare as ts
            self.pro = ts.pro_api(self.api_key)
            log.info('Tushare 数据源初始化成功')

    def is_available(self):
        return self.pro is not None

    def fetch_trade_calendar(self, start_date, end_date):
        """获取交易日历"""
        try:
            df = self.pro.trade_cal(exchange='SSE', start_date=start_date, end_date=end_date)
            return [(r['cal_date'], r['is_open']) for _, r in df.iterrows()]
        except Exception as e:
            log.warning(f'Tushare 交易日历获取失败: {e}')
            return []

    def fetch_limit_list(self, trade_date, limit_type='U'):
        """获取某日涨跌停数据"""
        if not self.is_available():
            return None
        try:
            df = self.pro.limit_list_d(trade_date=trade_date, limit_type=limit_type)
            if df is None or len(df) == 0:
                label = '涨停' if limit_type == 'U' else '跌停'
                log.info(f'  Tushare {trade_date}: 无{label}数据')
                return []

            rows = []
            for _, r in df.iterrows():
                stock_code = r['ts_code']
                row = {
                    'trade_date': trade_date,
                    'ts_code': stock_code,
                    'name': r.get('name', ''),
                    'industry': r.get('industry', ''),
                    'pct_chg': float(r.get('pct_chg', 0)),
                    'close': float(r.get('close', 0)),
                    'limit_times': int(r.get('limit_times', 1)),
                    'amount': float(r.get('amount', 0)) if r.get('amount') else 0,
                    'fd_amount': float(r.get('fd_amount', 0)) if r.get('fd_amount') else 0,
                    'float_mv': float(r.get('float_mv', 0)) if r.get('float_mv') else 0,
                    'total_mv': float(r.get('total_mv', 0)) if r.get('total_mv') else 0,
                    'turnover_ratio': float(r.get('turnover_ratio', 0)) if r.get('turnover_ratio') else 0,
                    'up_stat': r.get('up_stat', ''),
                    'limit_type': limit_type,
                }
                rows.append(row)

            label = '涨停' if limit_type == 'U' else '跌停'
            log.info(f'  Tushare {trade_date}: {len(rows)}只{label}')
            time.sleep(1.2)  # 限速控制
            return rows

        except Exception as e:
            label = '涨停' if limit_type == 'U' else '跌停'
            log.warning(f'  Tushare {trade_date} {label}失败: {e}')
            time.sleep(2)
            return None  # None 表示出错，需要切换数据源

    def fetch_all_concepts(self):
        """获取所有概念及其成分股"""
        if not self.is_available():
            return {}
        try:
            # 获取概念列表
            concepts = self.pro.concept()
            log.info(f'Tushare 概念总数: {len(concepts)}')

            # 构建 ts_code → [concept_names] 映射
            mapping = {}
            for _, concept in concepts.iterrows():
                code = concept['code']
                name = concept['name']
                try:
                    detail = self.pro.concept_detail(id=code)
                    for _, d in detail.iterrows():
                        ts_code = d['ts_code']
                        if ts_code not in mapping:
                            mapping[ts_code] = []
                        mapping[ts_code].append(name)
                    time.sleep(0.15)
                except Exception as e:
                    log.warning(f'  概念 {code} ({name}) 获取失败: {e}')
                    continue

                if len(mapping) % 100 == 0 and len(mapping) > 0:
                    log.info(f'  已处理 {len(mapping)} 只股票的概念映射...')

            log.info(f'概念映射完成: {len(mapping)} 只股票')
            return mapping

        except Exception as e:
            log.error(f'Tushare 概念获取失败: {e}')
            return {}


# ============================================================
# qstock 数据源（备用）
# ============================================================

class QstockSource:
    def __init__(self):
        log.info('qstock 数据源初始化成功（需代理）')

    def is_available(self):
        try:
            import requests
            resp = requests.get('https://www.baidu.com', timeout=3)
            return resp.status_code == 200
        except:
            return False

    def fetch_today_limit_up(self):
        """获取今日涨停数据（qstock只能取当日）"""
        try:
            import qstock as qs
            # 设置代理
            os.environ.setdefault('http_proxy', 'http://127.0.0.1:7897')
            os.environ.setdefault('https_proxy', 'http://127.0.0.1:7897')

            zt = qs.stock_zt_pool()
            if zt is None or len(zt) == 0:
                log.info('  qstock: 今日无涨停数据')
                return []

            today = datetime.now().strftime('%Y%m%d')
            rows = []
            for _, r in zt.iterrows():
                code = str(r.get('代码', ''))
                # 确定交易所后缀
                if code.startswith('6') or code.startswith('9'):
                    ts_code = f'{code}.SH'
                else:
                    ts_code = f'{code}.SZ'

                row = {
                    'trade_date': today,
                    'ts_code': ts_code,
                    'name': r.get('名称', ''),
                    'industry': r.get('所属行业', ''),
                    'pct_chg': float(r.get('涨跌幅', 0)),
                    'close': float(r.get('最新价', 0)),
                    'limit_times': int(r.get('连板数', 1)),
                    'amount': float(r.get('成交额(百万)', 0)) * 1e6 if r.get('成交额(百万)') else 0,
                    'fd_amount': float(r.get('封板资金(百万)', 0)) * 1e6 if r.get('封板资金(百万)') else 0,
                    'float_mv': float(r.get('流通市值(百万)', 0)) * 1e6 if r.get('流通市值(百万)') else 0,
                    'total_mv': float(r.get('总市值(百万)', 0)) * 1e6 if r.get('总市值(百万)') else 0,
                    'turnover_ratio': float(r.get('换手率', 0)),
                    'up_stat': r.get('涨停统计', ''),
                }
                rows.append(row)

            log.info(f'  qstock 今日: {len(rows)}只涨停')
            return rows

        except Exception as e:
            log.warning(f'  qstock 获取失败: {e}')
            return []


# ============================================================
# 主采集逻辑
# ============================================================

class LimitUpCollector:
    def __init__(self):
        self.db = Database()
        self.tushare = TushareSource()
        self.qstock = QstockSource()

    def fetch_single_date(self, trade_date):
        """从多数据源获取某一天的涨跌停数据，自动容错"""
        log.info(f'--- 采集 {trade_date} ---')

        total = 0
        for limit_type in ['U', 'D']:
            label = '涨停' if limit_type == 'U' else '跌停'
            # 先尝试 Tushare
            if self.tushare.is_available():
                rows = self.tushare.fetch_limit_list(trade_date, limit_type=limit_type)
                if rows is not None:
                    count = self.db.save_limit_up(rows, source='tushare')
                    status = 'ok' if len(rows) > 0 else 'empty'
                    self.db.save_fetch_log(trade_date, f'tushare_{limit_type}', count, status)
                    log.info(f'  ✅ {label}: 保存 {count} 条 (tushare)')
                    total += count
                    continue

            # Tushare 失败，尝试 qstock（仅今日，且qstock似乎只能取涨停）
            if limit_type == 'U':
                today_str = datetime.now().strftime('%Y%m%d')
                if trade_date == today_str:
                    rows = self.qstock.fetch_today_limit_up()
                    if rows:
                        for r in rows:
                            r['limit_type'] = 'U'
                        count = self.db.save_limit_up(rows, source='qstock')
                        self.db.save_fetch_log(trade_date, f'qstock_{limit_type}', count, 'ok')
                        log.info(f'  ✅ {label}: 保存 {count} 条 (qstock 备用)')
                        total += count
                        continue

            # 跌停备选：akshare（无需代理）
            if limit_type == 'D':
                try:
                    import akshare as ak
                    # 临时去掉代理（akshare访问东方财富不需要代理）
                    old_proxy = os.environ.pop('http_proxy', None)
                    os.environ.pop('https_proxy', None)
                    try:
                        df = ak.stock_zt_pool_dtgc_em(date=trade_date)
                        if df is not None and len(df) > 0:
                            rows = []
                            for _, r in df.iterrows():
                                code = str(r['代码'])
                                ts_code = f'{code}.SH' if code.startswith(('6','9')) else f'{code}.SZ'
                                rows.append({
                                    'trade_date': trade_date,
                                    'ts_code': ts_code,
                                    'name': r.get('名称', ''),
                                    'industry': r.get('所属行业', ''),
                                    'pct_chg': float(r.get('涨跌幅', -10)),
                                    'close': float(r.get('最新价', 0)),
                                    'limit_times': 1,
                                    'amount': 0, 'fd_amount': 0,
                                    'float_mv': 0, 'total_mv': 0,
                                    'turnover_ratio': 0, 'up_stat': '',
                                    'limit_type': 'D',
                                })
                            count = self.db.save_limit_up(rows, source='akshare')
                            self.db.save_fetch_log(trade_date, f'akshare_{limit_type}', count, 'ok')
                            log.info(f'  ✅ {label}: 保存 {count} 条 (akshare 备用)')
                            total += count
                            continue
                    finally:
                        if old_proxy:
                            os.environ['http_proxy'] = old_proxy
                except Exception as e:
                    log.warning(f'  akshare {label} 失败: {e}')
                    if old_proxy:
                        os.environ['http_proxy'] = old_proxy

            log.warning(f'  ❌ {label} 所有数据源均失败: {trade_date}')
            self.db.save_fetch_log(trade_date, f'all_{limit_type}', 0, 'failed', f'{label}数据源不可用')

        return total

    def fetch_history(self, start_date, end_date):
        """批量拉取历史数据"""
        log.info(f'=== 开始批量采集: {start_date} ~ {end_date} ===')

        # 先同步交易日历
        cal_rows = self.tushare.fetch_trade_calendar(start_date, end_date)
        if cal_rows:
            self.db.save_trade_calendar(cal_rows)
            trade_dates = [d for d, is_open in cal_rows if is_open]
            log.info(f'交易日: {len(trade_dates)}天')
        else:
            # 无日历，按工作日估算
            log.warning('无法获取交易日历，使用工作日估算')
            trade_dates = self._estimate_trade_days(start_date, end_date)

        total = 0
        success = 0
        for dt in trade_dates:
            if self.db.date_has_data(dt):
                log.info(f'  ⏭️  {dt} 已有数据，跳过')
                continue
            count = self.fetch_single_date(dt)
            if count > 0:
                success += 1
            total += count
            # 每10天汇报一次
            if success % 10 == 0 and success > 0:
                log.info(f'  [进度] 已采集 {success} 天, {total} 条记录')

        log.info(f'=== 批量采集完成: {success}天, {total}条 ===')
        return total

    def daily_update(self):
        """每日盘后增量更新（最近3天，防止漏数据）"""
        log.info('=== 每日增量更新 ===')
        today = datetime.now()
        total = 0
        for i in range(3):
            dt = (today - timedelta(days=i)).strftime('%Y%m%d')
            if not self.db.date_has_data(dt):
                count = self.fetch_single_date(dt)
                total += count
            else:
                log.info(f'  ⏭️  {dt} 已有数据')
        log.info(f'=== 增量更新完成: {total}条 ===')
        return total

    def build_concept_mapping(self):
        """构建股票→概念映射"""
        log.info('=== 构建概念映射 ===')
        if not self.tushare.is_available():
            log.error('Tushare 不可用，无法构建概念映射')
            return False
        mapping = self.tushare.fetch_all_concepts()
        if mapping:
            self.db.save_concepts(mapping)
            return True
        return False

    def _estimate_trade_days(self, start_date, end_date):
        """粗略估算交易日（周一~周五）"""
        start = datetime.strptime(start_date, '%Y%m%d')
        end = datetime.strptime(end_date, '%Y%m%d')
        days = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                days.append(current.strftime('%Y%m%d'))
            current += timedelta(days=1)
        return days


# ============================================================
# 数据导出为JSON（供前端使用）
# ============================================================

class DataExporter:
    def __init__(self):
        self.db = Database()

    def export_all(self):
        """导出所有数据为JSON"""
        log.info('=== 导出数据到JSON ===')

        # 1. 汇总统计
        log.info('生成汇总统计...')
        summary = self._build_summary()

        # 2. 每日数据
        log.info('生成每日数据...')
        self._export_daily_data()

        # 3. 概念映射
        log.info('生成概念映射...')
        self._export_concepts()

        # 4. 个股数据
        log.info('生成个股数据索引...')
        self._export_stock_index()

        # 5. 保存汇总
        with open(os.path.join(DATA_DIR, 'summary.json'), 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        log.info(f'✅ 导出完成: {DATA_DIR}')

    def _build_summary(self):
        """构建汇总统计数据"""
        conn = self.db.get_conn()
        c = conn.cursor()

        # 总览
        c.execute('SELECT COUNT(*) FROM limit_up')
        total_records = c.fetchone()[0]

        c.execute('SELECT COUNT(DISTINCT trade_date) FROM limit_up')
        total_days = c.fetchone()[0]

        c.execute('SELECT COUNT(DISTINCT ts_code) FROM limit_up')
        total_stocks = c.fetchone()[0]

        c.execute('SELECT COUNT(DISTINCT industry) FROM limit_up WHERE industry IS NOT NULL AND industry != ""')
        total_industries = c.fetchone()[0]

        # 最新日期
        c.execute('SELECT MAX(trade_date) FROM limit_up')
        latest_date = c.fetchone()[0]

        # 日期范围
        c.execute('SELECT MIN(trade_date), MAX(trade_date) FROM limit_up')
        date_range = c.fetchone()

        # 连板分布（全部历史）
        c.execute('''
            SELECT limit_times, COUNT(*) as cnt
            FROM limit_up
            GROUP BY limit_times
            ORDER BY limit_times
        ''')
        total_ban_dist = {str(r[0]): r[1] for r in c.fetchall()}

        # 今日数据（最新一天）
        today_data = {}
        if latest_date:
            c.execute('SELECT COUNT(*) FROM limit_up WHERE trade_date=?', (latest_date,))
            today_data['count'] = c.fetchone()[0]

            c.execute('''
                SELECT limit_times, COUNT(*) FROM limit_up
                WHERE trade_date=?
                GROUP BY limit_times ORDER BY limit_times
            ''', (latest_date,))
            today_data['ban_distribution'] = {str(r[0]): r[1] for r in c.fetchall()}

            c.execute('SELECT MAX(limit_times) FROM limit_up WHERE trade_date=?', (latest_date,))
            today_data['max_ban'] = c.fetchone()[0] or 0

            c.execute('SELECT industry, COUNT(*) as cnt FROM limit_up WHERE trade_date=? AND industry IS NOT NULL AND industry!="" GROUP BY industry ORDER BY cnt DESC LIMIT 10', (latest_date,))
            today_data['top_industries'] = [{'name': r[0], 'count': r[1]} for r in c.fetchall()]

        # 连板晋级率（全部历史）
        promotion_rates = self._calc_promotion_rates(conn)

        # 每日趋势
        c.execute('''
            SELECT trade_date, COUNT(*) as cnt, MAX(limit_times) as max_ban
            FROM limit_up
            GROUP BY trade_date
            ORDER BY trade_date
        ''')
        daily_trend = [{'date': r[0], 'count': r[1], 'max_ban': r[2]} for r in c.fetchall()]

        # 行业总排行
        c.execute('''
            SELECT industry, COUNT(*) as cnt, AVG(limit_times) as avg_ban
            FROM limit_up
            WHERE industry IS NOT NULL AND industry!=""
            GROUP BY industry
            ORDER BY cnt DESC
            LIMIT 30
        ''')
        industry_ranking = [{'name': r[0], 'count': r[1], 'avg_ban': round(r[2], 2)} for r in c.fetchall()]

        conn.close()

        return {
            'total_records': total_records,
            'total_days': total_days,
            'total_stocks': total_stocks,
            'total_industries': total_industries,
            'latest_date': latest_date,
            'date_range': {
                'start': date_range[0] if date_range else '',
                'end': date_range[1] if date_range else ''
            },
            'today': today_data,
            'ban_distribution': total_ban_dist,
            'promotion_rates': promotion_rates,
            'daily_trend': daily_trend,
            'industry_ranking': industry_ranking,
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

    def _calc_promotion_rates(self, conn):
        """计算连板晋级率"""
        c = conn.cursor()
        rates = {}

        # 统计：某天有N连板的股票 → 第二天变成N+1连板的比例
        prev_trade_dates = self._get_trade_date_pairs(conn)

        for ban_n in [1, 2, 3, 4, 5]:
            total = 0
            promoted = 0
            for prev_date, next_date in prev_trade_dates:
                c.execute('''
                    SELECT ts_code FROM limit_up
                    WHERE trade_date=? AND limit_times=?
                ''', (prev_date, ban_n))
                stocks_prev = set(r[0] for r in c.fetchall())

                c.execute('''
                    SELECT ts_code FROM limit_up
                    WHERE trade_date=?
                ''', (next_date,))
                stocks_next = set(r[0] for r in c.fetchall())

                promoted_stocks = stocks_prev & stocks_next
                total += len(stocks_prev)
                promoted += len(promoted_stocks)

            if total > 0:
                rates[f'{ban_n}→{ban_n+1}'] = round(promoted / total * 100, 1)
            else:
                rates[f'{ban_n}→{ban_n+1}'] = 0

        return rates

    def _get_trade_date_pairs(self, conn):
        """获取相邻交易日对"""
        c = conn.cursor()
        c.execute('SELECT DISTINCT trade_date FROM limit_up ORDER BY trade_date')
        dates = [r[0] for r in c.fetchall()]
        pairs = []
        for i in range(len(dates) - 1):
            pairs.append((dates[i], dates[i+1]))
        return pairs

    def _export_daily_data(self):
        """按日导出JSON"""
        conn = self.db.get_conn()
        c = conn.cursor()
        c.execute('SELECT DISTINCT trade_date FROM limit_up ORDER BY trade_date')
        dates = [r[0] for r in c.fetchall()]

        for trade_date in dates:
            c.execute('''
                SELECT ts_code, name, industry, pct_chg, close,
                       limit_times, amount, fd_amount, float_mv, total_mv,
                       turnover_ratio, up_stat, source
                FROM limit_up
                WHERE trade_date=?
                ORDER BY limit_times DESC, pct_chg DESC
            ''', (trade_date,))

            stocks = []
            for r in c.fetchall():
                stock = {
                    'code': r[0],
                    'name': r[1],
                    'industry': r[2] or '',
                    'pct_chg': r[3],
                    'close': r[4],
                    'ban': r[5],
                    'amount': r[6],
                    'fd_amount': r[7],
                    'float_mv': r[8],
                    'total_mv': r[9],
                    'turnover': r[10],
                    'up_stat': r[11] or '',
                    'source': r[12],
                }
                stocks.append(stock)

            # 补充概念
            for s in stocks:
                code = s['code']
                c.execute('SELECT concept_name FROM stock_concept WHERE ts_code=?', (code,))
                concepts = [r[0] for r in c.fetchall()]
                s['concepts'] = concepts

            filepath = os.path.join(DATA_DIR, 'daily', f'{trade_date}.json')
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump({
                    'date': trade_date,
                    'total': len(stocks),
                    'stocks': stocks
                }, f, ensure_ascii=False, indent=2)

        conn.close()
        log.info(f'  每日数据: {len(dates)}天')

    def _export_concepts(self):
        """导出概念映射"""
        conn = self.db.get_conn()
        c = conn.cursor()
        c.execute('SELECT concept_name, COUNT(*) as cnt FROM stock_concept GROUP BY concept_name ORDER BY cnt DESC')
        concepts = [{'name': r[0], 'stock_count': r[1]} for r in c.fetchall()]
        conn.close()

        with open(os.path.join(DATA_DIR, 'concepts.json'), 'w', encoding='utf-8') as f:
            json.dump(concepts, f, ensure_ascii=False, indent=2)
        log.info(f'  概念列表: {len(concepts)}个')

    def _export_stock_index(self):
        """导出个股索引"""
        conn = self.db.get_conn()
        c = conn.cursor()
        c.execute('''
            SELECT ts_code, name, industry, COUNT(*) as total_zt,
                   MAX(limit_times) as max_ban,
                   AVG(limit_times) as avg_ban
            FROM limit_up
            GROUP BY ts_code
            ORDER BY total_zt DESC
            LIMIT 5000
        ''')
        stocks = []
        for r in c.fetchall():
            stocks.append({
                'code': r[0],
                'name': r[1],
                'industry': r[2] or '',
                'total_zt': r[3],
                'max_ban': r[4],
                'avg_ban': round(r[5], 2)
            })
        conn.close()

        with open(os.path.join(DATA_DIR, 'stocks_idx.json'), 'w', encoding='utf-8') as f:
            json.dump(stocks, f, ensure_ascii=False, indent=2)
        log.info(f'  个股索引: {len(stocks)}只')


# ============================================================
# CLI 入口
# ============================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='涨停板数据采集工具')
    parser.add_argument('--history', type=str, nargs=2, metavar=('START', 'END'),
                        help='批量采集历史数据: --history 20230101 20260520')
    parser.add_argument('--daily', action='store_true',
                        help='每日增量更新（最近3天）')
    parser.add_argument('--concepts', action='store_true',
                        help='构建概念映射（一次性）')
    parser.add_argument('--export', action='store_true',
                        help='导出数据为JSON（给前端用）')
    parser.add_argument('--all', action='store_true',
                        help='全流程：更新数据 + 导出JSON')
    args = parser.parse_args()

    collector = LimitUpCollector()

    if args.concepts:
        collector.build_concept_mapping()

    if args.history:
        start, end = args.history
        collector.fetch_history(start, end)

    if args.daily:
        collector.daily_update()

    if args.export:
        exporter = DataExporter()
        exporter.export_all()

    if args.all:
        collector.daily_update()
        exporter = DataExporter()
        exporter.export_all()

    if not any([args.history, args.daily, args.concepts, args.export, args.all]):
        parser.print_help()
