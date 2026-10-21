# QuantPilot MVP2 验收记录

验收日期：2026-10-15。工作目录 `D:\QuantPilot`，分支 `feat/mvp2`。此记录随实际验收更新；尚未记录的项目不视为通过。MVP1 标签与历史实验保留。

## 已核实的公共数据和新闻链路

- 基线完整回归：124 passed。公共数据新增代码经规格审查和质量审查通过；主代理独立运行 `pytest tests/test_public_data.py -q`：18 passed，保留一个已有 Starlette/httpx 弃用警告。
- 实际 API 队列调用 Binance、OKX、Kraken，三家均返回 BTC/USDT、ETH/USDT ticker 和各 48 根小时 K 线。仅证明公开数据接入；不代表回测、模拟或实盘资格。完整缓存快照见 [exchanges.json](evidence/mvp2/exchanges.json)。
- 固定源 RSS 经过公共 DNS 解析固定、TLS 验证、响应限制及 XML 解析：美联储 20 条，欧洲央行 15 条。CoinDesk 在当前加固连接方式下超时；健康状态为 unavailable，不替换成示例文章。
- 真实新闻版本 `8686e220-184e-4a00-ba42-e76ee249dcb3`：原发布时间 2026-09-30，首次可知时间 2026-10-15。执行本地关键词分析成功，来源标为 local；没有调用付费模型。重复创建同一新闻研究返回同一 ID。
- 新闻研究 `d1467bd7-ba39-5a05-a0a8-ffdb1cc5b653` 经确认进入原有实验队列，实验 `74827205-66ba-4ede-8d6e-b45bcdca2995` 成功完成实际 Freqtrade 回测。区间 [2025-04-01, 2025-07-01)，BTC/ETH，60 笔交易；新闻仅作为今天提出假设的背景，**没有被回填成 2025 年特征，也不解释该历史收益**。
- 来源健康、分析元数据、研究关联及实际结果见 [news.json](evidence/mvp2/news.json)。

## 环境与实现约束

- 原 `.venv`：Python 3.13.5、Freqtrade 2026.9、CCXT 4.5.85。
- 独立 `runtime/rl-venv`：PyTorch 2.14.1+cpu、Stable-Baselines3 2.9.0、Gymnasium 1.4.0。安装后张量兼容探针与 CartPole 64 步 PPO 探针成功；该探针只证明运行环境，不属于金融评估。
- RL 限制 CPU 两线程；D 盘提供存储空间，不改变电脑物理内存。
- 没有读取或展示本机密钥；没有启用真实账户、发送真实订单或运行最终保留测试。新闻的外部内容只作为数据，不获得工具或下单能力。

## 强化学习实际实验

- 任务 `rl-1db76077c9504e0995821a29263f3ee7`：2026-10-15 06:27:19–06:35:37 UTC，约 8 分 18 秒。五个固定种子各训练 8192 步，随后运行 14 次实际 Freqtrade 回测（两个区间，各五种子加两个基线）。
- API 重启后 `/api/rl/readiness` 返回 available=true，任务仍为 succeeded，结果返回完整 14 行。证据见 [rl-api.json](evidence/mvp2/rl-api.json)。规格审查独立核对 186 个文件哈希、14 份引擎原始 ZIP 与收益/成交数。
- PPO 五种子净收益中位数：2025Q2 为 **−1.1251%**，2025Q3 为 **−1.8095%**；十个 PPO 结果全部为负，全部保留。本次没有证明 PPO 优于 SMA。两个区间均用于开发，不称为最终盲测。
- 六个观测仅含已闭合价格特征；标准化只拟合 2025Q1。Gym 与 Freqtrade 的终局成交、精度和止损规则存在明确差异；界面以真实 Freqtrade 行作为交易对照，不混用 Gym 权益。RL 只开放研究，不进入模拟或实盘发布。
- 本次采用独立 SB3 训练与 Freqtrade 动作 CSV 回放；不是原生 FreqAI RL learner。现有 FreqAI/LightGBM 流程仍保留，原始 flagged 状态不变。
- 新闻和 RL 集成后的完整主环境回归：`160 passed, 1 skipped, 1 warning`，55.66 秒。跳过的是需独立 RL 环境的环境测试；另用 `runtime/rl-venv/Scripts/python.exe -m pytest tests/test_rl_env.py -q` 实测 4 passed。最后增加的 Windows 就绪探针超时用例也独立通过，确认真实虚拟环境启动器的子进程已退出。

## 实盘控制离线验证

- 新增本地 scrypt 登录、会话/CSRF、Windows DPAPI 交互登记、独立 `live-sma-v1` 历史与连续模拟证据准入、Binance 现货引擎和交易前预算/状态控制。未配置任何实盘账户、没有真实订单。
- 规格审查独立运行 51 项测试通过。真实已安装 Freqtrade Worker、Exchange、策略与 RPC 在替换网络传输的夹具下成功启动；验证配置归一化、启动前 guard、重配置与底层初始/替换/仓位调整入口，以及原生 stoploss 装饰路径。
- 控制状态更新使用事务与操作令牌；引擎就绪绑定进程身份、源文件与启动对账。读任务所有账本提交校验 job/account/deadline。资金历史起点来自实际预检观察点；每日风险基准独立于初始入金，不能用初始本金抵消之后的单日亏损。
- 双向竞态用例验证：旧对账不能覆盖刚发出的订单，新 transport 不能提交已撤销的预留；不同交易对的相同订单编号不会混淆。未来报价、超时未知、部分成交、无发送证明、重复请求、暂停/停止语义均有明确离线覆盖。
- 真实 Binance 权限字段、私有历史分页、实际连续 24 小时模拟准入和真实成交仍未取得证据。持有资产跨 UTC 午夜时，缺少已核实的午夜估值则继续阻止入场；该边界不会通过重置基准绕过。
- 质量审查独立再次运行 51 项通过，无剩余阻断。集成实盘模块后主代理完整回归：`211 passed, 1 skipped, 1 warning`，75.02 秒。服务重启成功，实盘状态仍为未配置认证/未登记账户/未创建部署；PPO 已完成任务仍可读取。公开状态证据见 [live-status.json](evidence/mvp2/live-status.json)。

## 最终集成验收

- 最终主环境完整回归：**212 passed, 1 skipped, 1 warning**，92.87 秒；独立 RL 环境另行运行 **4 passed**，8.81 秒。跳过项由独立环境覆盖，警告为已有 Starlette/httpx 弃用提示。新闻报告关联的 16 项测试及实盘 51 项测试均通过，规格与质量审查均无剩余阻断。
- 前端构建成功（47 个模块），TypeScript 检查通过。浏览器加载最终 `index-7qmPAQN3.js` / `index-DuWbAYV8.css`，版本 **0.5.0-mvp2**；错误控制台为空。
- 实际网页完成三家公开行情刷新、本地新闻分析、关联研究与已完成实验跳转、报告新闻版本核对；结果来自真实 API 与持久化记录。没有调用付费模型。详情见 [ui-acceptance.json](evidence/mvp2/ui-acceptance.json)。
- RL 页面核对五种子摘要、14 行 Freqtrade 对照、逐笔成交与动作 CSV。另通过网页创建任务 `rl-431db111ffc04bbea8e5949e04e1d61a`，运行后取消，最终状态 cancelled，进程核查无该任务残留子进程。演示已有成功结果时，在任务下拉框选择 `rl-1db76077c9504e0995821a29263f3ee7`。
- 整体验收期间一次 RL 环境探针返回 unavailable；直接运行隔离环境兼容探针成功，随后 API 复查 available=true。未确定首次失败原因，不将单次成功描述为所有负载下均可用；界面保留失败状态并提供复查。
- 实盘页面六层准入与默认关闭状态已核对；认证未配置、账户未登记、部署未创建。**这是实现与离线验收完成，不是实际账户、连续 24 小时模拟准入或真实成交验收完成。**
- 桌面 1366×900、窄屏 390×844 页面检查通过；窄屏没有页面横向溢出，测试后恢复默认窗口尺寸。页面截图见 [RL 结果](evidence/mvp2/ui/rl-results.jpg)、[实盘准入](evidence/mvp2/ui/live-gates.jpg)、[窄屏](evidence/mvp2/ui/live-mobile.jpg)。
- 完整使用、启动及双人八分钟答辩路线见 [MVP2 使用手册](MVP2-USER-GUIDE.md)。本地交付分支 `feat/mvp2`，交付标签 `mvp2`；保留原 `mvp1` 标签及原始实验。公共新闻外部超时、负收益和未验证实盘边界均如实保留。
