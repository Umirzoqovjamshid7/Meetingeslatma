import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_meros_group_exists_and_has_null_ids():
    groups = json.loads((ROOT / 'groups.json').read_text(encoding='utf-8'))
    assert 'Meros' in groups
    assert groups['Meros']['chat_id'] is None
    assert groups['Meros']['topic_id'] is None
