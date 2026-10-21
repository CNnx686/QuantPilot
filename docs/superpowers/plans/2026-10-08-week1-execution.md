# QuantPilot 第一周执行计划

**目标：** 网页提交真实历史回测，独立 worker 执行，结果持久化并能核对原始引擎产物。

**环境选择：** 用户指定 D 盘开发，项目位于 D:\QuantPilot；Windows 原生 Python 3.13.5 + Node 22.17.1，无 Docker。Freqtrade 2026.9 及 Python 依赖锁定于 requirements.lock，前端依赖锁定于 apps/web/package-lock.json。

## 已明确的实现决定

- 2024-12 至 2025-03 的 BTC/ETH 官方小时归档；2025Q1 为开发验证，不作为后续最终测试集。
- Freqtrade 自身的 timerange 结束边界为包含，本项目 API 为排除；转换为 UTC 结束时间减一秒。
- 预热使用前一月数据，不用未来观测填充。
- 长任务在单独 worker，API 只负责校验和入队；所有任务阶段来自数据库。
- 本机系统代理对 urllib 可用；CCXT 异步连接通过 aiohttp_trust_env 采用相同代理环境。
- 页面不展示假模型、虚构净值或伪造进度。第一周仅规则基线。

## 执行清单

- [x] 环境预检、创建 feat/week1-mvp 分支与独立虚拟环境。
- [x] 下载行情并校验官方 CHECKSUM、连续性、重复及价格范围。
- [x] 开发 SQLite 持久化、输入校验、幂等、独立 worker 与故障恢复。
- [x] 后端规格审查及质量审查，修复数据可用性、幂等重试和中断状态问题。
- [x] 固定规则策略、引擎命令、每次运行数据与代码快照、日志归档。
- [x] 真实引擎烟雾回测完成，核对 57 笔成交及资金一致性。
- [x] 校正时间边界，使用安装版本的实际裁剪函数做回归验证。
- [ ] React 页面、图表、数据溯源和原始产物下载。
- [ ] 通过网页真实创建并完成一次实验，验证刷新后持久化。
- [ ] 复现实验与可见失败路径验证。
- [ ] 最终审查、启动停止验证、验收记录和交付。

## 有效验证命令

```powershell
cd D:\QuantPilot
.venv\Scripts\python.exe -m pytest -q
cd apps\web
npm.cmd run build
cd ..\..
.\start.ps1
```

自动化测试中的合成数据仅验证程序逻辑。真实行情回测和浏览器走查的结论分别记录在验收文档。
