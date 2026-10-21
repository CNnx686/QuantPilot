"""Fixed 1000 USDT first-entry hold comparator, not an all-wallet benchmark."""
from freqtrade.strategy import IStrategy


class RLResearchBuyHold(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = '1h'
    can_short = False
    startup_candle_count = 60
    minimal_roi = {}
    stoploss = -0.05
    use_exit_signal = True
    order_types = {'entry': 'market', 'exit': 'market', 'stoploss': 'market', 'stoploss_on_exchange': False}
    order_time_in_force = {'entry': 'GTC', 'exit': 'GTC'}

    def populate_indicators(self, dataframe, metadata):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe.loc[(dataframe['date'] == self.config['rl_research_start']) & (dataframe['volume'] > 0), 'enter_long'] = 1
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dataframe['exit_long'] = 0
        return dataframe
