"""Research-only leakage-aware price/news forecast blending on frozen artifacts.

This does not register a model for paper trading or change legacy model packages.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor
from .terminal_features import FEATURES
from .terminal_news_features import NEWS_FEATURES
from .terminal_engine import HOUR, _prediction_actions, evaluate
from .terminal_metrics import prediction_metrics, risk_metrics

WEIGHTS = (0.0, .25, .5, 1.0)


def fit_and_select(values, labels, times, cutoff, horizon, seed=7):
    """Fit two models; select blend using only purged in-training validation labels."""
    ts = pd.to_datetime(times, utc=True)
    cutoff = pd.Timestamp(cutoff)
    if cutoff.tzinfo is None: raise ValueError('Cutoff must be timezone aware')
    if len(values) != len(labels) or len(labels) != len(ts): raise ValueError('Inconsistent feature lengths')
    if ts.duplicated().any() or not ts.is_monotonic_increasing: raise ValueError('Times must be ordered and unique')
    if list(values.columns) != FEATURES + NEWS_FEATURES or not np.isfinite(values.to_numpy()).all(): raise ValueError('Invalid ordered model features')
    train_idx = np.flatnonzero(ts < cutoff)
    if len(train_idx) < 80: raise ValueError('Insufficient training history')
    split_index = int(len(train_idx) * .75)
    split = ts[train_idx[split_index]]
    available = ts + (horizon + 1) * HOUR
    label_values = np.asarray(labels,dtype=float)
    initial = (ts < split) & (available <= split) & np.isfinite(label_values)
    valid = (ts >= split) & (ts < cutoff) & (available <= cutoff) & np.isfinite(label_values)
    all_train = (ts < cutoff) & (available <= cutoff) & np.isfinite(label_values)
    if initial.sum() < 40 or valid.sum() < 24: raise ValueError('Insufficient purged calibration samples')
    def fit(mask, cols):
        model=LGBMRegressor(n_estimators=100,num_leaves=15,max_depth=5,min_child_samples=40,learning_rate=.05,
                            random_state=seed,n_jobs=2,deterministic=True,force_col_wise=True,verbosity=-1)
        model.fit(values.loc[mask,cols], label_values[mask]); return model
    price=fit(initial,FEATURES); news=fit(initial,FEATURES+NEWS_FEATURES)
    a=price.predict(values.loc[valid,FEATURES]); b=news.predict(values.loc[valid,FEATURES+NEWS_FEATURES]); y=label_values[valid]
    scores=[{'weight':w,'rmse':float(np.sqrt(np.mean(((1-w)*a+w*b-y)**2)))} for w in WEIGHTS]
    chosen=min(scores,key=lambda x:(x['rmse'],x['weight']))['weight']
    return {'price':fit(all_train,FEATURES),'news':fit(all_train,FEATURES+NEWS_FEATURES),'weight':chosen,
            'audit':{'validation_start':split.isoformat(),'training_cutoff':cutoff.isoformat(),
            'initial_training_rows':int(initial.sum()),'validation_rows':int(valid.sum()),'refit_training_rows':int(all_train.sum()),
            'initial_label_max':max(available[initial]).isoformat(),'validation_label_max':max(available[valid]).isoformat(),
            'refit_label_max':max(available[all_train]).isoformat(),'weight_scores':scores,'selected_weight':chosen,
            'validation_predictions':[{'candle_at':at.isoformat(),'actual':float(z),'price':float(x),'news':float(q)} for at,z,x,q in zip(ts[valid],y,a,b)]}}


def predict(fitted, values, weight=None):
    w=fitted['weight'] if weight is None else weight
    if w not in WEIGHTS: raise ValueError('Unapproved blend weight')
    x=fitted['price'].predict(values[FEATURES]); y=fitted['news'].predict(values[FEATURES+NEWS_FEATURES])
    return (1-w)*x+w*y


def research(model_dir):
    """Run all predeclared arms on one previously frozen genuine news data package."""
    p=Path(model_dir)
    manifest=json.loads((p/'manifest.json').read_text(encoding='utf-8'))
    if manifest['kind'] != 'prediction' or manifest.get('news',{}).get('mode') != 'historical_archive_assumption': raise ValueError('Requires historical news prediction candidate')
    if not manifest['data_provenance']['source_verified']: raise ValueError('Verified market source required')
    for name in ('snapshot.feather','feature-matrix.feather','news-feature-audit.json','news-snapshot.json','evaluation.json'):
        if hashlib.sha256((p/name).read_bytes()).hexdigest()!=manifest['files'][name]: raise ValueError('Artifact hash mismatch: '+name)
    candles=pd.read_feather(p/'snapshot.feather'); candles['date']=pd.to_datetime(candles['date'],utc=True)
    matrix=pd.read_feather(p/'feature-matrix.feather'); matrix['date']=pd.to_datetime(matrix['date'],utc=True)
    aligned=matrix.merge(candles[['date','open','high','low','close','volume']],on='date',validate='one_to_one',how='left')
    if aligned['close'].isna().any() or len(aligned)!=len(matrix): raise ValueError('Snapshot mismatch')
    config=manifest['effective_config']; period=manifest['period']; cutoff=pd.Timestamp(period['train']['end']); end=pd.Timestamp(period['evaluation']['end']); start=pd.Timestamp(period['train']['start'])
    times=aligned['date']; horizon=config['parameters']['horizon']
    labels=aligned['close'].shift(-horizon)/aligned['close']-1
    vals=aligned[FEATURES+NEWS_FEATURES]
    fitted=fit_and_select(vals,labels,times,cutoff,horizon,config['parameters']['seed'])
    mask=(times>=cutoff)&(times<end)
    ev=aligned.loc[mask,['date','open','high','low','close','volume']].reset_index(drop=True); x=vals.loc[mask]
    result={'source_model':manifest['id'],'source_snapshot_sha256':manifest['files']['snapshot.feather'],
            'source_feature_sha256':manifest['files']['feature-matrix.feather'],'news_snapshot_sha256':manifest['files']['news-snapshot.json'],
            'period':period,'config':config,'calibration':fitted['audit'],'arms':{},'scope':'research_only_not_deployable'}
    for name,w in [('price_only',0.0),('full_news',1.0),('fixed_25_news',.25),('validation_selected',fitted['weight'])]:
        predictions=predict(fitted,x,w); actions=_prediction_actions(predictions,config); evaluation=evaluate(ev,actions,config)
        decisions=[{'decision_at':(at+HOUR).isoformat(),'prediction':float(pred)} for at,pred in zip(ev['date'],predictions)]
        result['arms'][name]={'weight':w,'metrics':evaluation['metrics'],'risk':risk_metrics(evaluation['equity']),
            'prediction':prediction_metrics(candles,decisions,period,horizon),'predictions':decisions,'equity':evaluation['equity'],'trades':evaluation['trades']}
    return result
