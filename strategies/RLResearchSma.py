"""Same sizing/cost/risk configuration as RLActionReplay; fixed 12/48 SMA crossover."""
from freqtrade.strategy import IStrategy


class RLResearchSma(IStrategy):
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
        dataframe['fast'] = dataframe['close'].rolling(12).mean()
        dataframe['slow'] = dataframe['close'].rolling(48).mean()
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        signal = (dataframe['fast'] > dataframe['slow']) & (dataframe['fast'].shift(1) <= dataframe['slow'].shift(1))
        dataframe.loc[signal & (dataframe['date'] >= self.config['rl_research_start']) & (dataframe['volume'] > 0), 'enter_long'] = 1
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        signal = (dataframe['fast'] < dataframe['slow']) & (dataframe['fast'].shift(1) >= dataframe['slow'].shift(1))
        dataframe.loc[signal, 'exit_long'] = 1
        return dataframe
