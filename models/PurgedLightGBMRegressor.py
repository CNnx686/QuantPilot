"""Audited historical-only model; fitted preprocessing is owned by FreqAI."""
import csv
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor
from freqtrade.freqai.base_models.BaseRegressionModel import BaseRegressionModel


def purge_training_frame(frame, cutoff, horizon=6):
    """Dates are candle OPEN times; a horizon-h label is known at t+h+1 hours."""
    endpoint = pd.to_datetime(frame["date"], utc=True) + pd.Timedelta(hours=horizon + 1)
    return frame.loc[endpoint <= pd.Timestamp(cutoff)].copy()


class PurgedLightGBMRegressor(BaseRegressionModel):
    def train(self, unfiltered_df, pair, dk, **kwargs):
        if self.live:
            raise ValueError("Week-two model is authorized for historical backtests only")
        started = time.perf_counter()
        cutoff = self.training_timerange.stopdt
        clean = purge_training_frame(unfiltered_df, cutoff)
        model = super().train(clean, pair, dk, **kwargs)
        dates = pd.to_datetime(dk.train_dates, utc=True)
        label_max = dates.max() + pd.Timedelta(hours=7)
        if label_max > pd.Timestamp(cutoff):
            raise ValueError("Training labels cross fitting cutoff")
        audit = {"pair": pair, "model_id": dk.model_filename, "fit_cutoff": pd.Timestamp(cutoff).isoformat(),
                 "training_start": pd.Timestamp(self.training_timerange.startdt).isoformat(),
                 "model_label_max_at": label_max.isoformat(), "input_rows": len(unfiltered_df),
                 "purged_rows": len(unfiltered_df) - len(clean), "training_rows": len(dates),
                 "features": list(dk.training_features_list), "duration_seconds": time.perf_counter() - started,
                 "preprocessing": "FreqAI pipelines fit only on purged training data; test_size=0",
                 "fit_time_semantics": "Idealized historical cutoff; actual training duration reported separately"}
        dk.data["quantpilot_audit"] = audit
        path = Path(self.config["user_data_dir"]).parent / "training-audit.json"
        audits = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        audits.append(audit)
        path.write_text(json.dumps(audits, indent=2, allow_nan=False), encoding="utf-8")
        return model

    def fit(self, data_dictionary, dk, **kwargs):
        model = LGBMRegressor(**self.model_training_parameters)
        model.fit(data_dictionary["train_features"], data_dictionary["train_labels"].iloc[:, 0],
                  sample_weight=data_dictionary["train_weights"])
        return model

    def predict(self, unfiltered_df, dk, **kwargs):
        predictions, validity = super().predict(unfiltered_df, dk, **kwargs)
        # Installed BaseRegressionModel replaces filter_features validity with pipeline outliers.
        # Reapply raw finite-feature validity explicitly so filled NaNs cannot become signals.
        finite_features = np.isfinite(unfiltered_df[dk.training_features_list].to_numpy(dtype=float)).all(axis=1)
        valid = (validity == 1) & finite_features & np.isfinite(predictions.to_numpy()).all(axis=1)
        validity = np.where(valid, 1, 0)
        dk.do_predict = validity
        audit = dk.data["quantpilot_audit"]
        path = Path(self.config["user_data_dir"]).parent / "predictions.csv"
        fields = ["pair", "model_id", "decision_at", "feature_available_at", "model_fit_at", "model_label_max_at", "prediction", "do_predict"]
        exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            if not exists:
                writer.writeheader()
            for date, pred, usable in zip(unfiltered_df["date"], predictions.iloc[:, 0], validity, strict=True):
                decision = (pd.Timestamp(date) + pd.Timedelta(hours=1)).isoformat()
                writer.writerow({"pair": dk.pair, "model_id": audit["model_id"], "decision_at": decision,
                                 "feature_available_at": decision, "model_fit_at": audit["fit_cutoff"],
                                 "model_label_max_at": audit["model_label_max_at"],
                                 "prediction": float(pred) if np.isfinite(pred) else "", "do_predict": int(usable)})
        return predictions, validity
