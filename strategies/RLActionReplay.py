"""Research-only causal CSV replay. Never added to deployment/legacy strategy allowlists."""
from pathlib import Path
import pandas as pd
from freqtrade.strategy import IStrategy


class RLActionReplay(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = '1h'
    can_short = False
    startup_candle_count = 60
    minimal_roi = {}
    stoploss = -0.05
    process_only_new_candles = True
    use_exit_signal = True
    order_types = {'entry': 'market', 'exit': 'market', 'stoploss': 'market', 'stoploss_on_exchange': False}
    order_time_in_force = {'entry': 'GTC', 'exit': 'GTC'}

    def populate_indicators(self, dataframe, metadata):
        if metadata['pair'] != 'BTC/USDT':
            raise ValueError('RL replay is BTC/USDT only')
        actions = pd.read_csv(Path(__file__).parent / 'actions.csv')
        times = pd.to_datetime(actions['candle_at'], utc=True)
        decision = pd.to_datetime(actions['decision_at'], utc=True)
        available = pd.to_datetime(actions['feature_available_at'], utc=True)
        fit = pd.to_datetime(actions['model_fit_at'], utc=True)
        label = pd.to_datetime(actions['model_label_max_at'], utc=True)
        if times.duplicated().any() or not actions['requested_action'].isin([0, 1, 2]).all():
            raise ValueError('Invalid or duplicate action rows')
        if not ((decision == times + pd.Timedelta(hours=1)) & (available <= decision) & (label <= fit) & (fit <= decision)).all():
            raise ValueError('Action timing failed causality check')
        mapping = pd.Series(actions['requested_action'].to_numpy(), index=times)
        dataframe['rl_action'] = dataframe['date'].map(mapping).fillna(0).astype(int)
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe.loc[(dataframe['rl_action'] == 1) & (dataframe['volume'] > 0), ['enter_long', 'enter_tag']] = (1, 'frozen_ppo_action')
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dataframe.loc[dataframe['rl_action'] == 2, ['exit_long', 'exit_tag']] = (1, 'frozen_ppo_action')
        return dataframe
