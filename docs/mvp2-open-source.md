# MVP2 开源复用记录

核对日期：2026-10-15。安装版本以锁文件和本机包元数据为准；仓库默认分支仅用于参考，不作为自动升级目标。

| 项目 | 本机版本 | 在 QuantPilot 中的用途 | 包元数据许可 |
|---|---|---|---|
| [Freqtrade / FreqAI](https://github.com/freqtrade/freqtrade) | 2026.9 | 历史撮合、LightGBM 研究、模拟执行；实盘接入继续复用其订单生命周期 | GPLv3 |
| [CCXT](https://github.com/ccxt/ccxt) | 4.5.85 | Binance、OKX、Kraken 公开行情与市场规格；执行账户由 Freqtrade 单独管理 | MIT |
| [Stable-Baselines3](https://github.com/DLR-RM/stable-baselines3) | 2.9.0 | 独立 CPU 环境中的 PPO 训练 | MIT |
| [Gymnasium](https://github.com/Farama-Foundation/Gymnasium) | 1.4.0 | 项目自定义现货交易研究环境 | MIT |
| [PyTorch](https://github.com/pytorch/pytorch) | 2.14.1+cpu | PPO 张量计算；保留完整上游包中的第三方许可声明 | 包含 Apache-2.0、BSD、MIT 等，详见安装包 |

[FinRL](https://github.com/AI4Finance-Foundation/FinRL) 用于参考金融环境与训练/评估分离思路；[FinGPT](https://github.com/AI4Finance-Foundation/FinGPT) 用于参考金融新闻抽取任务。没有安装或调用这两个项目，不能在答辩中将其列为已集成引擎。RSSHub、Qlib、RD-Agent 同样没有接入。

本项目实现研究配置与证据绑定、任务持久化、新闻版本与可用时间、数据源健康状态、PPO 实验协议、跨环境动作回放、发布与执行控制及中文界面。底层 PPO 算法和 Freqtrade 撮合不属于本项目原创。

新闻只存标题、必要短摘要、源站链接与指纹；不复制全文。行情、模型、账户配置和历史数据库不进入 Git。对外分发时应保留实际随包附带的许可及声明，不能将所有依赖统一标注为 MIT。
