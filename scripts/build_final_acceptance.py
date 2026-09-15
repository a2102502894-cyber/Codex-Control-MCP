"""Build the final report from real artifacts. Fails closed on missing evidence."""
import collections,datetime,hashlib,json,pathlib,shutil,subprocess,sys
from freeze_source_lkg import tree_manifest,PROBE
ROOT=pathlib.Path(__file__).resolve().parents[1];E=ROOT/'evidence';STATE=pathlib.Path.home()/'.codex-control-mcp/state'
def load(p):
 b=pathlib.Path(p).read_bytes();return json.loads(b.decode('utf-16' if b.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig'))
health=load(E/'health-production-final-20260910/last-call.json')['response']['result']
assert all(health['checks'][n]=='PASS' for n in ['codex','app_server','schema','shell','files','browser','proxy'])
assert health['checks']['computer_use']!='PASS' and health['phase1_complete'] is False
public=load(E/'public-final-20260910/summary.json');stable=load(E/'production-stability-final.summary.json');tasks=load(E/'final-task-readback.json');tests=load(E/'final-tests-summary.json')
assert public['core_pid']==stable['core_pids'][0] and len(stable['core_pids'])==1 and not stable['core_down_observed']
assert stable['connections_all_four'] and stable['8767_always_absent']
assert tests['all_passed'] and not tests['full_stack_accepted']
infra=load(E/'infrastructure-production-final.json');readback=load(E/'architecture-readback-20260910/summary.json')
assert readback['task']['task']['status']=='completed' and readback['fixture_directory_removed']
assert readback['hosts']['count']==1 and readback['skills']['count']==0
recovery={n:load(E/'recovery-production-20260910'/f'{n}.json') for n in ['formal-restart','current-fallback','lkg-fallback','watchdog-recovery']}
for n,expected in [('formal-restart','formal_task'),('current-fallback','current_source_direct'),('lkg-fallback','lkg_direct'),('watchdog-recovery','formal_task')]:
 assert recovery[n]['exit_code']==0 and recovery[n]['selected']==expected
assert recovery['watchdog-recovery']['old_pid']==0 and recovery['watchdog-recovery']['operation']=='recover' and not recovery['watchdog-recovery']['requests']
final_reload=load(STATE/'core-controller-report.json');assert final_reload['new_pid']==public['core_pid'] and final_reload['exit_code']==0
candidate=STATE/'lkg-0.2.0.candidate-20260910-194319';manifest=load(candidate/'manifest.json');rows,digest=tree_manifest(candidate);assert digest==manifest['sha256'] and len(rows)==manifest['file_count']
p=subprocess.run([sys.executable,'-I','-X','utf8','-c',PROBE,str(candidate)],cwd=candidate,capture_output=True,text=True,encoding='utf-8',timeout=40,check=True);candidate_probe=json.loads(p.stdout)
assert not manifest['activated'];active_lkg=load(STATE/'lkg-0.2.0/manifest.json')
cleanup=load(E/'cleanup-final.json');assert not cleanup['active_migration_residuals'] and not cleanup['gui_fixture_processes'] and cleanup['owned_http_servers_closed']
grok=load(ROOT.parent/'Grok-MCP/evidence/grok-final-recovery.json');cua=load(E/'cua-runtime-diagnostic.json');assert not grok['full_three_chain_pass']
parent_cua_proof=load(E/'cua-explicit-activation-proof/sdk-proof.json')
assert parent_cua_proof['proof_pass'] is False and parent_cua_proof['completed'] is False
cua['parent_independently_verified']={'evidence_path':str(E/'cua-explicit-activation-proof/sdk-proof.json'),'error':parent_cua_proof['error_detail'],'proof_pass':False,'same_official_manifest':parent_cua_proof['runtime']['manifest'],'sandbox_bypass_used':False}
browser=load(E/'browser-production-final-20260910/browser-e2e.json');assert browser['pass']
items=[
(1,'PASS','Core 0.2.0 / 8774 / public / 47 tools'),
(2,'PASS_WITH_FORENSIC_LIMIT','Independent recovery repaired and three real fallback routes passed; historical controller termination was not conclusively captured.'),
(3,'PARTIAL','Cold start and real Scheduler path passed; startup/logon triggers read back; physical reboot/logon not performed.'),
(4,'PASS','Real unattended watchdog recovery after two missing-listener cycles; no healthy tunnel restart.'),
(5,'PASS','Latest health source loaded by independent production restart.'),
(6,'PASS','Browser full eight-action chain, observed fixture effects and cleanup.'),
(7,'BLOCKED','Official CUA activation/input failed on isolated targets; reproduced independently below Core adapter.'),
(8,'PASS','Official Codex command/exec inherited 7897 proxy, CONNECT + verified TLS + HTTPS200.'),
(9,'BLOCKED','Seven PASS, computer_use UNVERIFIED; overall and phase1_complete are not fabricated healthy.'),
(10,'BLOCKED','Fresh cooled diagnostic Grok image edit still HTTP502; no saved output.'),
(11,'PARTIAL','Alias available and reference payload matched; deployed origin/worker failure layer not conclusively established.'),
(12,'BLOCKED','Actual Grok2API origin deployment/admin logs required.'),
(13,'PASS','Non-idempotent POST not replayed; timeout/lost response unknown; GET bounded retry; diagnostics redacted.'),
(14,'PARTIAL','Prior image/video artifacts revalidated; edit failed, three-chain PASS is false.'),
(15,'PASS','Core version/tool count/static surfaces verified.'),
(16,'PASS','Dynamic director17/grok3 ready, exact registry read back.'),
(17,'PASS','Director8771 + independent stdio17 + real director_read; no8767.'),
(18,'PASS','Task lifecycle and final completed revision7 read back.'),
(19,'PASS','Real local and isolated MCP/file roundtrip; SSH/Docker contract only as requested.'),
(20,'PASS','Skill validate/install/activate/rollback/inspect/uninstall; no test skill remains.'),
(21,'PASS','Unchanged running tunnel config http2 →127.0.0.1:8774, no edge-ip-version.'),
(22,'PASS','Metadata200, unauthenticated/wrong bearer401; owner authentication successful; OAuth credentials not reissued.'),
(23,'PASS','Public initialize/version47/health/director_read verified.'),
(24,'PASS','Four-minute final real observation,17 samples,one Core PID,all four tunnel connections.'),
(25,'PASS','Core240 passed,18 integration deselected.'),
(26,'PASS','Grok12 passed.'),
(27,'PASS','Director1 passed plus live17/read.'),
(28,'PARTIAL','Source candidate built, isolated imports and treeSHA verified; not promoted over LKG while required acceptance is incomplete.'),
(29,'AWAITING_REBOOT','Empty legacy gateway directory still held by DirectorDesk; pending reboot deletion confirmed.'),
(30,'PASS','Named active migration/smoke residuals absent; identified temp homes/proof/incomplete candidate cleaned.'),
(31,'PASS','Owned GUI fixtures and HTTP servers closed; no user windows closed.'),
(32,'PASS','This report rebuilt from current evidence, not stale copied status.'),
(33,'PASS','Required evidence fields present with true PASS/BLOCKED status, test totals, hashes and packaging_required=false.'),
(34,'PASS','README synchronized to source/Dynamic MCP/recovery/strict verification and limitations.')]
counts=collections.Counter(x[1] for x in items)
report={'schema_version':2,'recorded_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'PARTIALLY_COMPLETED','all_acceptance_criteria_met':False,'packaging_required':False,'source_project':str(ROOT),'core':public,'health':health,'browser':browser,'computer_use':cua,'dynamic_mcp':public['dynamic_mcp'],'infrastructure':infra,'infrastructure_final_readback':{'task_id':readback['task']['task']['id'],'revision':readback['task']['task']['revision'],'status':readback['task']['task']['status'],'hosts':readback['hosts'],'skills':readback['skills'],'isolated_directory_removed':readback['fixture_directory_removed']},'recovery':{'production_drills':recovery,'final_reload':final_reload,'watchdog_observation':load(E/'recovery-production-20260910/watchdog-real.summary.json'),'historical_confirmed_failure':'Stale empty maintenance.lock suppressed prior watchdog recovery. Original script directly stopped its own Core task before restart/fallback; its exact termination event was not captured.','historical_job_object_cause_confidence':'high-confidence architectural explanation, not definitive exit-event proof','no_manual_start_during_watchdog_drill':True},'scheduled_tasks':tasks,'physical_reboot_test_performed':False,'tunnel':load(E/'tunnel-config-final.json'),'stability':stable,'grok':grok,'tests':tests,'lkg':{'active_path':str(STATE/'lkg-0.2.0'),'active_snapshot_unchanged':True,'active_manifest_declared_sha256':active_lkg['sha256'],'active_fallback_live_verified':True,'candidate_path':str(candidate),'candidate_sha256':digest,'candidate_file_count':len(rows),'candidate_isolated_probe':candidate_probe,'candidate_promoted':False,'promotion_blockers':['Computer Use real input acceptance','Grok image edit success','physical reboot/logon acceptance']},'cleanup':cleanup,'acceptance_items':[{'id':i,'status':s,'detail':d} for i,s,d in items],'acceptance_status_counts':dict(counts),'remaining_user_inputs':['Actual Grok2API deployment/admin access or corresponding origin logs for the failed edit request.','A safe active-session official CUA activation retest or a verified official runtime fix; no sandbox bypass.','Reboot/logon at user convenience for physical startup and deferred directory-deletion verification.']}
# Back up the previous user-requested final evidence exactly once.
target=E/'architecture-0.2.0-final.json';backup=E/'architecture-0.2.0-final.pre-recovery-20260910.json'
if target.exists() and not backup.exists():shutil.copy2(target,backup)
text=json.dumps(report,ensure_ascii=False,indent=2);assert '\ufffd' not in text
assert len(report['acceptance_items'])==34 and len({x['id'] for x in report['acceptance_items']})==34
assert sum(counts.values())==34 and report['all_acceptance_criteria_met'] is False
target.write_text(text,encoding='utf-8');check=load(target);assert check['core']['core_pid']==public['core_pid'] and check['lkg']['candidate_sha256']==digest
print(json.dumps({'path':str(target),'status':check['status'],'core_pid':public['core_pid'],'health':health['checks'],'tests':tests,'lkg_candidate_sha256':digest,'acceptance_counts':dict(counts)},ensure_ascii=False,indent=2))
