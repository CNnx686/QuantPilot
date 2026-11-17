import numpy as np
import pandas as pd
import pytest
from quantpilot.terminal_features import FEATURES
from quantpilot.terminal_news_features import NEWS_FEATURES
from quantpilot.terminal_news_blend_research import fit_and_select, predict


def test_calibration_and_refit_exclude_future_labels_and_values():
    rng=np.random.default_rng(12)
    times=pd.date_range('2025-01-01',periods=180,freq='h',tz='UTC')
    features=pd.DataFrame(rng.normal(size=(180,10)),columns=FEATURES+NEWS_FEATURES)
    labels=np.sin(np.arange(180)/15)*.01
    cutoff=times[160]
    first=fit_and_select(features,labels,times,cutoff,6,7)
    changed=features.copy(); changed.loc[160:,:]=500
    labels_changed=labels.copy(); labels_changed[154:]=1e9
    second=fit_and_select(changed,labels_changed,times,cutoff,6,7)
    assert first['audit']==second['audit']
    assert first['audit']['initial_label_max']<=first['audit']['validation_start']
    assert first['audit']['validation_label_max']<=first['audit']['training_cutoff']
    assert first['audit']['refit_label_max']<=first['audit']['training_cutoff']
    assert np.allclose(predict(first,features.iloc[160:]),predict(second,features.iloc[160:]))


def test_rejects_unapproved_weights_and_unordered_features():
    rng=np.random.default_rng(1); times=pd.date_range('2025-01-01',periods=150,freq='h',tz='UTC')
    vals=pd.DataFrame(rng.normal(size=(150,10)),columns=FEATURES+NEWS_FEATURES); y=rng.normal(size=150)
    with pytest.raises(ValueError,match='ordered model features'):
        fit_and_select(vals.iloc[:,::-1],y,times,times[130],6)
    fitted=fit_and_select(vals,y,times,times[130],6)
    with pytest.raises(ValueError,match='Unapproved'):
        predict(fitted,vals.iloc[130:],.33)
