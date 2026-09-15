"""Assemble genuine Grok evidence; does not submit another generation request."""
import datetime,hashlib,json,pathlib,shutil,subprocess
root=pathlib.Path(__file__).resolve().parents[1];grok=root.parent/'Grok-MCP'
rec=json.loads((root/'evidence/grok-diagnostic-20260910/last-call.json').read_text('utf-8'))
assert rec['name']=='mcp_tool_call'
edit=rec['response']['result']['result']['structuredContent'];assert edit['ok'] is False
files=[]
for n in ['dynamic-generate.jpg','dynamic-video-smoke.mp4']:
 p=grok/'evidence'/n;b=p.read_bytes();files.append({'saved_path':str(p),'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest(),'exists':True})
probe=shutil.which('ffprobe')
video={'probe_available':bool(probe)}
if probe:
 p=subprocess.run([probe,'-v','error','-show_entries','format=duration:stream=codec_name,width,height,codec_type','-of','json',files[1]['saved_path']],capture_output=True,text=True,encoding='utf-8',check=True)
 video.update(json.loads(p.stdout))
result={'recorded_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'blocked','full_three_chain_pass':False,'image_generate':{'status':'historical_success_artifact_revalidated','new_generation_submitted':False,**files[0]},'image_edit':{'status':'failed','request_started_at':rec['at'],'idempotency_key':rec['arguments']['idempotency_key'],'output':edit,'success_artifact_exists':any((grok/'evidence').glob('final-edit-diag-v2-20260910*'))},'video_generate':{'status':'historical_success_artifact_revalidated','new_generation_submitted':False,**files[1],'media_probe':video},'diagnosis':{'live_model_listing_contains_edit_alias':True,'payload_matches_reference_upstream_handler':True,'reference_upstream_commit':'8913b53fe92307a6f111b2885ab298a43c74a9ba','actual_deployed_upstream_revision_verified':False,'last_response_body':'error code: 502','actual_failure_layer_confirmed':False,'blocker':'Need the actual Grok2API origin deployment/admin logs; the public 502 body has no upstream request identifier or structured worker error.'},'non_idempotent_protection':{'post_5xx_automatic_replays':0,'post_timeout_or_lost_response':'execution_state_unknown','get_status_bounded_retries':True},'regression':json.loads((root/'evidence/final-tests-summary.json').read_text('utf-8'))['grok']}
p=grok/'evidence/grok-final-recovery.json';p.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False,indent=2))
