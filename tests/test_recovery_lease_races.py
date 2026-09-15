"""Real OS lease exclusion plus controller unit-policy regression, not production fault acceptance."""
import importlib.util
import json
from pathlib import Path
from datetime import timedelta

SPEC=importlib.util.spec_from_file_location('recovery',Path(__file__).resolve().parents[1]/'scripts/core_recovery_controller.py')
c=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(c)


def test_guard_excludes_contender_even_when_metadata_empty(tmp_path):
    path=tmp_path/'maintenance.lock'
    one=c.Lease(path);two=c.Lease(path)
    assert one.acquire()
    path.write_bytes(b'')
    assert not two.acquire()
    one.release()
    assert two.acquire()
    two.release()
    assert not path.exists() and Path(str(path)+'.guard').exists()


def test_released_guard_can_be_reused_repeatedly(tmp_path):
    path=tmp_path/'maintenance.lock'
    for _ in range(20):
        one=c.Lease(path);two=c.Lease(path)
        assert one.acquire() and not two.acquire()
        one.release();assert two.acquire();two.release()


def test_held_lease_does_not_consume_restart_request(tmp_path):
    home=tmp_path/'home';(home/'state').mkdir(parents=True)
    q=home/'state/requests';q.mkdir()
    (q/'pending.json').write_text(json.dumps({'operation':'restart','created_at':c.iso(c.utc_now())}))
    config={'home':str(home),'port':8774,'expected_version':'0.2.0','lease':str(home/'state/maintenance.lock'),
            'report':str(home/'state/report.json'),'request':str(q),'host_tasks':['core'],
            'candidates':[{'name':'formal_task','task':'core'}]}
    cfg=home/'config.json';cfg.write_text(json.dumps(config))
    lock=c.Lease(Path(config['lease']));assert lock.acquire()
    try:
        assert c.run(cfg)==c.EXIT_LEASE_HELD
        assert (q/'pending.json').exists()
    finally:lock.release()


def test_multiple_queued_requests_are_coalesced_without_loss(tmp_path):
    for i,kind in enumerate(['recover','restart']):
        (tmp_path/f'{i}.json').write_text(json.dumps({'operation':kind,'created_at':c.iso(c.utc_now()),'id':i}))
    operation,requests=c.consume_requests(tmp_path)
    assert operation=='restart' and len(requests)==2
    assert not list(tmp_path.glob('*.json'))
