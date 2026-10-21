# QuantPilot 第三周实施计划

> 使用 subagent-driven-development 逐模块实施，先规格审查再质量审查。沿用 D 盘工作目录，分支 feat/week3-research-simulation。

**Goal:** 研究配置确认 → 实验 → 不可变发布审核 → Freqtrade dry-run → 决策证据与历史回放。

**Architecture:** React/FastAPI/SQLite 保持现状，新增 research、release、deployment 模块；模拟运行使用固定版本 Freqtrade 独立进程与私有 localhost RPC。历史回放只读现有成交，不向引擎发单。

**Tech Stack:** Python 3.13、Freqtrade 2026.9、Pydantic、SQLite、React/TypeScript。

## 范围与预先固定的审核规则

仅小时现货 BTC/ETH、虚拟资金。大模型未配置时只提供明确标注的本地模板建议，不宣称在线 AI；配置 schema 和手动编辑路径始终可用。接口密钥不展示、不提交 Git，不调用收费服务。

发布策略仅用于教学模拟：成功实验、真实产物哈希一致、至少 30 笔、至少 60 天、账户回撤不超过 10%、资金核对、匹配源实验的前视抽查通过且至少 20 信号、运行策略源码与审核快照一致。规则在第三周实施前冻结，已知第二周结果，因此不是盲测选型或盈利认证。最终测试仍保留，独立列为未验证而非通过。ML 原始 flagged 阻止发布；当前 PurgedLightGBMRegressor 明确拒绝 live，因此不能直接用于 dry-run，必须显示不支持而非降级为 SMA。

## A. 研究配置与审核（后端）

- [ ] 新建 quantpilot/research.py、quantpilot/releases.py、quantpilot/workflow_api.py、tests/test_workflow.py；必要时扩展 store.py，API 在 SPA fallback 前注册路由。
- [ ] ResearchSpec 固定模板、交易对、日期、资金、费率与数据 ID，拒绝未知字段/代码、非有限值及最终测试区间；保存假设、来源 local_template/manual、版本和确认状态。
- [ ] 确认配置后原子关联实验；相同确认重试返回同一实验，不因服务重启重复排队；已确认记录不可改写。
- [ ] 审核读取成功实验和磁盘证据，保存检查明细、原始告警、策略/配置/数据/结果哈希，不改变原实验。缺失证据为 insufficient，失败为 rejected；仅全部必需检查通过才 approved。
- [ ] 测试非法配置、幂等确认、篡改产物、样本不足、ML flagged 与通过的合成测试夹具（明确测试用）。运行真实已有实验的通过/驳回路径。

## B. 模拟运行与历史证据（后端）

- [ ] 新建 quantpilot/deployments.py、quantpilot/replay.py、strategies/QuantPilotSmaDryRun.py、tests/test_deployments.py、tests/test_replay.py；扩展 workflow_api.py 和 service.py。
- [ ] 仅 approved 且哈希复核通过的 SmaTrend 可启动；独立目录、数据库、随机 RPC 凭据与 loopback 端口，dry_run=True、无交易所密钥、禁 force-entry，清除 FREQTRADE__ 环境覆盖。
- [ ] 持久化启动/运行/暂停开仓/停止/失败状态；校验 PID 创建时间及命令，防止误控其他进程；失败网络不能显示为运行。暂停使用上游 stopentry，已有仓位继续管理；停止进程明确不平仓。
- [ ] 余额/持仓/成交来自真实 RPC；记录数据时间、均线、信号、策略版本的实际决策证据，未记录风控或订单关联时明确 unknown；不猜测成交原因。
- [ ] 历史回放从已有成交与可匹配 ML 预测构建有序只读事件；标注来源、原始时间、缺少的当时风控证据。
- [ ] 验证模式锁、拒绝未审核与 ML、重复启动、暂停/停止、进程退出与重启恢复、RPC 不可用；执行本机真实 bounded dry-run 并留日志，不强制制造成交。

## C. 中文交互

- [ ] 新建 Research、Releases、Deployments、Replay 组件并接入 App；保持现有设计与实验入口。
- [ ] 可操作配置编辑与确认、审核详情与拒绝理由、模拟状态与控制、原始决策证据、历史回放播放/暂停/倍速；明确三种模式。
- [ ] 生产构建，浏览器完整配置确认、审核通过/拒绝、模拟状态及回放；无在线 LLM 时显示本地模板。

## D. 验收与交付

- [ ] 57 项既有测试为基线；运行新增针对业务边界的测试与完整回归、前端构建，重启检查持久化。
- [ ] 更新 README、progress、demo-script、acceptance-week3；记录实验/审核/部署 IDs、命令、真实观测和限制。
- [ ] 规格和质量审查均通过后提交本地版本。未完成的外部依赖与检查明确报告，不伪造成功。

验证命令：`.venv/Scripts/python.exe -m pytest -q`；`npm.cmd run build`（apps/web）；真实服务 `start.ps1` / `stop.ps1`，浏览器 http://127.0.0.1:8765/。
