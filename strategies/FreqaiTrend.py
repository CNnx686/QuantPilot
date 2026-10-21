"""Frozen week-two causal features and six-hour forward-return target."""
import numpy as np
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class FreqaiTrend(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    startup_candle_count = 60
    minimal_roi = {}
    stoploss = -0.05
    process_only_new_candles = True
    use_exit_signal = True
    order_types = {"entry": "market", "exit": "market", "stoploss": "market", "stoploss_on_exchange": False}
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    def feature_engineering_standard(self, dataframe: DataFrame, metadata: dict, **kwargs):
        close = dataframe["close"]
        for lag in (1, 6, 24):
            dataframe[f"%-return{lag}"] = close.pct_change(lag, fill_method=None)
        for window in (12, 48):
            dataframe[f"%-mean{window}"] = close / close.rolling(window).mean() - 1
        dataframe["%-volatility24"] = close.pct_change(fill_method=None).rolling(24).std()
        dataframe["%-volume24"] = dataframe["volume"] / dataframe["volume"].rolling(24).mean().replace(0, np.nan)
        dataframe["%-range"] = (dataframe["high"] - dataframe["low"]) / close
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs):
        dataframe["&-return6"] = dataframe["close"].shift(-6) / dataframe["close"] - 1
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict):
        return self.freqai.start(dataframe, metadata, self)

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict):
        valid = (dataframe["do_predict"] == 1) & np.isfinite(dataframe["&-return6"]) & (dataframe["volume"] > 0)
        dataframe.loc[valid & (dataframe["&-return6"] > 0.003), ["enter_long", "enter_tag"]] = (1, "lgbm_return6_gt003")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict):
        valid = (dataframe["do_predict"] == 1) & np.isfinite(dataframe["&-return6"]) & (dataframe["volume"] > 0)
        dataframe.loc[valid & (dataframe["&-return6"] < 0), ["exit_long", "exit_tag"]] = (1, "lgbm_return6_negative")
        return dataframe
