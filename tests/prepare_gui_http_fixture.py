import json
from pathlib import Path
import socket
import uuid
from codex_control_mcp.auth import owner_token
from codex_control_mcp.config import Config, DEFAULT_CONFIG

root = Path(__file__).resolve().parents[1]
home = root / 'test-workspace' / ('gui-http-' + uuid.uuid4().hex[:8])
home.mkdir()
with socket.socket() as bound:
    bound.bind(('127.0.0.1', 0))
    port = bound.getsockname()[1]
text = ('computer_use_enabled = true\nlocal_application_consent = false\n' + DEFAULT_CONFIG).replace('8767', str(port))
(home / 'config.toml').write_text(text, 'utf-8')
cfg = Config.load(home)
cfg.initialize_storage()
owner_token(cfg, create=True)
record = {'home': str(home), 'port': port, 'purpose': 'owned_GUI_MCP_HTTP_fixture'}
(root / 'evidence/gui-http-service-current.json').write_text(json.dumps(record, indent=2), 'utf-8')
print(json.dumps(record))
