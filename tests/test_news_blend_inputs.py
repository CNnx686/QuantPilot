import json
from pathlib import Path
import pytest
from quantpilot.terminal_news_blend_research import research


def test_research_rejects_missing_verified_source(tmp_path):
    (tmp_path/'manifest.json').write_text(json.dumps({'kind':'prediction','news':{'mode':'historical_archive_assumption'},'data_provenance':{'source_verified':False}}),encoding='utf-8')
    with pytest.raises(ValueError,match='Verified market source'):
        research(tmp_path)


def test_research_rejects_changed_snapshot_bytes(tmp_path):
    manifest={'kind':'prediction','news':{'mode':'historical_archive_assumption'},'data_provenance':{'source_verified':True},'files':{'snapshot.feather':'0'*64}}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    (tmp_path/'snapshot.feather').write_bytes(b'tampered')
    with pytest.raises(ValueError,match='Artifact hash mismatch'):
        research(tmp_path)
