import json
import math

import pytest

from codex_control_mcp.bridge import Bridge
from codex_control_mcp.config import Config
from codex_control_mcp.errors import BridgeError
from codex_control_mcp.tools import validate_tool


@pytest.mark.parametrize('text', ['\ud800', '\udc87', 'private-value\udcad'])
def test_invalid_unicode_never_reaches_official_executor(tmp_path, monkeypatch, text):
    bridge = Bridge(Config(home=tmp_path, cwd=str(tmp_path), computer_use_enabled=True))
    called = []
    monkeypatch.setattr(bridge, '_do', lambda *args: called.append(args))
    try:
        result = bridge.execute('computer_type', {'snapshot_id': 'observed', 'text': text})
        assert result['ok'] is False
        assert result['error']['code'] == 'invalid_arguments'
        assert called == []
        assert 'private-value' not in json.dumps(result)
        json.dumps(result, ensure_ascii=False).encode('utf-8')
    finally:
        bridge.close()


def test_unicode_environment_keys_are_checked():
    with pytest.raises(BridgeError) as caught:
        validate_tool('exec_command', {'command': 'exit 0', 'env': {'\ud800': 'x'}})
    assert caught.value.code == 'invalid_arguments'


@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf])
def test_nonfinite_gui_coordinates_are_rejected(value):
    with pytest.raises(BridgeError):
        validate_tool('computer_click', {'snapshot_id': 'observed', 'x': value, 'y': 0})


def test_chinese_and_supplementary_unicode_remain_unchanged():
    args = json.loads('{"snapshot_id":"observed","text":"中文\\ud83d\\ude00"}')
    validate_tool('computer_type', args)
    assert args['text'] == '中文😀'
