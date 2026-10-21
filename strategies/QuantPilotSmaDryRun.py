"""Observation-only subclass of the frozen SMA baseline. Never places or changes orders."""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from SmaTrend import SmaTrend


class QuantPilotSmaDryRun(SmaTrend):
    def populate_exit_trend(self, dataframe, metadata):
        result = super().populate_exit_trend(dataframe, metadata)
        try:
            if result.empty:
                return result
            row = result.iloc[-1]
            candle = row['date'].to_pydatetime()
            if candle.tzinfo is None:
                candle = candle.replace(tzinfo=timezone.utc)
            key = (metadata['pair'], candle.isoformat())
            seen = getattr(self, '_qp_observed', {})
            if seen.get(key[0]) == key[1]:
                return result
            def finite(value):
                value = float(value)
                return value if math.isfinite(value) else None
            fast, slow = finite(row['sma_fast']), finite(row['sma_slow'])
            record = {'pair': metadata['pair'], 'candle_open_at': candle.isoformat(),
                      'available_at': (candle + timedelta(hours=1)).isoformat(),
                      'observed_at': datetime.now(timezone.utc).isoformat(),
                      'sma_fast': fast, 'sma_slow': slow, 'signal_valid': fast is not None and slow is not None,
                      'enter_long': finite(row.get('enter_long', 0)) == 1,
                      'exit_long': finite(row.get('exit_long', 0)) == 1,
                      'risk_state': None, 'risk_evidence': 'not_recorded',
                      'order_reference': None, 'order_evidence': 'not_recorded'}
            root = Path(self.config['user_data_dir']).parent
            manifest = json.loads((root / 'snapshot.json').read_text(encoding='utf-8'))
            record['versions'] = manifest
            with (root / 'decisions.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, allow_nan=False) + '\n')
            seen[key[0]] = key[1]
            self._qp_observed = seen
        except Exception:
            # Observability failures must never change the strategy result or its orders.
            pass
        return result
