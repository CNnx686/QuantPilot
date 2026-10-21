# QuantPilot MVP2：多交易所、新闻、强化学习与实盘设计

日期：2026-10-15。基线：MVP1 / 6501551。本文保留最初设计提案时的决策背景；当时尚未实现新增能力。后续实现与实际验收以 [MVP2 验收记录](../acceptance-mvp2.md) 和各模块 API 合同为准。最终 RL 采用独立 SB3 环境加 Freqtrade 回放，未采用下文提案中的原生 FreqAI RL 路线。MVP1 标签和历史证据保留，真实账户未启用。

## 1. 产品决策

保留 FastAPI + React + Freqtrade/FreqAI。建设统一研究与执行平台：多交易所数据 → 新闻事件 → 策略实验 → 对照评估 → 分级发布 → 模拟/真实执行 → 订单对账与报告。

接入顺序为多交易所基础、新闻中心、RL 实验室、真实资金执行。每个阶段都能独立演示。第一版交易范围保持加密资产现货、BTC/ETH、1h；不同时扩张股票、杠杆、跨所套利或资金划转。实盘先验证一个交易所、一个专用账户、一个 SMA 策略，再扩展。新闻与 RL 可先在研究模式工作，不要求实盘才能体现价值。

## 2. 已核对的本地事实

- requirements.lock：Freqtrade 2026.9，CCXT 4.5.85；没有锁定 torch、stable-baselines3、gymnasium，RL 依赖需要单独兼容性验证。
- quantpilot/engine.py 的 engine_config 固定 Binance、spot、dry_run；data.py 使用 Binance 历史归档及固定 manifest。
- quantpilot/research.py 只接受 SmaTrend/FreqaiTrend；releases.py 的 teaching-simulation-v1 仅允许 SMA 教学模拟，不是实盘准入。
- deployments.py 清空交易所凭据、强制 dry_run、使用独立进程和数据库；不能把它直接改成一个可任意翻转的布尔开关。
- api.py 有 localhost Host/Origin 限制，但没有适用于实盘的用户认证及短期授权机制。
- 已安装 FreqAI 源码含 ReinforcementLearner、Base3/4/5ActionRLEnv；三动作基类的 Buy/Sell 语义需检查，不能只改动作名字便宣称是目标现货环境。
- 当前 ML flagged 不变。新闻、RL 或多交易所接入不能自动给原策略解除限制。

## 3. GitHub 选型与复用方式

截至本次查阅，以下均为项目自身仓库/官方文档。网页 stable/master 不是本机版本锁；实现前固定具体 tag/commit 并检查 LICENSE、依赖和接口。

| 项目 | 采用内容 | 集成决策 |
|---|---|---|
| [Freqtrade](https://github.com/freqtrade/freqtrade) | 回测、订单生命周期、dry-run/live、交易所适配 | 继续作为唯一交易执行者，扩展外围控制与审核 |
| [CCXT](https://github.com/ccxt/ccxt) | 统一公共行情、市场规格、交易所能力 | 独立公共数据适配层；Freqtrade 管理的账户不再由第二个 CCXT 下单器重复写入 |
| [Stable-Baselines3](https://github.com/DLR-RM/stable-baselines3) | PPO、自定义环境、训练日志 | 经 FreqAI 集成，初版只实现 PPO |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | 金融环境、训练/测试流程、基线组织 | 参考设计，不把整套依赖和另一套执行引擎塞进现有环境 |
| [FinGPT](https://github.com/AI4Finance-Foundation/FinGPT) | 金融情绪、实体和事件抽取思路 | 复用任务定义；初版调用已有 DeepSeek 适配器，不要求本机微调大模型 |
| [RSSHub](https://github.com/DIYgod/RSSHub) | 非标准站点的 RSS 聚合 | 可选独立源适配；优先网站直接 RSS。当前仓库显示 AGPL-3.0，不能按旧印象视为 MIT |

FinRL 仓库当前将新架构指向 FinRL-X/FinRL-Trading；本项目暂不迁移，避免推倒已验证执行链。FinGPT 的研究任务与结果不代表它在 BTC/ETH 新闻上有效，仍需单独标注评估。

## 4. 多交易所中心

### 页面和流程

交易所卡片显示：公共行情连接、最近成功更新时间、延迟、数据缺口、可用交易对、精度/最小金额、行情/回测/模拟/实盘各自支持状态。网络失败显示不可用，不静默切换成另一交易所数据。

技术候选为 Binance、OKX、Kraken。先做两家公共行情读接口验收；真实账户选用取决于用户可合法使用的账户、当地服务范围及实测支持，不承诺三家实盘都可用。CCXT 支持不等于当前 Freqtrade 版本已通过该交易所的实盘验收。

### 关键模型

- MarketKey = exchange_id + market_type + symbol + timeframe；BTC/USDT 与 BTC/USD 不自动合并。
- DatasetV2 = MarketKey + UTC 时间区间 + 原始来源 + 采集时间 + 内容哈希 + 质量状态。
- ExchangeCapability = public_market / historical / dry_run / testnet / live，加 checked_at、version、evidence；未知必须 unknown。
- AccountRef = account_id + exchange_id + credential_ref；API 返回引用与权限检测状态，不返回密钥。
- Deployment = mode + exchange_id + account_id + release_id + capital_budget。每个交易所/账户独立引擎、日志、数据库。

旧数据通过读时兼容映射识别为 Binance，不能改写旧实验 JSON 或哈希。新实验明确绑定交易所；跨所结果对照标明来源不同，不能当作严格同条件策略对照。账户资产保留原币种；估值显示报价时间和缺失状态。

验收：两家公共数据成功读取；限流退避与缓存；币种精度/最小金额校验；同名交易对不串数据；断网/陈旧状态明确；旧报告仍可核验。testnet、dry-run、live 分开命名，只有实际测试过的 testnet 才显示可用。

## 5. 新闻情报中心

### 用户体验

新闻列表支持资产、来源、事件类型和时间过滤；详情包含原文链接、原发布时间、首次采集时间、修订、摘要、情绪、相关资产、证据句以及处理状态。一键创建研究草稿时保留 article_id；不生成订单。

采集先限定 2–3 个有允许访问方式的 RSS/API 源，候选为官方交易所公告和加密媒体 RSS，接入时验证地址、条款、稳定性。优先保存标题、必要摘要、链接与内容指纹；全文仅在权限允许时保存。RSSHub 不作为绕过来源限制的手段。

### 数据处理

采集 → URL/内容哈希去重 → 事件聚类 → 资产识别 → 结构化摘要/情绪 → 人工抽查 → 研究引用。LLM 输出固定 schema，枚举事件和情绪；其自述置信度不作为概率。预算限额、每条内容+模型版本缓存、失败显示待处理；新闻文本作为不可信数据，无工具权限、无密钥、无下单接口。抓取仅允许配置域名，禁止任意 URL 和私网跳转；RSS/HTML 清洗，防止 SSRF 与脚本注入。

ArticleVersion 保存 source、canonical_url、published_at、first_seen_at、revision_seen_at、content_hash、raw_ref；Analysis 保存 model_id、prompt_hash、analyzed_at、article_version_id、结构化结果。发布时刻缺失不伪造，标记 unknown。

### 避免时间泄漏

对在线策略，原文特征可用时间不早于 max(published_at, first_seen_at, revision_seen_at)；LLM 派生特征还需包含 analyzed_at。按特征实际可用时刻 as-of join，再进入已闭合 K 线的决策；禁止只按新闻标题日期回填。

今天采集的 2025 年新闻可供阅读，不直接进入 2025 年回测。若引入可靠历史快照和模拟分析延迟，须作为独立研究数据版本记录假设。现代 LLM 可能记得历史后续事件，即使输入截断也不能消除这种污染；用于历史评估时标明探索性，正式证据优先来自前向采集。实时增量积累新闻数据期间，RL 先只用价格特征。

验收：重复采集不重复计数、修订不覆盖旧版本；无模型仍可浏览；过期新闻不触发新决策；同一事件多篇报道去重；含恶意指令的新闻只能成为文本；报告可追踪新闻版本和分析来源。

## 6. 强化学习实验室

首个实验：单交易所 BTC 现货、1h、PPO、每次只训练一个模型，限制 CPU 线程为 2。随后再做 ETH 独立策略。新建隔离 RL 虚拟环境与专用锁文件，保留 MVP1 的 Python 3.13 环境；兼容性探针决定 RL 环境版本，不直接升级现有依赖。

### 状态、动作、奖励

- 第一版观测只含过去已闭合 OHLCV、收益、波动和量价指标，标准化只在训练窗口拟合。FreqAI 文档指出部分账户状态在回测不可用，初版禁用 add_state_info；若加入仓位、浮盈、持仓时长，必须先实现训练/回测/模拟一致的账户状态提供器。
- 自定义现货动作：保持、买入固定预算、卖出当前策略持仓。重复买入、空仓卖出确定性转为保持，另记录 invalid_action；不映射为空头。
- 奖励候选：r_t = log(E_t / E_(t-1)) - λ × max(0, DD_t - DD_(t-1)) - η × invalid_action_t。E 为包含未实现盈亏、已扣手续费与配置滑点的权益；不能再次重复扣同一成本。λ、η 在开发集冻结，不根据最终测试调节。
- 环境必须显式复现闭合 K 线决策、后续执行价格、费用、持仓约束、止损和终局清算；没有实现的成交条件列为差异，不把训练 reward 当策略收益。
- 从 FreqAI 自定义 learner/environment 接入；发布模型动作后用实际 Freqtrade 回测重新计算表现。训练环境与执行引擎逐步对账是准入条件。

### 评估与展示

记录模型哈希、特征版本、训练窗口、种子、归一化参数、环境版本、动作日志。5 个预先固定种子分别报告，展示中位数、范围、回撤、换手、手续费和非法动作率，不只挑最佳种子。与 SMA、同条件买入持有、现有 LightGBM 并列（LightGBM flagged 持续展示）。

沿用开发/验证分离，时间顺序滚动评估，禁用随机打乱；不自动打开最终保留集。现有 Q3 已用于多次研发，不重新包装为盲测；新最终评估要先冻结规则，之后仅执行一次预注册评估。

UI 展示训练任务、曲线、动作时间线、费用压力测试与基线比较。第一阶段发布等级仅 research。进入 shadow/dry-run 需额外预测有效性与实时一致性验收；RL 的首版不自动获得实盘资格。

## 7. 真实资金交易模块

### 独立准入与控制面

保留 teaching-simulation-v1，新增 live 发布类型，旧通过记录不得重用。默认关闭实盘；凭据由用户在本机配置，采用系统凭据存储/受限 ACL 文件，禁止前端 localStorage、Git、日志或报告保存秘密。执行服务仅收到该账户所需凭据，新闻和训练进程无法访问。

实盘启用前增加登录会话、精确 Origin/CSRF 校验、Host 校验和本地绑定；普通模型或前端请求不能修改账户权限、资金上限或启用实盘。页面持续显示交易所、账户标识、真实资金模式、配置哈希、预算和未平仓敞口。

准入流程：历史评估 → 影子运行/模拟证据 → 独立 live 风控检查 → 用户确认账户/范围/额度 → 初始暂停 → 用户显式启用。账户、预算、策略或模型版本变化都使旧授权失效。上线真实下单属于后续独立操作，本设计不视为下单授权。

### 执行一致性

由 Freqtrade 唯一下单。QuantPilot 对 deployment command 建立幂等键和不可变 command journal；一个账户采用专用子账户或禁止外部写入，避免多机器人争用余额。风险预算跨实例需要事务性预留，不能各实例各自检查后同时超支。

风险检查必须在实际订单提交链内执行（策略回调/已验证引擎钩子），覆盖自动开仓、重挂和恢复；外部 UI 风控开关不足以保证拦截。需验证引擎原生止损等订单路径的例外和保护逻辑。

每次订单前检查：资金预算、单笔金额、最大敞口、日内损失、交易对白名单、报价时效、价差、市场精度和交易所可用性。损失统计使用含浮盈亏的账户权益并调整充值/提现影响；价格过期或余额不确定时拒绝增加风险。

交易请求超时标为 unknown，先依据引擎订单 ID/clientOrderId（平台支持时）查询和对账，不盲目重试。对账处理部分成交、取消与成交竞态、重复事件、漏报和手续费币种；成交事实以交易所为准。进程重启先拉取仓位/开放订单/成交并恢复 journal，对账未完成禁止新开仓。

状态：disabled → validating → ready_paused → active；异常进入 entry_blocked 或 reconciliation_required。不能将进程 stopped 解释为资产安全或仓位清空。

### 三种停止动作分离

| 动作 | 行为 |
|---|---|
| 禁止新开仓 | 停止增加风险，继续管理已有仓位与保护单 |
| 停止进程 | 不再本地管理仓位；展示遗留仓位/订单，外部保护单是否有效需核验 |
| 撤单/平仓 | 独立、明确的真实交易指令，需要用户针对动作确认，不混入停止按钮 |

权限要求：专用交易 API key、无提现权限、平台支持时 IP 白名单；不自动申请或扩大权限。账户是否可用和平台规则在实际接入时按官方信息确认。

验收先使用可控适配器与该平台实际支持的测试环境，证明重复点击不重复启动/下单、超时不重复提交、部分成交对账、重启恢复、风控拒单、密钥脱敏及旧模拟流程不变。再收集真实只读账户证据。实盘启用和资金上限由用户明确确定，代码开发不需要提前接收其资金或密钥。

## 8. 接口与文件边界（拟新增，尚未实现）

| 模块 | 文件 | 责任 |
|---|---|---|
| 交易所 | quantpilot/exchanges.py、exchange_api.py | 能力清单、公共行情、账户引用 |
| 新闻 | quantpilot/news_sources.py、news_store.py、news_analysis.py、news_api.py | 白名单采集、版本、异步分析、检索 |
| RL | models/QuantPilotPPO.py、models/rl/SpotEnvironment.py、strategies/RlSpotTrend.py、quantpilot/rl_jobs.py | 隔离训练、环境和动作适配 |
| 实盘 | quantpilot/live_policy.py、live_deployments.py、risk.py、credentials.py、order_journal.py | 独立准入、风险检查、凭据引用与对账 |
| 前端 | ExchangePanel.tsx、NewsPanel.tsx、RlLabPanel.tsx、LivePanel.tsx | 四个清晰分区，与原有报告串联 |

现有 data.py/engine.py/research.py 扩展 v2 数据契约；store.py/deployment_store.py 增加显式版本迁移，迁移前备份，旧工件只读。reports.py/replay.py 新增引用型证据字段，保留旧 schema 解析。

接口草案：GET /api/exchanges、GET /api/exchanges/{id}/markets；GET /api/news、POST /api/news/sync、POST /api/news/{id}/analyze；POST /api/rl/experiments；POST /api/live/preflight、POST /api/live/deployments、POST /api/live/deployments/{id}/commands。长任务返回 job_id；所有有副作用命令验证鉴权、幂等键、资源版本和模式，不允许客户端提交任意引擎配置或 shell。

DecisionTrace 连接 dataset_id、article_version_ids、analysis_ids、model_hash、strategy_hash、signal_time、risk_decision、deployment_id、engine_order_id。没有的关联显示未记录，不用 LLM 补造交易理由。

## 9. 分阶段交付与答辩

| 阶段 | 交付件 | 完成条件 | 粗估开发投入 |
|---|---|---|---|
| A | 多交易所公共数据与数据版本 | 两家数据、异常状态、旧数据兼容测试 | 2–4 工作日 |
| B | 新闻中心和研究引用 | 可检索、去重、摘要、时间边界、离线已存新闻 | 3–5 工作日 |
| C | PPO 训练和真实引擎对照 | 隔离环境、小规模训练、5 种子结果、动作/成交核对 | 5–8 工作日 |
| D | 实盘控制面和执行验收 | 认证、独立发布、风险拦截、对账、测试环境证据 | 7–12 工作日 |

以上是设计估算，不是后台任务或完成承诺；账户可用性、行情/新闻源权限和 RL 兼容性可能增加时间。真实资金观察周期另算，不承诺短期证明策略有效。答辩近时优先保证 A/B/C 可演示，D 展示完整预检与测试流程；界面必须真实标注测试环境，不能冒充真实成交。

建议演示主线：选择交易所 → 查看新闻与可用时间 → 从新闻创建研究假设 → 对比 SMA/ML/PPO → 解释为何通过或被拒绝 → 展示部署模式和风险限制 → 回放信号、审核与订单证据。成功标准是业务链可解释、失败会被正确处理，不是所有模型都赚钱。

下一次开发从 A 开始；B 不依赖 RL，C 不依赖新闻数据已积累，D 必须基于 A 和新安全控制面。每阶段单独编写可执行实现计划、失败用例、验收与提交，不一次重写 MVP1。

## 10. 依据与未验证事项

- [Freqtrade 交易所列表](https://docs.freqtrade.io/en/latest/)：平台支持说明需与本机版本和账户地区匹配。
- [Freqtrade 实盘与模拟配置](https://www.freqtrade.io/en/stable/configuration/)：提供模式/账户配置基础，不能代替本项目实盘准入。
- [FreqAI RL 官方文档](https://www.freqtrade.io/en/stable/freqai-reinforcement-learning/)：自定义环境/奖励、动作映射及状态特征约束。
- [CCXT Manual](https://github.com/ccxt/ccxt/wiki/manual)：统一接口、能力差异、限流和测试环境需逐平台处理。
- 其余 GitHub 原始项目见第 3 节。未验证新闻源实际可用性、实盘账户、RL 包安装/训练耗时或任何收益改善；这些均是后续验收内容。
