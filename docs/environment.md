# 开发环境记录

- 主目录：D:\QuantPilot（用户指定）；C:\Users\20705\QuantPilot 为迁移前备份。
- 系统：Windows 11 x64。
- CPU：20 个逻辑处理器；物理内存约 15.7 GiB。
- 检查时磁盘：D 盘约 1 TB 可用，C 盘约 24 GB 可用。磁盘空间与运行内存不同。
- Python：3.13.5，独立环境 D:\QuantPilot\.venv。
- Node：22.17.1。
- Freqtrade：2026.9，CCXT：4.5.85；依赖完整锁定 requirements.lock。
- 无 Docker，当前使用 Windows 原生 Python 运行，不要求额外安装 Docker/WSL。
- 模型：第一周无训练模型，采用固定 SmaTrend 规则策略。
- 网络：Binance 官方行情归档可下载，CCXT 需要遵循本机系统代理环境，已启用 aiohttp_trust_env。

后续机器学习阶段会再评估内存和训练时长。本周数据规模很小，不需要 GPU。
