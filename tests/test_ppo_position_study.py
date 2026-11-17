import pandas as pd

from quantpilot.ppo_position_study import conflicts, summary


def test_position_conflict_counts_exclude_final_unexecutable_decision():
    frame=pd.DataFrame({'date':pd.date_range('2025-01-01',periods=5,freq='h',tz='UTC'),
        'open':[100.]*5,'high':[101.]*5,'low':[99.]*5,'close':[100.]*5,'volume':[1.]*5})
    config={'wallet':10000.,'stake':1000.,'fee':.001,'parameters':{'stop_loss':.05}}
    rows=conflicts(frame,[1,1,2,2,2],config)
    assert [r['position_before'] for r in rows] == [0,1,1,0,0]
    assert [r['position_conflict'] for r in rows] == [False,True,False,True,True]
    assert summary(rows) == {'evaluated_decisions':4,'position_conflict_rate':0.5}
