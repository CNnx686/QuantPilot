"""Fixed educational baseline. No parameter search and no future-looking features."""
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class SmaTrend(IStrategy):
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

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma_fast"] = dataframe["close"].rolling(12).mean()
        dataframe["sma_slow"] = dataframe["close"].rolling(48).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cross = (dataframe["sma_fast"] > dataframe["sma_slow"]) & (dataframe["sma_fast"].shift(1) <= dataframe["sma_slow"].shift(1))
        dataframe.loc[cross & (dataframe["volume"] > 0), ["enter_long", "enter_tag"]] = (1, "sma12_cross_above_sma48")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cross = (dataframe["sma_fast"] < dataframe["sma_slow"]) & (dataframe["sma_fast"].shift(1) >= dataframe["sma_slow"].shift(1))
        dataframe.loc[cross & (dataframe["volume"] > 0), ["exit_long", "exit_tag"]] = (1, "sma12_cross_below_sma48")
        return dataframe
