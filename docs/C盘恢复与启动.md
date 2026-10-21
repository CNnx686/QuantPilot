# QuantPilot：C 盘恢复版本

项目位置：`C:\Users\20705\QuantPilot-Restored`。

本版本从 C 盘幸存的早期文件及开发会话中的代码片段重建。它不是原 D 盘的完整副本，也不包含原始数据库、模型、实盘凭据或完整 Git 历史。原始证据另存于 `C:\Users\20705\QuantPilot-Recovery-20261021`，未被本项目覆盖。

本次使用新数据库验收。若以后找回旧 `public.sqlite`，不要直接覆盖：旧新闻表的唯一约束需要单独迁移，当前恢复流程未验证该升级路径。

## 启动与停止

双击项目根目录的 `launch.cmd`，或在 PowerShell 中执行：

```powershell
Set-Location C:\Users\20705\QuantPilot-Restored
.\launch.cmd
```

打开 <http://127.0.0.1:8765/>。停止时使用根目录的 `stop.cmd`。研究和运行状态保存在本项目的 `data`、`artifacts`、`runtime` 目录，不能只复制前端网页作为备份。

## 环境重建

本机已安装 Python 3.13 和 Node.js 22。主环境安装 Freqtrade/FreqAI；强化学习使用独立环境，避免不同版本依赖相互覆盖。

```powershell
Set-Location C:\Users\20705\QuantPilot-Restored
powershell -ExecutionPolicy Bypass -File .\setup.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\setup-rl.ps1
.\.venv\Scripts\python.exe -m quantpilot.data --week2
```

行情来自 Binance 公开历史月度压缩包，包含 BTC/USDT、ETH/USDT 的小时线。下载器校验公开校验和，数据清单保留来源与哈希。范围为 2023-12 至 2025-09；2025 年第四季度保留集没有下载。本次重建数据及新实验均不应被描述为从原盘恢复的原始实验记录。

## 业务流程与边界

从研究配置开始，选择本地模板或可选的大模型建议；人工确认规格后创建实验。回测、比较和诊断用于形成审阅证据。模拟交易使用虚拟资金，实盘控制有独立的账户和审核门槛。PPO 实验展示多个固定随机种子的结果，不自动挑选盈利种子或发布为实盘策略。新闻是可追溯的研究上下文，不能直接触发真实下单。

恢复版本不携带旧密钥。需要在线 AI 时，将 `.env.example` 复制为 `.env`，在本机填写有效模型名称和新密钥，再重启服务。不配置时仍可使用本地研究模板。请勿把 `.env` 上传到 Git 或发送到聊天。

历史文档中可能出现旧 D 盘路径和旧实验编号，它们属于恢复线索。本版本的实际路径、运行证据和限制以本说明及恢复验收记录为准。

## 备份

同一台机器 C 盘上的归档只能防止部分误操作，不能防止整块磁盘损坏。后续有外置盘或可信远端存储时，应再保存一份独立副本。依赖环境可以重新安装；源码、文档、数据清单、实验输出和数据库需要保留。
