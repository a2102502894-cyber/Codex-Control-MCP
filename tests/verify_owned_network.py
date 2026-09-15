"""Real ETW observation, with a separate non-forwarding proxy diagnostic.

Requires administrator PowerShell 7 and Microsoft's TraceEvent 3.2.6 NuGet
package, extracted outside the product. Does not change machine proxy/policies,
stop other traces, inspect TLS payloads, or persist unrelated process details.
"""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe', type=Path, required=True)
    ap.add_argument('--pwsh', type=Path, required=True)
    ap.add_argument('--traceevent', type=Path, required=True,
                    help='Extracted TraceEvent/lib/netstandard2.0/Microsoft.Diagnostics.Tracing.TraceEvent.dll')
    args = ap.parse_args()
    for p in (args.exe, args.pwsh, args.traceevent):
        if not p.is_file():
            raise SystemExit('Required local dependency is absent: ' + str(p))
    results = {}
    for mode in ('inherited', 'classify'):
        run = ROOT / 'test-workspace' / ('etw-' + mode + '-' + uuid.uuid4().hex[:10])
        run.mkdir(parents=True)
        script = '\n'.join([
            "$ErrorActionPreference='Stop'",
            "$env:CCM_TRACE_CLASSIFY_PROXY=" + quote('1' if mode == 'classify' else '0'),
            '$library=' + quote(args.traceevent.resolve()),
            'Add-Type -Path $library',
            "$refs=@(Get-ChildItem (Join-Path $PSHOME 'ref') -Filter *.dll | Select-Object -ExpandProperty FullName)",
            '$refs+=$library',
            'Add-Type -Path ' + quote(ROOT / 'tests/OwnedNetworkCapture.cs') + ' -ReferencedAssemblies $refs',
            '[CCMNetworkCapture]::Run(' + ','.join(map(quote, [sys.executable, ROOT / 'tests/network_fixture.py', run, args.exe.resolve()])) + ') | Out-Null',
        ])
        p = subprocess.run([str(args.pwsh), '-NoProfile', '-NonInteractive', '-Command', script],
                           capture_output=True, timeout=160, creationflags=subprocess.CREATE_NO_WINDOW)
        if p.returncode or not (run / 'etw.json').is_file() or not (run / 'fixture.json').is_file():
            raise RuntimeError('ETW fixture did not complete: ' + p.stderr.decode('utf-8', 'replace')[-1500:])
        trace = json.loads((run / 'etw.json').read_text('utf-8-sig'))
        fixture = json.loads((run / 'fixture.json').read_text('utf-8-sig'))
        events = trace['owned_network_events']
        marker = str(fixture['positive_control_port'])
        positive = any(marker in (e['network'].get('dport'), e['network'].get('sport'))
                       and e['event'] == 'TcpIp/Send' for e in events)
        names = {p['name'].lower() for p in trace['owned_processes']}
        capture_ok = bool(not trace.get('error') and trace['events_lost'] == 0
                          and not trace['reader_error'] and trace['session_removed']
                          and trace.get('fixture_exit_code') == 0 and fixture['pass'] and positive
                          and 'codex.exe' in names and 'powershell.exe' in names)
        record = {'capture_pass': capture_ok, 'fixture': fixture, 'trace': trace,
                  'positive_control_observed': positive}
        destination = ROOT / 'evidence' / ('owned-network-' + mode + '.json')
        destination.write_text(json.dumps(record, ensure_ascii=False, indent=2), 'utf-8')
        results[mode] = {'capture_pass': capture_ok, 'events_lost': trace['events_lost'],
                         'owned_process_count': len(trace['owned_processes']),
                         'owned_network_event_count': len(events), 'positive_control_observed': positive,
                         'configured_model_requests': fixture['configured_model_requests'],
                         'proxy_destinations': fixture.get('proxy_destination_attempts', []),
                         'session_removed': trace['session_removed'], 'evidence': destination.name}
        if not capture_ok:
            raise RuntimeError('Network observation failed its coverage assertions; see ' + destination.name)
    report = {
        'capture_pass': all(v['capture_pass'] for v in results.values()),
        'exe_sha256': hashlib.sha256(args.exe.read_bytes()).hexdigest(),
        'traceevent_version': '3.2.6',
        'traceevent_nuget_sha256': 'e4dd62e649642130145fa6e07f75a73ceb9c617b739b96455fb60b7cac89ed20',
        'traceevent_source': 'https://www.nuget.org/packages/Microsoft.Diagnostics.Tracing.TraceEvent/3.2.6',
        'runs': results,
        'full_chain_zero_model_traffic': 'UNVERIFIED',
        'reason': 'ETW proves process ancestry and TCP/UDP activity, not encrypted application paths. The separate proxy diagnostic records destination authorities and refuses forwarding. Zero requests at the configured model endpoint is a narrower assertion.',
        'tls_intercepted': False, 'global_proxy_modified': False,
        'unrelated_process_details_persisted': False,
    }
    (ROOT / 'evidence/owned-network-trace.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
