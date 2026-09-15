"""Regression tests: an acknowledged GUI RPC is not proof that input happened."""
import threading
import pytest
from codex_control_mcp.computer import OfficialComputer
from codex_control_mcp.errors import BridgeError

def adapter(result=None, error=None):
 c=OfficialComputer.__new__(OfficialComputer)
 c.action_lock=threading.RLock();c.verified_operations={'snapshot','click','type'};c.verified=True;c.last_failure=None
 def call(*args):
  if error:raise error
  return result or {}
 c._call=call
 return c

def test_ack_without_observed_text_cannot_pass_health():
 c=adapter();c.call('computer_type',{'snapshot_id':'s','text':'proof'})
 assert not c.verified and 'type' not in c.verified_operations
 assert c.last_failure=='input_effect_unverified'

def test_observed_type_can_complete_chain():
 c=adapter({'accessibility':{'tree':'proof'}});c.call('computer_type',{'snapshot_id':'s','text':'proof'})
 assert c.verified and c.last_failure is None

def test_window_title_is_valid_official_post_state_proof():
 c=adapter({'window':{'title':'fixture proof'}});out=c.call('computer_type',{'snapshot_id':'s','text':'proof'})
 assert out['input_effect_verified'] is True
 assert c.verified and c.last_failure is None

def test_unrelated_window_title_cannot_fake_input_proof():
 c=adapter({'window':{'title':'fixture only'},'accessibility':{'tree':'no marker'}});out=c.call('computer_type',{'snapshot_id':'s','text':'proof'})
 assert out['input_effect_verified'] is False
 assert not c.verified and 'type' not in c.verified_operations

def test_failed_action_revokes_old_verified_chain():
 c=adapter(error=BridgeError('execution_state_unknown','failed to activate captured window'))
 with pytest.raises(BridgeError):c.call('computer_click',{'snapshot_id':'s','element_index':2})
 assert not c.verified and not c.verified_operations and c.last_failure=='execution_state_unknown'
