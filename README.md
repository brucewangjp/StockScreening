# 中期突破选股系统（Position Breakout System）

个人用的半自动选股管道：自上而下管风险，自下而上选股票，人工执行下单。
目标是用系统化流程捕捉"横盘蓄势后突破"的中期翻倍候选（持有数周到数月），
同时用多层风控防止单一主题集中、追高、和在错误的市场环境里开仓。

**本系统只输出建议，不自动下单。所有交易由人工在 moomoo / SBI 证券确认执行。**

## 架构

```
① market_regime.py        宏观状态引擎：红/黄/绿灯 + 事件禁新仓标志
        ↓
② moomoo_openapi_screener.py --mode position
                           取数与预筛：moomoo OpenD 拉全市场→预筛→逐只算指标
        ↓
③ position_trend_scanner.py
                           评分器：硬性闸门 + 加权打分 → ALERT/WATCH/SETUP
        ↓
④ position_sizer.py        仓位计算：ATR仓位法 + 集中度上限 → 具体股数和止损价
        ↓
⑤ 人工确认 → 下单 → trade_journal.csv 记录
        ↓
⑥ moomoo_backtest_runner.py --strategy position
                           策略验证：样本内/外划分回测，防过拟合
```

## 各层规则速查

**① 状态引擎**（市场层0失败=绿，1-2=黄，3+=红；慢变量只降不升）
- 市场层：指数>200日线且上行、广度代理、VIX<25、HY利差<400bp且未急扩
- 市场专属：日股盯日元急升（20日>5%），港股盯人民币急贬（20日>2%）
- 慢变量：初请失业金恶化、Sahm规则、收益率曲线解除倒挂、核心PCE再加速
- 事件：FOMC/CPI/非农/日银 前48小时禁新仓（非农自动算，其余维护 macro_event_calendar.csv）
- 灯色→仓位系数：绿1.0 / 黄0.5 / 红0（停止新开仓）

**③ 评分器硬性闸门**（任一不过即淘汰）：流动性、股价>50日线>200日线且200日线上行、
距52周高点25%以内、离52周低点30%以上、6月相对强度为正、排除OTC/SPAC

**③ 评分权重**（满分100）：相对强度25 / 底部形态质量20 / 放量突破15 /
营收增长+加速25 / 高点接近度10 / 催化剂5。ALERT≥70且当日突破，WATCH≥55，
SETUP=条件齐备等突破

**④ 仓位规则**（按序执行）：红灯/事件→拒绝；单笔风险=账户1%×灯色系数；
止损=min(2×ATR, 15%)；单股上限10%；AI/半导体主题合计上限40%；财报前2日不进场

**⑥ 回测退出规则**：-15%硬止损 / 收盘跌破50日线 / 60日期限；同一标的禁止重叠开仓；
看"未知期间"段的期望值，样本内好看样本外差=过拟合

## 周常工作流（周日晚约1小时）

```bash
python3 outputs/market_regime.py --markets US,JP,HK     # 1. 看灯
python3 outputs/moomoo_openapi_screener.py --mode position --markets US,JP,HK --bars-source yahoo  # 2. 全市场扫描(Yahoo数据源,不耗配额)
python3 outputs/position_sizer.py outputs/position_candidates.csv \
    --portfolio-csv outputs/my_portfolio.csv             # 3. 出仓位计划
# 4. 执行候选逐只人工查株探/财报10分钟 → 下单 → 记日志
```

## 数据源与约束

- moomoo OpenD：全市场筛选、个股快照、板块（均不耗配额）；日K仅作兜底
  （历史K线配额100只/7天，免费档）
- Yahoo Finance：日K主数据源（`--bars-source auto/yahoo`，免费无配额），
  符号自动映射 US.PRSU→PRSU / JP.7716→7716.T / HK.00700→0700.HK
- FRED：宏观数据，免费无key，本地缓存断网可用
- 财务数值（营收增速）moomoo不提供：高分候选需手动查株探/财报，
  补进 fundamentals CSV（symbol,revenue_growth_pct,revenue_accel_pp,catalyst）

## 维护清单

- 每月：更新 my_portfolio.csv 评估额
- 每季度：核对 macro_event_calendar.csv 的FOMC/日银日期（CPI自行从BLS添加）
- 每笔交易：trade_journal.csv 必填 thesis/catalyst，平仓补 lesson；
  满50笔做胜因败因统计，作为下一轮参数迭代依据

## 纪律红线

1. 状态引擎只管仓位许可，永远不修改个股评分（保证亏损可归因）
2. 评分器给的是研究名单，不是买入指令；财务未确认的高分票不下单
3. 回测未知期间期望值为正、且样本≥100笔之前，整个策略只用小仓位试运行

## 政策テーマ層（日本17戦略分野）

`config/japan_growth_strategy_17fields.yaml` + `src/policy_theme_score.py` で、
高市政権「日本成長戦略本部」の17戦略分野を産業ベータとして加点する。

**重要: 政策テーマスコア（0-20）は買い判断ではなく産業ベータ加点。**
技術スコア(0-100)には足し込まず別カラムで並走し、status(ALERT/WATCH/IGNORE)を
変えない。下落トレンド・赤字・低流動性の銘柄は政策テーマに該当してもBUYにしない。
最終BUY判定はトレンド・業績・出来高・バリュエーションで行う。政策スコアは
同点時の分散優先タイブレークと中期の追い風フラグとしてのみ使う。

- Aランク(基礎10+5): 防衛/造船/AI半導体/マテリアル/情報通信/コンテンツ
- Bランク(基礎6+2): 航空宇宙/サイバー/防災/GX/創薬/港湾/海洋
- Cランク(基礎3+0): 量子/核融合/合成生物/フードテック

出力カラム: policy_theme_score, policy_theme_main, policy_theme_rank,
policy_theme_sub, policy_theme_reason, policy_theme_keywords_hit

既知の制約: moomooのJP銘柄テーマデータは英語で粗く、コンセプト分類が
ノイズを含む（例: 化学企業に"Shipbuilding"タグ）。政策タグは大まかなヒント
として扱い、重点候補は手動で業種を確認すること。三菱重工クラスの大型は
時価総額上限を上げてスキャンしないと拾えない。
