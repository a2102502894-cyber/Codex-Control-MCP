"""Bounded, payload-free call evidence. Never infers upstream safety decisions."""
from __future__ import annotations

import contextvars
import threading
from datetime import datetime, timezone

from .common import utc_now

# The MCP handler seeds one ID; nested bridge calls receive independent IDs.
INCOMING_OPERATION = contextvars.ContextVar('incoming_operation', default=None)
CURRENT_TRACE = contextvars.ContextVar('call_trace', default=None)


class CallTrace:
    def __init__(self, operation_id, *, mcp_received=False, parent_operation_id=None):
        self.operation_id = operation_id
        self.parent_operation_id = parent_operation_id
        self.mcp_received = mcp_received
        self.received_at = utc_now()
        self.stage = 'bridge_received'
        self.rpc_dispatched_count = 0
        self.rpc_response_count = 0
        self.rpc_records = []
        self.lock = threading.RLock()

    def mark(self, stage):
        with self.lock:
            self.stage = stage

    def rpc_event(self, event, method, request_id, generation, *, code=None):
        # Store only protocol metadata, never command text, arguments or output.
        with self.lock:
            if event == 'rpc_send':
                self.rpc_dispatched_count += 1
                self.stage = 'rpc_dispatched'
            elif event in ('rpc_result', 'rpc_error'):
                self.rpc_response_count += 1
                self.stage = 'rpc_response_received'
            row = {'event': event, 'method': method, 'request_id': request_id,
                   'runtime_generation': generation, 'at': utc_now()}
            if type(code) is int:
                row['rpc_code'] = code
            self.rpc_records.append(row)
            del self.rpc_records[:-32]

    def receipt(self, out, *, failure_origin=None, replayed_operation_id=None):
        data = out.get('result')
        data = data if isinstance(data, dict) else {}
        if out.get('idempotent_replay'):
            execution_status = 'cached_receipt'
        elif data.get('state') in ('starting', 'running', 'exited', 'lost', 'failed'):
            execution_status = data['state']
        elif not out.get('ok'):
            execution_status = 'failed_or_unconfirmed'
        elif data.get('exit_code') is not None:
            execution_status = 'exited'
        else:
            execution_status = 'returned'
        with self.lock:
            return {
                'schema_version': 1,
                'operation_id': self.operation_id,
                'parent_operation_id': self.parent_operation_id,
                'received_at': self.received_at,
                'mcp_received': self.mcp_received,
                'bridge_received': True,
                'last_verified_stage': self.stage,
                'rpc_dispatched_count': self.rpc_dispatched_count,
                'rpc_response_count': self.rpc_response_count,
                'rpc_records': [dict(row) for row in self.rpc_records],
                'rpc_records_truncated': self.rpc_dispatched_count + self.rpc_response_count > 32,
                'failure_origin': failure_origin,
                'execution_status': execution_status,
                'execution_operation_id': data.get('origin_operation_id'),
                'replayed_operation_id': replayed_operation_id,
                'upstream_safety_decision': 'not_observable',
                'automatic_retry_performed': False,
            }


def error_origin(error, *, stage=None):
    error = error or {}
    details = error.get('details')
    details = details if isinstance(details, dict) else {}
    explicit = details.get('origin')
    if explicit in ('codex_app_server', 'execution_transport', 'bridge_validation',
                    'bridge_runtime', 'command_process'):
        return explicit
    if stage in ('bridge_received', 'argument_validation'):
        return 'bridge_validation'
    if error.get('code') == 'command_failed':
        return 'command_process'
    if error.get('code') == 'execution_state_unknown':
        return 'execution_transport'
    return 'bridge_or_adapter_unclassified'


def current_rpc_evidence(bridge, methods):
    generation = getattr(getattr(bridge, 'rpc', None), 'generation', None)
    verified = getattr(bridge, 'verified', {})
    matches = [verified[m] for m in methods if isinstance(verified.get(m), dict)
               and verified[m].get('generation') in (None, generation)]
    return max(matches, key=lambda row: row.get('at', '')) if matches else None


def health_observation(bridge, checks, active):
    generation = getattr(getattr(bridge, 'rpc', None), 'generation', None)
    now = utc_now()
    evidence = {}
    for name, status in checks.items():
        row = {'status': status, 'source': 'not_observed', 'observed_at': None,
               'runtime_generation': generation}
        if name in ('codex', 'app_server', 'schema'):
            row.update(source='current_runtime_state', observed_at=now)
        elif active and name in ('shell', 'files', 'proxy') and status != 'NOT_TESTED':
            row.update(source='active_probe', observed_at=now)
        elif name in ('shell', 'files') and status == 'PASS':
            methods = ('command/exec',) if name == 'shell' else ('fs/readFile', 'fs/readDirectory')
            previous = current_rpc_evidence(bridge, methods)
            if previous:
                row.update(source='cached_rpc_success', observed_at=previous.get('at'),
                           runtime_generation=previous.get('generation'))
        elif name in ('browser', 'computer_use') and status == 'PASS':
            row['source'] = 'extension_state_no_fresh_probe'
        if row['observed_at']:
            try:
                observed = datetime.fromisoformat(row['observed_at'])
                row['age_seconds'] = round(max(0, (datetime.now(timezone.utc) - observed).total_seconds()), 3)
            except (ValueError, TypeError):
                row['age_seconds'] = None
        evidence[name] = row
    return {'mode': 'active' if active else 'passive', 'reported_at': now,
            'runtime_generation': generation, 'checks': evidence,
            'guarantees_future_call_authorization': False}
