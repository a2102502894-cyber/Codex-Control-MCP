"""Candidate Browser adapter over the installed official CUA API only.
No Selenium, Playwright installation, CDP implementation, or model calls.
Unknown or absent official browser hosts are reported, never substituted.
"""

from __future__ import annotations
import json
import uuid
from .computer import OfficialComputer
from .errors import BridgeError

BROWSER_JS = r"""{
 const req=REQUEST;
 if(!globalThis.ccmBrowserPages)globalThis.ccmBrowserPages=new Map();
 if(!globalThis.ccmBrowserObservations)globalThis.ccmBrowserObservations=new Map();
 const pages=globalThis.ccmBrowserPages, observations=globalThis.ccmBrowserObservations;
 const fail=(code,message)=>{let e=new Error(message);e.ccmCode=code;throw e;};
 const checkURL=(value)=>{const u=new URL(value);if(!['http:','https:','file:'].includes(u.protocol)&&value!=='about:blank')fail('invalid_arguments','Unsupported navigation scheme');return value;};
 const observe=async(page_id,tab)=>{
   const state=await tab.getAXState({emit:false,disableDiffing:true});
   if(req.args.screenshot)await tab.getScreenshot();
   const prior=[...observations].filter(([k,v])=>v.page_id===page_id);
   for(const [key] of prior)observations.delete(key);
   observations.set(req.snapshot_id,{tab,page_id,at:Date.now()});
   return {page_id,snapshot_id:req.snapshot_id,observed_at_ms:Date.now(),valid_for_ms:120000,
      accessibility:state,coordinate_space:'official_browser_viewport',screenshot_requested:!!req.args.screenshot};
 };
 try{
   let out;
   if(req.tool==='browser_start'){
     const available=await cua.listBrowsers({emit:false});
     if(!available.length)fail('capability_unavailable','No official browser is available; install and authorize the official Browser plugin and extension.');
     if(req.args.discover_only){out={browsers:available};}
     else{
       const selected=req.args.browser_id?available.find(b=>b.id===req.args.browser_id):null;
       if(req.args.browser_id&&!selected)fail('capability_unavailable','Requested browser was not returned by official discovery.');
       const browser=await cua.getBrowser(selected?{id:selected.id}:{});
       const tab=await cua.createBrowserTab(browser.browserId,checkURL(req.args.url||'about:blank'),{visible:false,sessionName:'Codex-Control-MCP'});
       const page_id=req.page_id;pages.set(page_id,tab);
       out=await observe(page_id,tab);out.browser_id=browser.browserId;
     }
   }else{
     const tab=pages.get(req.args.page_id);
     if(!tab)fail('page_not_owned','This bridge does not own the requested page.');
     if(req.tool==='browser_snapshot'){out=await observe(req.args.page_id,tab);}
     else if(req.tool==='browser_close'){
       await tab.close();pages.delete(req.args.page_id);
       for(const [k,v] of observations)if(v.page_id===req.args.page_id)observations.delete(k);
       out={page_id:req.args.page_id,closed:true};
     }else if(req.tool==='browser_navigate'){
       for(const [k,v] of observations)if(v.page_id===req.args.page_id)observations.delete(k);
       await tab.goto(checkURL(req.args.url));out=await observe(req.args.page_id,tab);
     }else{
       const ob=observations.get(req.args.snapshot_id);
       if(!ob||ob.page_id!==req.args.page_id||Date.now()-ob.at>120000)fail('stale_snapshot','Observe this page again before acting.');
       observations.delete(req.args.snapshot_id);
       if(req.tool==='browser_click'){
         const target=Number.isInteger(req.args.element_index)?req.args.element_index:[req.args.x,req.args.y];
         await tab.click(target);
       }else if(req.tool==='browser_fill'){
         await tab.setValue(req.args.element_index,req.args.text);
       }else if(req.tool==='browser_press'){
         await tab.pressKey(req.args.key);
       }else if(req.tool==='browser_scroll'){
         const target=Number.isInteger(req.args.element_index)?req.args.element_index:[req.args.x,req.args.y];
         await tab.scroll(target,req.args.direction,req.args.pages||1);
       }else fail('capability_unavailable','Unknown Browser adapter action.');
       out=await observe(req.args.page_id,tab);
     }
   }
   nodeRepl.write('CCM_RESULT='+JSON.stringify(out));
 }catch(e){nodeRepl.write('CCM_RESULT='+JSON.stringify({error:String(e.message||e),error_code:e.ccmCode||'execution_state_unknown'}));}
}"""


class OfficialBrowser(OfficialComputer):
    surface = "browser"
    backend = "official_cua_repl -> @oai/browser-desktop"

    def call(self, tool, args, forwarder=None):
        if tool not in {
            "browser_start",
            "browser_snapshot",
            "browser_click",
            "browser_fill",
            "browser_press",
            "browser_scroll",
            "browser_navigate",
            "browser_close",
        }:
            raise BridgeError("capability_unavailable", "Unknown Browser tool")
        if tool in {"browser_click", "browser_scroll"}:
            indexed = type(args.get("element_index")) is int
            coordinates = type(args.get("x")) in (int, float) and type(
                args.get("y")
            ) in (int, float)
            if indexed == coordinates:
                raise BridgeError(
                    "invalid_arguments",
                    "Supply one element index or one coordinate pair",
                )
        with self.action_lock:
            request = {
                "tool": tool,
                "args": args,
                "snapshot_id": "br_" + uuid.uuid4().hex,
                "page_id": "page_" + uuid.uuid4().hex,
            }
            code = BROWSER_JS.replace(
                "REQUEST", json.dumps(request, ensure_ascii=True), 1
            )
            out = self._call(code, forwarder)
            if out.get("snapshot_id"):
                self.verified_operations.add("snapshot")
            if tool not in {"browser_start", "browser_snapshot"}:
                self.verified_operations.add(tool.removeprefix("browser_"))
            self.verified = {"snapshot", "click", "fill"}.issubset(
                self.verified_operations
            )
            out["verified_operations"] = sorted(self.verified_operations)
            return out
