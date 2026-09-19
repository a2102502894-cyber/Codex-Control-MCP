"""Deterministic adapter for the official installed @oai/sky CUA plugin.
No copied runtime, custom native helper, alternative GUI backend, or model turns.
"""

from __future__ import annotations
import asyncio, concurrent.futures, json, os, pathlib, queue, threading, uuid
from .common import digest
from .errors import BridgeError


class OfficialComputer:
    surface = "computer"
    backend = "official_node_repl_unsandboxed -> @oai/sky"

    def __init__(self, cfg):
        self.cfg = cfg
        self.requests = queue.Queue()
        self.ready = threading.Event()
        self.error = None
        self.closed = False
        self.loop = None
        self.main_task = None
        self.info = {}
        self.verified = False
        self.verified_operations = set()
        self.last_failure = None
        self.calls = 0
        self.current_forwarder = None
        self.last_authorization = None
        self.action_lock = threading.RLock()
        self.thread = threading.Thread(
            target=self._entry, name="official-cua-mcp", daemon=True
        )
        self.thread.start()
        if not self.ready.wait(40):
            self.close()
            raise BridgeError(
                "capability_unavailable",
                "Official CUA plugin initialization timed out.",
            )
        if self.error:
            self.close()
            raise BridgeError("capability_unavailable", self.error)

    def _entry(self):
        try:
            asyncio.run(self._run())
        except BaseException as e:
            self.error = f"Official CUA plugin stopped ({type(e).__name__})."
            self.ready.set()
        finally:
            self.closed = True
            self.ready.set()
            while True:
                try:
                    item = self.requests.get_nowait()
                except queue.Empty:
                    break
                if item and not item[1].done():
                    item[1].set_exception(
                        BridgeError(
                            "execution_state_unknown",
                            "Official CUA connection stopped; action is not replayed.",
                        )
                    )

    def _application_decision(self, request):
        if self.current_forwarder is not None:
            return self.current_forwarder(request)
        from .consent import resolve_local_application_consent

        decision = resolve_local_application_consent(self.cfg, request)
        if decision is not None:
            return decision
        return {"action": "cancel", "_meta": {"codex_control_mcp": {
            "decision_source": "no_interactive_approval_handler",
        }}}

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        self.main_task = asyncio.current_task()
        from mcp import ClientSession, StdioServerParameters, types
        from mcp.client.stdio import stdio_client

        base = (
            pathlib.Path.home()
            / ".codex/plugins/cache/openai-bundled/unified-computer-use"
        )
        manifests = sorted(
            base.glob("*/.mcp.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True
        )
        if not manifests:
            raise BridgeError(
                "capability_unavailable",
                "Official installed CUA plugin manifest was not found.",
            )
        manifest = manifests[0]
        c = json.loads(manifest.read_text("utf-8"))["mcpServers"]["cua_repl"]
        official_env = dict(c.get("env") or {})
        node_repl_value = official_env.get("CUA_REPL_NODE_REPL_PATH")
        if not isinstance(node_repl_value, str) or not node_repl_value.strip():
            raise BridgeError(
                "runtime_missing",
                "Official CUA node_repl path is missing from the installed manifest.",
            )
        command = pathlib.Path(node_repl_value)
        if not command.is_file():
            raise BridgeError("runtime_missing", "Official CUA node_repl is missing.")
        args = ["--disable-sandbox"]
        from .config import build_environment

        env, proxy_evidence = build_environment(self.cfg)
        env.update(official_env)
        env["CODEX_HOME"] = str(
            official_env.get("CODEX_HOME") or pathlib.Path.home() / ".codex"
        )
        env["CUA_REPL_ENABLED_SURFACES"] = self.surface
        env.pop("SKY_CUA_NATIVE_PIPE", None)
        env.pop("SKY_CUA_NATIVE_PIPE_DIRECTORY", None)
        env["NODE_REPL_TRUSTED_SERVICES"] = json.dumps(
            {"sky": "@oai/sky/service"}, separators=(",", ":")
        )
        env["NODE_REPL_TRUSTED_RPC_ENABLED"] = "1"
        allowlist = {
            x.strip()
            for x in env.get("NODE_REPL_UNTRUSTED_ENV_ALLOWLIST", "").split(",")
            if x.strip()
        }
        allowlist.update(
            {
                "CODEX_HOME",
                "CODEX_CLI_PATH",
                "CUA_REPL_ENABLED_SURFACES",
                "NODE_REPL_NODE_MODULE_DIRS",
                "NODE_REPL_NODE_PATH",
                "NODE_REPL_TRUSTED_CODE_PATHS",
            }
        )
        env["NODE_REPL_UNTRUSTED_ENV_ALLOWLIST"] = ",".join(sorted(allowlist))
        for k in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            env.pop(k, None)
        self.info = {
            "manifest": str(manifest),
            "manifest_sha256": digest(manifest.read_bytes()),
            "node_path": str(official_env.get("NODE_REPL_NODE_PATH") or ""),
            "node_repl_path": str(command),
            "plugin_version": manifest.parent.name,
            "backend": self.backend,
            "surfaces": [self.surface],
            "node_repl_sandbox": "disabled_for_windows_desktop_cua",
            "native_pipe_dependency": False,
            "experimental": True,
            "model_turns_requested": False,
            "application_access_policy": self.cfg.application_access_policy,
            "proxy_source": proxy_evidence["source"],
        }

        async def elicit(context, params):
            request = params.model_dump(mode="json", by_alias=True)
            self.last_authorization = {
                "message": request.get("message"),
                "decision": "cancel",
            }
            # MCP and direct local calls both resolve the owner's saved choice.
            try:
                response = await asyncio.to_thread(self._application_decision, request)
                decision = types.ElicitResult.model_validate(response)
                self.last_authorization["decision"] = decision.action
                self.last_authorization["source"] = (
                    (response.get("_meta") or {}).get("codex_control_mcp") or {}
                ).get("decision_source", "interactive_client_or_local_dialog")
                return decision
            except Exception as exc:
                self.last_authorization.update(
                    source="approval_transport_error", error_type=type(exc).__name__
                )
                return types.ElicitResult(action="cancel")

        with open(os.devnull, "w") as err:
            async with stdio_client(
                StdioServerParameters(
                    command=str(command), args=args, cwd=str(manifest.parent), env=env
                ),
                errlog=err,
            ) as (read, write):
                async with ClientSession(
                    read, write, elicitation_callback=elicit
                ) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    if not any(t.name == "js" for t in listed.tools):
                        raise BridgeError(
                            "version_incompatible",
                            "Official CUA plugin no longer exposes js.",
                        )
                    self.ready.set()
                    while True:
                        item = await asyncio.to_thread(self.requests.get)
                        if item is None:
                            break
                        code, future, forwarder = item
                        self.current_forwarder = forwarder
                        self.last_authorization = None
                        try:
                            async with asyncio.timeout(190):
                                result = await session.call_tool(
                                    "js",
                                    {
                                        "code": code,
                                        "title": "Codex-Control-MCP official "
                                        + self.surface
                                        + " action",
                                        "timeout_ms": 180000,
                                    },
                                )
                            data = result.model_dump(mode="json")
                            self.calls += 1
                            if not future.done():
                                future.set_result(data)
                        except BaseException as e:
                            if not future.done():
                                future.set_exception(
                                    BridgeError(
                                        "execution_state_unknown",
                                        f"Official CUA call failed ({type(e).__name__}); observe before retrying.",
                                    )
                                )
                            if isinstance(e, asyncio.CancelledError):
                                raise

    def _call(self, code, forwarder=None):
        if self.closed or not self.thread.is_alive():
            raise BridgeError(
                "capability_unavailable", "Official CUA connection is closed."
            )
        f = concurrent.futures.Future()
        self.requests.put((code, f, forwarder))
        try:
            data = f.result(195)
        except concurrent.futures.TimeoutError:
            raise BridgeError(
                "execution_state_unknown",
                "Official CUA response timed out; action is not replayed.",
            )
        text = [
            x.get("text", "")
            for x in data.get("content", [])
            if x.get("type") == "text"
        ]
        payload = None
        for value in text:
            for line in value.splitlines():
                if line.startswith("CCM_RESULT="):
                    try:
                        payload = json.loads(line[len("CCM_RESULT=") :])
                    except ValueError:
                        pass
        if data.get("isError") or payload is None:
            raise BridgeError(
                "capability_unavailable"
                if data.get("isError")
                else "version_incompatible",
                "Official CUA call returned an error or an unrecognized result.",
                details={"official_text_tail": text[-1][-1800:] if text else ""},
            )
        if payload.get("error"):
            if (
                self.last_authorization
                and self.last_authorization.get("decision") != "accept"
            ):
                transport_error = self.last_authorization.get("source") == "approval_transport_error"
                raise BridgeError(
                    "authorization_transport_error" if transport_error else "authorization_required",
                    "Application access approval could not be delivered."
                    if transport_error else "The official GUI runtime did not receive application access approval.",
                    details=self.last_authorization,
                )
            code = payload.get("error_code", "execution_state_unknown")
            raise BridgeError(code, payload["error"])
        payload["_images"] = [
            x for x in data.get("content", []) if x.get("type") == "image"
        ]
        payload["backend"] = self.info["backend"]
        payload["runtime"] = self.info
        if self.last_authorization:
            payload["application_authorization"] = dict(self.last_authorization)
        return payload

    def call(self, tool, args, forwarder=None):
        with self.action_lock:
            request = {
                "tool": tool,
                "args": args,
                "snapshot_id": "cu_" + uuid.uuid4().hex,
            }
            encoded = json.dumps(request, ensure_ascii=True)
            code = """{
 const req=REQUEST;
 if(!globalThis.ccmSky){globalThis.ccmSky=(await import("@oai/sky")).sky;globalThis.ccmObservations=new Map();}
 const sky=globalThis.ccmSky;
 const observe=async (window,lastAction=null)=>{
   const state=await sky.get_window_state({window,include_screenshot:true,include_text:true});
   globalThis.ccmObservations.clear();globalThis.ccmObservations.set(req.snapshot_id,{state,at:Date.now(),lastAction});
   return {snapshot_id:req.snapshot_id,observed_at_ms:Date.now(),valid_for_ms:120000,window:state.window,accessibility:state.accessibility,screenshots:(state.screenshots||[]).map(({url,...info})=>info),coordinate_space:"official_screenshot_window_relative"};
 };
 try {
   let result;
   if(req.tool==="computer_wait"){await new Promise(resolve=>setTimeout(resolve,req.args.milliseconds));result={waited_ms:req.args.milliseconds};}
   else if(req.tool==="computer_snapshot"){
     const windows=await sky.list_windows();
     if(req.args.window_id==null){result={windows,selection_required:true};}
     else {const found=windows.filter(w=>w.id===req.args.window_id);if(found.length!==1)throw new Error("Target must be exactly one currently returned official window");result=await observe(found[0]);}
   }else{
     const observation=globalThis.ccmObservations.get(req.args.snapshot_id);
     if(!observation||Date.now()-observation.at>120000){nodeRepl.write("CCM_RESULT="+JSON.stringify({error_code:"stale_snapshot",error:"Capture a fresh official window snapshot before acting."}));return;}
     const state=observation.state;globalThis.ccmObservations.delete(req.args.snapshot_id);
     if(req.tool==="computer_click"){
       const click={window:state.window};
       if(req.args.element_index!=null){click.element_index=req.args.element_index;}
       else {if(req.args.x==null||req.args.y==null||!state.screenshots?.length)throw new Error("Coordinate click requires x, y and the current screenshot");click.x=req.args.x;click.y=req.args.y;click.screenshotId=state.screenshots[0].id;}
       await sky.click(click);
     }else if(req.tool==="computer_type"){
       // focused_element is optional in the official Window2 API (including
       // screenshot-only WinForms). A fresh successful click in this exact
       // observed window is the explicit targeting alternative, not a global
       // focus assumption. Ordinary snapshots and other windows never qualify.
       if(!state.accessibility?.focused_element && observation.lastAction!=="computer_click"){
         nodeRepl.write("CCM_RESULT="+JSON.stringify({error_code:"invalid_arguments",error:"Click the intended input in this window before typing when accessibility focus is unavailable."}));return;
       }
       await sky.type_text({window:state.window,text:req.args.text});
     }else if(req.tool==="computer_press"){await sky.press_key({window:state.window,key:req.args.key});}
     else if(req.tool==="computer_scroll"){await sky.scroll({window:state.window,x:req.args.x,y:req.args.y,scrollX:req.args.delta_x||0,scrollY:req.args.delta_y||0,screenshotId:state.screenshots?.[0]?.id});}
     else throw new Error("Unsupported native action");
     result=await observe(state.window,req.tool);
   }
   nodeRepl.write("CCM_RESULT="+JSON.stringify(result));
 }catch(error){nodeRepl.write("CCM_RESULT="+JSON.stringify({error_code:"execution_state_unknown",error:String(error)}));}
}""".replace("REQUEST", encoded)
            # Top-level return is not valid in the official JS cell. Wrap the deterministic cell.
            code = "await (async()=>" + code + ")();"
            try:
                result = self._call(code, forwarder)
            except BridgeError as exc:
                self.verified_operations.clear()
                self.verified = False
                self.last_failure = exc.code
                raise
            if tool == "computer_snapshot" and result.get("screenshots"):
                self.verified_operations.add("snapshot")
            elif tool == "computer_type":
                accessibility = result.get("accessibility") or {}
                window = result.get("window") or {}
                observed_values = [
                    accessibility.get("tree"),
                    accessibility.get("document_text"),
                    accessibility.get("focused_element"),
                    accessibility.get("selected_text"),
                    window.get("title"),
                ]
                text = args.get("text")
                result["input_effect_verified"] = bool(text) and any(
                    isinstance(value, str) and text in value for value in observed_values
                )
                if result.get("input_effect_verified"):
                    self.verified_operations.add("type")
                    self.last_failure = None
                else:
                    self.verified_operations.discard("type")
                    self.last_failure = "input_effect_unverified"
            elif tool in {
                "computer_click",
                "computer_press",
                "computer_scroll",
            }:
                self.verified_operations.add(tool.removeprefix("computer_"))
            self.verified = {"snapshot", "click", "type"}.issubset(
                self.verified_operations
            )
            result["verified_operations"] = sorted(self.verified_operations)
            return result

    def close(self):
        # Closing is idempotent, but a previous timeout is not proof of exit.
        if not self.closed:
            self.closed = True
            self.requests.put(None)
        self.thread.join(8)
        if self.thread.is_alive() and self.loop and self.main_task:
            try:
                self.loop.call_soon_threadsafe(self.main_task.cancel)
            except RuntimeError:
                pass
            self.thread.join(8)
        if self.thread.is_alive():
            raise BridgeError("execution_state_unknown", "桌面控制连接尚未退出；未启动替代连接。")
