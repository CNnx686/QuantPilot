"""Version-bound comparison domain; no strategy, predictions or trade checks changed.

Freqtrade 2026.9 loads candles through stopdt (inclusive), while FreqAI predicts
strictly before stopdt. The extra execution candle's zero-filled indicators are
outside the prediction domain. Keep it for trade checks, exclude only that exact
endpoint from indicator comparison, and retain every raw difference on disk.
"""
import copy
import importlib.metadata
import json
from pathlib import Path

import pandas as pd


def comparison_frames(full, cut, end):
    end = pd.Timestamp(end)
    if not cut.index.is_unique or not full.index.is_unique or cut.empty:
        raise ValueError('Invalid comparison indices or empty data')
    if (cut.date > end).any():
        raise ValueError('Unexpected rows beyond exclusive prediction endpoint')
    reference = full.loc[cut.index]
    if not reference.date.equals(cut.date):
        raise ValueError('Comparison timestamps do not match')
    comparable = cut.loc[cut.date < end]
    if comparable.empty:
        raise ValueError('Empty prediction comparison domain')
    return reference.loc[comparable.index], comparable


def main():
    if importlib.metadata.version('freqtrade') != '2026.9':
        raise RuntimeError('Revalidate comparison adapter for this Freqtrade version')
    from freqtrade.optimize.analysis.lookahead import LookaheadAnalysis
    original = LookaheadAnalysis.analyze_indicators

    def analyze(self, full_vars, cut_vars, pair):
        if not self.local_config.get('freqai', {}).get('enabled') or self.local_config['timeframe'] != '1h':
            raise ValueError('Adapter is limited to hourly FreqAI diagnostics')
        if pd.Timestamp(cut_vars.to_dt) - pd.Timestamp(cut_vars.compared_dt) != pd.Timedelta(hours=1):
            raise ValueError('Unexpected upstream execution-candle boundary')
        full, cut = full_vars.indicators[pair], cut_vars.indicators[pair]
        left, right = comparison_frames(full, cut, cut_vars.to_dt)
        raw = full.loc[cut.index].compare(cut)
        remaining = left.compare(right)
        evidence = {'pair': pair, 'compared_at': str(cut_vars.compared_dt),
                    'exclusive_prediction_end': str(cut_vars.to_dt),
                    'compared_rows': len(right), 'excluded_endpoint_rows': len(cut)-len(right),
                    'raw_difference_dates': [str(x) for x in cut.loc[raw.index, 'date']],
                    'raw_difference_columns': list(dict.fromkeys(raw.columns.get_level_values(0))),
                    'raw_differences': json.loads(raw.to_json(orient='split')),
                    'in_window_difference_rows': len(remaining)}
        destination = Path(self.local_config['user_data_dir']).parent / 'comparison-boundaries.jsonl'
        with destination.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(evidence, allow_nan=False) + '\n')
        full_copy, cut_copy = copy.copy(full_vars), copy.copy(cut_vars)
        full_copy.indicators, cut_copy.indicators = {pair: left}, {pair: right}
        # All columns still reach the original detector. Entry/exit replay checks
        # already ran with the unmodified frames in upstream analyze_row().
        return original(self, full_copy, cut_copy, pair)

    LookaheadAnalysis.analyze_indicators = analyze
    from freqtrade.main import main as freqtrade_main
    freqtrade_main()


if __name__ == '__main__':
    main()
