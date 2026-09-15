// Deterministic Playwright adapter for the owner's installed Tabbit.
// The parent assigns this worker to its process Job before sending configure.
const readline = require('node:readline');
const { chromium } = require(process.argv[2]);
const pwVersion = require(process.argv[2] + '/package.json').version;
const crypto = require('node:crypto');
let cfg, context;
const pages = new Map(), observations = new Map();
const verified = new Set();
const required = ['start','snapshot','click','fill','press','scroll','navigate','close'];
const id = prefix => prefix + crypto.randomUUID().replaceAll('-', '');
const fail = (code, message) => { const e = new Error(message); e.bridgeCode=code; throw e; };
function checkURL(value) {
  let url;
  try { url = new URL(value); } catch { fail('invalid_arguments','Invalid browser URL.'); }
  if (!['http:','https:','file:'].includes(url.protocol) && value!=='about:blank') fail('invalid_arguments','Unsupported browser URL scheme.');
  if (url.username || url.password) fail('invalid_arguments','Credentials in browser URLs are not supported.');
  return value;
}
function invalidate(pageId) {
  const old = observations.get(pageId);
  observations.delete(pageId);
  if (old) for (const row of old.elements) row.handle.dispose().catch(()=>{});
}
function register(page) {
  for (const [key, value] of pages) if(value.page===page) return key;
  const pageId=id('page_');
  const state={page, revision:0};
  pages.set(pageId,state);
  page.on('framenavigated',()=>{state.revision++; invalidate(pageId);});
  page.on('close',()=>{invalidate(pageId); pages.delete(pageId);});
  // Dialogs are reported to the caller and dismissed; never auto-accept a site action.
  page.on('dialog', async dialog=>{state.lastDialog={type:dialog.type(),message:dialog.message().slice(0,500)}; await dialog.dismiss().catch(()=>{});});
  return pageId;
}
async function launch() {
  if (context) return;
  context = await chromium.launchPersistentContext(cfg.profile_directory, {
    executablePath:cfg.executable_path, headless:cfg.headless, chromiumSandbox:true,
    viewport:{width:1280,height:800}, timeout:cfg.timeout_ms,
    args:['--no-first-run','--no-default-browser-check'],
    ...(cfg.proxy ? {proxy:cfg.proxy}:{}), acceptDownloads:false
  });
  context.setDefaultTimeout(cfg.timeout_ms);
  context.setDefaultNavigationTimeout(cfg.timeout_ms);
  context.on('close',()=>{context=null; for(const pageId of observations.keys()) invalidate(pageId); pages.clear();});
  context.on('page', page=>{
    if (pages.size>=cfg.max_pages) {page.close().catch(()=>{}); return;}
    register(page);
  });
  // This is a dedicated profile; restore no pages from an earlier bridge run.
  for (const page of context.pages()) await page.close();
}
async function shutdown() {
  const previous=context;
  context=null;
  for (const pageId of observations.keys()) invalidate(pageId);
  pages.clear();
  if(previous) await previous.close();
}
function detail(el) {
  const rect=el.getBoundingClientRect(), style=getComputedStyle(el);
  const visible=rect.width>0 && rect.height>0 && style.visibility!=='hidden' && style.display!=='none';
  const labelled=(el.getAttribute('aria-labelledby')||'').split(/\s+/).map(k=>el.ownerDocument.getElementById(k)?.textContent||'').join(' ').trim();
  const name=el.getAttribute('aria-label') || labelled || Array.from(el.labels||[]).map(x=>x.innerText).join(' ') || el.innerText || el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt') || '';
  return {connected:el.isConnected,visible,tag:el.tagName.toLowerCase(),role:el.getAttribute('role')||'',
    type:el.getAttribute('type')||'',name:name.replace(/\s+/g,' ').trim().slice(0,300),
    disabled:!!el.disabled,readonly:!!el.readOnly,scroll_x:el.scrollLeft,scroll_y:el.scrollTop,
    value:el.type==='password'?'[redacted]':('value' in el?String(el.value).slice(0,500):null)};
}
function fingerprint(meta) { return JSON.stringify([meta.tag,meta.role,meta.type,meta.name,meta.disabled,meta.readonly]); }
async function observe(pageId, screenshot) {
  for(let attempt=0;attempt<3;attempt++) {
    try {return await observeOnce(pageId,screenshot);}
    catch(e) {
      if(e.bridgeCode!=='stale_snapshot'||attempt===2) throw e;
      // Only repeat observation when a frame navigates; never repeat the action.
      await new Promise(resolve=>setTimeout(resolve,100));
    }
  }
}
async function observeOnce(pageId, screenshot) {
  const state=pages.get(pageId);
  if(!state || state.page.isClosed()) fail('page_not_owned','This bridge does not own the page.');
  invalidate(pageId);
  const {page}=state, revision=state.revision;
  const rows=[], frames=[], texts=[];
  let truncated=false;
  const selectedFrames=page.frames().slice(0,8);
  if(page.frames().length>8) truncated=true;
  try {
    for (let frameIndex=0;frameIndex<selectedFrames.length;frameIndex++) {
      const frame=selectedFrames[frameIndex];
      let accessibility='', text='';
      try {
        accessibility=(await frame.locator('body').ariaSnapshot({timeout:1000})).slice(0,40000);
        text=(await frame.locator('body').innerText({timeout:1000})).slice(0,30000);
      } catch { /* A loading or detached frame may not have a body yet. */ }
      frames.push({frame_index:frameIndex,url:frame.url(),accessibility}); texts.push(text);
      const handles=await frame.locator('a[href],button,input:not([type=hidden]),textarea,select,[role],[contenteditable=true],[tabindex]').elementHandles();
      for (const handle of handles) {
        if(rows.length>=250) {truncated=true; await handle.dispose(); continue;}
        let meta, box;
        try {meta=await handle.evaluate(detail); box=await handle.boundingBox();} catch {await handle.dispose(); continue;}
        if(!meta.connected || !meta.visible || !box) {await handle.dispose(); continue;}
        rows.push({handle,meta,box,frame_index:frameIndex,fingerprint:fingerprint(meta)});
      }
    }
    if(state.revision!==revision || page.isClosed()) fail('stale_snapshot','The page navigated while being observed; take another snapshot.');
    const snapshotId=id('br_');
    observations.set(pageId,{id:snapshotId,revision,at:Date.now(),elements:rows});
    const output={page_id:pageId,snapshot_id:snapshotId,observed_at_ms:Date.now(),valid_for_ms:120000,
      url:page.url(),title:await page.title(),viewport:page.viewportSize(),
      scroll_position:await page.evaluate(()=>({x:scrollX,y:scrollY})),
      coordinate_space:'tabbit_viewport_css_pixels',accessibility:frames[0]?.accessibility||'',
      text:texts.join('\n').slice(0,60000),frames:frames.map(f=>({...f,accessibility:f.accessibility.slice(0,12000)})),
      elements:rows.map((row,index)=>({element_index:index,frame_index:row.frame_index,...row.meta,box:row.box})),
      truncated,owned_pages:Array.from(pages,([key,val])=>({page_id:key,url:val.page.url()})),
      last_dialog:state.lastDialog||null,screenshot_requested:!!screenshot};
    if(screenshot) output._images=[{type:'image',mimeType:'image/jpeg',data:(await page.screenshot({type:'jpeg',quality:70,timeout:10000})).toString('base64')}];
    if(state.revision!==revision || page.isClosed()) fail('stale_snapshot','The page navigated before observation completed.');
    verified.add('snapshot');
    return output;
  } catch(e) {
    for(const row of rows) await row.handle.dispose().catch(()=>{});
    observations.delete(pageId);
    throw e;
  }
}
async function actionable(pageId, args) {
  const state=pages.get(pageId), observation=observations.get(pageId);
  if(!state || !observation || observation.id!==args.snapshot_id || Date.now()-observation.at>120000 || observation.revision!==state.revision)
    fail('stale_snapshot','Observe this page again before acting.');
  let row;
  if(Number.isInteger(args.element_index)) {
    row=observation.elements[args.element_index];
    if(!row) fail('invalid_arguments','Element index is absent from this snapshot.');
    let meta;
    try {meta=await row.handle.evaluate(detail);} catch {fail('stale_snapshot','The observed element was detached.');}
    if(!meta.connected || !meta.visible || fingerprint(meta)!==row.fingerprint) fail('stale_snapshot','The observed element changed; take another snapshot.');
  }
  observations.delete(pageId); // Every action consumes the snapshot, including failures.
  return {state,observation,row};
}
function point(args, viewport) {
  if(!Number.isFinite(args.x)||!Number.isFinite(args.y)||args.x<0||args.y<0||args.x>=viewport.width||args.y>=viewport.height)
    fail('invalid_arguments','Coordinates must be inside the observed viewport.');
  return {x:args.x,y:args.y};
}
function key(value) {
  const aliases={CTRL:'Control',CONTROL:'Control',ALT:'Alt',SHIFT:'Shift',CMD:'Meta',META:'Meta',ENTER:'Enter',RETURN:'Enter',ESC:'Escape',ESCAPE:'Escape',TAB:'Tab',BACKSPACE:'Backspace',DELETE:'Delete',UP:'ArrowUp',DOWN:'ArrowDown',LEFT:'ArrowLeft',RIGHT:'ArrowRight',SPACE:'Space'};
  return value.split('+').map(part=>aliases[part.toUpperCase()]||part).join('+');
}
async function dispatch(tool,args) {
  if(tool==='configure') {cfg=args; return {playwright_version:pwVersion,browser_id:'tabbit',transport:'playwright_pipe'};}
  if(tool==='shutdown') {await shutdown(); return {closed:true};}
  let output;
  if(tool==='browser_start') {
    if(args.browser_id && args.browser_id!=='tabbit') fail('capability_unavailable','This backend is configured for Tabbit.');
    if(args.discover_only) return {browsers:[{id:'tabbit',name:'Tabbit',executable_path:cfg.executable_path,backend:'playwright'}]};
    const url=checkURL(args.url||'about:blank');
    await launch();
    if(pages.size>=cfg.max_pages) fail('resource_limit','The bridge has reached its owned page limit.');
    const page=await context.newPage(), pageId=register(page);
    try {await page.goto(url,{waitUntil:'domcontentloaded'}); output=await observe(pageId,args.screenshot);}
    catch(e) {await page.close().catch(()=>{}); throw e;}
    output.browser_version=context.browser()?.version();
    verified.add('start');
  } else {
    const state=pages.get(args.page_id);
    if(!state) fail('page_not_owned','This bridge does not own the requested page.');
    const page=state.page;
    if(tool==='browser_snapshot') output=await observe(args.page_id,args.screenshot);
    else if(tool==='browser_close') {
      invalidate(args.page_id); await page.close();
      if(pages.size===0) await shutdown();
      output={page_id:args.page_id,closed:true}; verified.add('close');
    } else if(tool==='browser_navigate') {
      const url=checkURL(args.url); invalidate(args.page_id);
      await page.goto(url,{waitUntil:'domcontentloaded'});
      output=await observe(args.page_id,args.screenshot); verified.add('navigate');
    } else {
      const {observation,row}=await actionable(args.page_id,args);
      try {
        if(tool==='browser_click') {
          if(row) await row.handle.click(); else {const p=point(args,page.viewportSize()); await page.mouse.click(p.x,p.y);}
        } else if(tool==='browser_fill') {
          if(!row) fail('invalid_arguments','Fill requires an observed element index.');
          await row.handle.fill(args.text);
        } else if(tool==='browser_press') await page.keyboard.press(key(args.key));
        else if(tool==='browser_scroll') {
          let p;
          if(row) {const b=await row.handle.boundingBox(); if(!b) fail('stale_snapshot','Scroll target is no longer visible.'); p={x:Math.max(0,Math.min(1279,b.x+b.width/2)),y:Math.max(0,Math.min(799,b.y+b.height/2))};}
          else p=point(args,page.viewportSize());
          await page.mouse.move(p.x,p.y);
          const distance=(args.pages||1)*(args.direction==='left'||args.direction==='right'?1280:800);
          await page.mouse.wheel(args.direction==='left'?-distance:args.direction==='right'?distance:0,args.direction==='up'?-distance:args.direction==='down'?distance:0);
          await page.waitForTimeout(100);
        } else fail('capability_unavailable','Unknown Browser action.');
        verified.add(tool.slice('browser_'.length));
      } finally {for(const item of observation.elements) await item.handle.dispose().catch(()=>{});}
      output=await observe(args.page_id,args.screenshot);
    }
  }
  return {...output,browser_id:'tabbit',verified_operations:[...verified].sort(),full_action_chain_verified:required.every(k=>verified.has(k))};
}
const lines=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
(async()=>{
  for await (const line of lines) {
    let request;
    try {
      request=JSON.parse(line);
      const result=await dispatch(request.tool,request.args||{});
      process.stdout.write(JSON.stringify({id:request.id,result})+'\n');
      if(request.tool==='shutdown') break;
    } catch(e) {
      const code=e.bridgeCode||'execution_state_unknown';
      const message=e.bridgeCode?e.message:`Tabbit action failed (${e.name||'Error'}); inspect the page before retrying.`;
      process.stdout.write(JSON.stringify({id:request?.id,error:{code,message}})+'\n');
    }
  }
  await shutdown(); lines.close();
})().catch(()=>{process.exitCode=1;});
