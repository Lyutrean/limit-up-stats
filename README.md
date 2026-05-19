# 涨停板全方位统计分析 (Limit-Up Stats)

A股涨停板历史数据查询与全方位统计分析工具。

## 功能

- 📊 连板梯队分布统计
- 🏭 行业涨停热度排行
- 📈 涨停家数趋势分析
- 🔥 晋级率漏斗图
- 🗺️ 行业轮动热力图
- 💰 封单/市值/换手率分析
- 🔍 个股涨停日历查询
- 📅 历史任意日期查询

## 数据源

- **主数据源**: Tushare Pro (limit_list_d)
- **备数据源**: qstock (zt_pool)
- **概念数据**: Tushare concept + concept_detail

## 项目结构

```
limit-up-stats/
├── index.html          # 网站主页面
├── scripts/            # Python数据采集处理脚本
│   ├── fetch_data.py       # 主采集脚本
│   ├── export_json.py      # SQLite → JSON导出
│   └── requirements.txt    # Python依赖
├── data/               # 前端使用的JSON数据文件
│   └── daily/              # 按日期分文件
├── db/                 # SQLite数据库（本地）
└── pages/              # 其他页面
```

## 部署

```bash
vercel --prod
```
