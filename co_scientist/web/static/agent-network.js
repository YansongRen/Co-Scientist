/* Execution-backed graph. No fabricated transfers or forever-looping particles. */
(() => {
  const NS='http://www.w3.org/2000/svg';
  const nodes={generation:[130,100,'Generation'],reflection:[400,60,'Reflection'],ranking:[670,100,'Ranking'],evolution:[670,310,'Evolution'],metareview:[400,350,'Meta-review'],proximity:[130,310,'Proximity'],supervisor:[400,205,'Supervisor']};
  const edges=[['generation','reflection'],['reflection','ranking'],['ranking','evolution'],['evolution','reflection'],['generation','proximity'],['evolution','proximity'],['metareview','generation'],['metareview','evolution'],['proximity','ranking']];
  function path(a,b){const [x,y]=nodes[a],[u,v]=nodes[b];return `M${x},${y} Q400,205 ${u},${v}`;}
  function element(tag,attrs){const e=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs)) e.setAttribute(k,v);return e;}
  function mount(host,sid){
    for(const t of host._timers||[])clearTimeout(t);
    host.dataset.networkSession=sid;host._replaying=false; host._seen=new Set(); host._ready=false;host._timers=[];
    host.innerHTML='<section class="network-panel"><div class="network-heading"><strong>协作网络</strong><span><span class="network-status"></span> <button type="button" class="network-replay">回放</button></span></div><svg viewBox="0 0 800 410" role="group" aria-label="Agent 实时协作网络"></svg><div class="network-transfer" role="status"></div></section>';
    host.querySelector('.network-replay').addEventListener('click',()=>{
      if(host._replaying)return;
      host._replaying=true;
      host.querySelector('.network-status').textContent='历史回放';
      host.querySelector('.network-replay').disabled=true;
      const records=(host._records||[]).slice(-40);
      records.forEach((e,i)=>host._timers.push(setTimeout(()=>{if(host.isConnected)pulse(host,e,true);},i*450)));
      host._timers.push(setTimeout(()=>{host._replaying=false;host.querySelector('.network-status').textContent='历史记录';host.querySelector('.network-replay').disabled=false;},records.length*450+1600));
    });
    const svg=host.querySelector('svg');
    for(const [a,b] of edges){const edge=element('path',{d:path(a,b),class:'network-edge'});const title=element('title',{});title.textContent='依赖或反馈关系；仅红点表示新任务交接';edge.append(title);svg.append(edge);}
    for(const key of Object.keys(nodes))if(key!=='supervisor') svg.append(element('path',{d:path('supervisor',key),class:'network-spoke'}));
    for(const [key,[x,y,label]] of Object.entries(nodes)){
      const g=element('g',{'data-node':key,transform:`translate(${x},${y})`,class:'network-node'});
      if(key!=='supervisor'){g.dataset.agent=key;g.setAttribute('role','button');g.setAttribute('tabindex','0');g.setAttribute('aria-label',`查看 ${label} 动态`);g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();g.dispatchEvent(new MouseEvent('click',{bubbles:true}));}});}
      g.append(element('circle',{r:key==='supervisor'?36:31,class:'network-halo'}),element('circle',{r:key==='supervisor'?29:24,class:'network-core'}));
      const t=element('text',{y:47,'text-anchor':'middle',class:'network-label'});t.textContent=label;g.append(t);
      const n=element('text',{y:4,'text-anchor':'middle',class:'network-count'});g.append(n);svg.append(g);
    }
  }
  function pulse(host,transfer,replay=false){
    const a=transfer.source_agent,b=transfer.destination_agent;
    if(!Object.hasOwn(nodes,a)||!Object.hasOwn(nodes,b)||a===b) return;
    const status=host.querySelector('.network-transfer');
    status.textContent=`${replay?'回放 · ':''}${nodes[a][2]} → ${nodes[b][2]} · 已入队`;
    status.title=[transfer.source_task_id,transfer.destination_task_id,transfer.hypothesis_id].filter(Boolean).join(' → ');
    if(matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const svg=host.querySelector('svg'),p=element('path',{d:path(a,b),fill:'none',class:'network-travel'}),dot=element('circle',{r:4,class:'network-particle'});
    svg.append(p,dot);const length=p.getTotalLength(),start=performance.now();
    const tick=now=>{if(!dot.isConnected)return;const f=Math.min(1,(now-start)/1500),point=p.getPointAtLength(f*length);dot.setAttribute('cx',point.x);dot.setAttribute('cy',point.y);if(f<1)requestAnimationFrame(tick);else{dot.remove();p.remove();}};requestAnimationFrame(tick);
  }
  window.AgentNetwork={update(host,r,sid,disconnected=false){
    if(!r) return;
    if(host.dataset.networkSession!==sid)mount(host,sid);
    const live=r.session_status==='running'&&!disconnected;
    const all=[...(r.agents||[]),r.coordinator].filter(Boolean);
    for(const a of all){const g=host.querySelector(`[data-node="${a.agent}"]`);if(!g)continue;const count=live?(a.active_tasks||[]).length:0;g.classList.toggle('active',live&&a.status==='running');g.querySelector('.network-count').textContent=count||'·';g.setAttribute('aria-label',`${nodes[a.agent][2]} · ${live?a.status:r.session_status} · ${count} 个执行任务`);}
    host._records=r.handoffs||[];
    host.querySelector('.network-replay').disabled=r.session_status==='running'||host._replaying||!host._records.length;
    const active=live?(r.execution?.active_agents||0):0;
    host.querySelector('.network-status').textContent=disconnected?'连接中断':live?`${active} 个 Agent 执行中`:(host._replaying?'历史回放':r.session_status==='paused'?'已暂停':'历史记录');
    for(const e of r.handoffs||[]){if(!host._seen.has(e.id)&&host._ready&&live&&r.server_time-e.ts<8000)pulse(host,e);host._seen.add(e.id);}
    // Initial history establishes a cursor, not fake live animation.
    host._ready=true;
    if(host._seen.size>240)host._seen=new Set((r.handoffs||[]).map(e=>e.id));
    if(!live&&!host._replaying)host.querySelectorAll('.network-particle,.network-travel').forEach(e=>e.remove());
    if(!host.querySelector('.network-transfer').textContent)host.querySelector('.network-transfer').textContent=(r.handoffs||[]).length?'交接记录已载入':'尚无交接记录';
  }};
})();
