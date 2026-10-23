# 真实终端主流程实施计划

> 使用 writing-plans 与 subagent-driven-development，将已批准路线拆为互相隔离的文件任务，进行需求复核和代码质量复核。本次用户已授权实施与技术决策，无需再次批准原型。

**目标：** 用户通过新终端使用真实公开数据，创建规则、RL、预测策略，运行真实训练/回测，手工选定冻结候选，启动独立虚拟账户并由当前模型处理已闭合小时线。

**架构：** 复用现有 FastAPI、Binance 数据、LightGBM、隔离 SB3 环境；新终端静态文件挂载 `/terminal/`。新 SQLite 工作流隔离旧实验和部署，模型/数据产物保存新目录。现有 Freqtrade/FreqAI 研究入口保留；本次新流程明确采用 `local_paper_v1` 回测和本机模拟执行，不称为 Freqtrade dry-run，也不拼接两种引擎的收益。

**已有证据：** 主环境 Python 3.13.5、Freqtrade 2026.9、LightGBM 4.7.0。旧部署只支持单 SMA；旧 RL 为历史 CSV replay；旧 FreqAI live 路径有显式限制。不能仅把旧发布按钮搬到新界面。

## T1 真实行情与数据快照（root）

文件：`quantpilot/terminal_market.py`、`terminal_market_api.py`、`tests/test_terminal_market.py`。

- [x] Binance 公开现货目录、ticker 与请求 256 根小时线后保留的闭合数据；排除未闭合线，明确 stale/error/observed_at，失败不退回合成值。
- [x] 历史优先复用并核验原始快照哈希；新品种下载官方月度 ZIP 和 CHECKSUM。保留预热、UTC、缺失与OHLC质量检查。
- [x] 开发窗口限制至 2025-10-01 前，继续保留 Q4；单任务限制范围和下载量。
- [x] 接口 `prepare_dataset(config, directory) -> DataFrame`、`snapshot(symbol, refresh=False) -> dict`、`closed_frame(snapshot) -> DataFrame`。
- [x] 测试未闭合线过滤、时间过期、断线失败保留旧缓存但禁止执行、历史哈希/缺口拒绝。

## T2 冻结训练与当前推理（engine）

文件：`terminal_engine.py`、`terminal_rl.py` 或隔离训练/推理入口、`tests/test_terminal_engine.py`。

```python
candidate = train(config, output_dir, candles)
decision = predict(output_dir, current_closed_candles)
assert decision['feature_available_at'] <= decision['decision_at']
```

- [x] rule/LightGBM/PPO 实际训练及统一公开费用口径回测，train/evaluation 按时间分离，预测标签跨拟合切点剔除。
- [x] 保存权重、归一化、特征顺序、配置、模型/数据/代码哈希、版本与实际指标；每次训练保留候选，不自动挑最高收益。
- [x] 当前 PPO 使用真实 `PPO.load().predict()`，绝不消费旧动作CSV。
- [x] 非法/不完整输入与产物哈希改变必须拒绝；运行真实小规模三类实验并记录结果。

## T3 服务端状态、任务与虚拟账户（backend）

文件：`terminal_store.py`、`terminal_api.py`、`terminal_worker.py`、`terminal_job.py` 及对应测试。

- [x] 独立 `terminal.sqlite3` 持久化策略/版本、任务、候选、实例、订单、决策；任务冻结参数。
- [x] 创建/编辑/训练/选模型/启动接口带服务端校验、版本冲突和任务幂等；有历史实例不原地变更交易配置。
- [x] 单训练子进程队列，状态 queued/running/succeeded/failed/cancelled；失败不标成功，重启明确恢复。
- [x] 独立资金、费用、持仓，选模型不自动替换运行引用；负收益可启动，产物/数据/兼容性必须通过。
- [x] 当前公开行情驱动模拟，每根已闭合线决策去重；暂停只禁开仓；显式平仓；有仓停止等待或明确平仓。
- [x] 数据故障保留诊断并阻断新执行，不能伪造价格。模型当前推理和本机模拟记录可追溯。

核心 API：`GET /api/terminal/bootstrap`；`POST /strategies`；`PATCH /strategies/{id}`；`POST /strategies/{id}/runs`；`GET /jobs/{id}`；`POST /jobs/{id}/cancel`；`POST /strategies/{id}/select`；`POST /instances`；`POST /instances/{id}/action`。

配置：`name,kind,symbol,timeframe,start,end,wallet,stake,fee,parameters,news=false`。参数含 fast/slow/horizon/entry_threshold/rl_steps/seed/stop_loss。

## T4 已批准终端接真实 API（frontend）

文件：`apps/terminal/index.html`、`styles.css`、`app.js`；root 改 `api.py` 挂载与生命周期。

- [x] 复用布局，删除合成行情、虚构任务进度与本地账户；API 是业务状态唯一来源。
- [x] 三类模板创建、真实任务/错误、候选比较/选用、独立模拟实例与真实行情图。
- [x] 已闭合线、报价时间、源和状态明确展示；无数据就是空态/故障，不能回退示例。
- [x] 本轮新闻训练因子与 DeepSeek 尚未接入，应禁用并说明；保持已批准完整目标在后续计划中，不把禁用视为完成。
- [x] 保留 `/` 旧应用和 8770 原型；本轮不接真实资金。

## T5 端到端验收与交付（root + 独立复核）

- [x] 定向单元测试后运行原项目回归，验证没有破坏旧功能。
- [x] 真实 Binance 历史→三类训练→候选→当前推理→本机模拟；包括负收益、未成交、重复同线与故障路径。
- [x] 浏览器新建到运行操作、刷新保留、不同实例资金隔离；截图和真实任务/候选/实例 ID。
- [x] 保存实际通过/失败/未验证项，更新使用文档，提交可回退版本。

本轮不宣称生产实盘可靠、新闻因子收益、完整后台可用性 SLA 或所有四周需求已完成。新执行器实际成交规则、手续费、滑点和限制必须随着实现记录。

验收证据与未覆盖的完整 V1 需求见 [实际验收记录](live-workflow-verification.md)。上述勾选表示本批限定实施任务完成，不代表新闻因子、在线升级或全部四周功能完成。
