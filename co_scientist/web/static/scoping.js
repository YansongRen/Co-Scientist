/* Independent pre-research UI, sharing the existing launch dialog. No build step. */
'use strict';
window.createScopingUI = ({onChange, onLaunch}) => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const sections = {background:'研究背景',objectives:'研究目标',known_facts:'已知信息与已有工作',directions:'值得探索的方向',constraints:'研究约束',open_questions:'尚待确认的问题',tasks:'研究任务'};
  const sources = {user_confirmed:'用户确认',ai_suggestion:'AI 建议',unconfirmed:'待确认'};
  const s = {draft:null, drafts:[], busy:false, error:'', editing:false, input:'', pending:null, model:'Mock',
    limits:{budget_usd:1,budget_tokens:60000,wall_clock_seconds:600},caps:{budget_usd:1,max_tokens:60000},limitsDirty:false};
  async function api(url, method='GET', body) {
    const response = await fetch(url, {method, headers:{'Content-Type':'application/json'}, cache:'no-store', ...(body ? {body:JSON.stringify(body)} : {})});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败（${response.status}）`);
    return data;
  }
  const changed = () => onChange();
  const syncDraft=d=>{s.draft=d;if(d.limits && Object.keys(d.limits).length)s.limits=d.limits;uploads.set(d.input_group_id,d.attachments||[]);if(d.use_live)s.model=d.calls.at(-1)?.model||'配置模型';};
  const reload = async () => {if (s.draft) syncDraft(await api(`/api/scoping/${s.draft.id}`));};
  const uploads=window.createInputUploader({onChange:changed,afterChange:reload,ensureGroup:async()=>{
    await ensureDraft();return api('/api/inputs/groups','POST',{draft_id:s.draft.id});
  }});
  async function ensureDraft(){
    if(!s.draft){syncDraft(await api('/api/scoping','POST',{limits:s.limits}));history.replaceState(null,'',`#guided/${s.draft.id}`);s.limitsDirty=false;}
    if(s.limitsDirty){syncDraft(await api(`/api/scoping/${s.draft.id}/limits`,'PUT',{limits:s.limits,version:s.draft.version}));s.limitsDirty=false;}
  }
  const parameters=()=>`<details class="scope-parameters"><summary>参数</summary><div class="scope-parameter-fields"><label>预算 (USD)<input data-scope-limit="budget_usd" aria-label="引导预算" type="number" min="0.001" max="${s.caps.budget_usd}" step="0.001" value="${s.limits.budget_usd}"></label><label>Token 上限<input data-scope-limit="budget_tokens" aria-label="引导 Token 上限" type="number" min="1000" max="${s.caps.max_tokens}" step="1" value="${s.limits.budget_tokens}"></label><label>运行时限 (秒)<input data-scope-limit="wall_clock_seconds" aria-label="引导运行时限" type="number" min="1" max="${s.caps.wall_clock_seconds||7200}" value="${s.limits.wall_clock_seconds}"></label><button type="button" class="secondary-button" data-scope="save-limits">保存</button></div></details>`;
  let loadSeq=0, pollTimer;
  async function load(id) {
    const seq=++loadSeq;
    clearTimeout(pollTimer);
    s.busy=true; s.error=''; changed();
    try {
      const draft=await api(`/api/scoping/${encodeURIComponent(id)}`);
      if(seq!==loadSeq)return;
      syncDraft(draft);s.limitsDirty=false; s.editing=Boolean(s.draft.brief); s.input=''; s.pending=null;
      if(draft.busy)pollTimer=setTimeout(()=>load(id),1500);
    }
    catch(e) {s.error=e.message;}
    finally {if(seq===loadSeq){s.busy=Boolean(s.draft?.busy); changed();}}
  }
  async function list() {
    try {const d=await api('/api/scoping'); s.drafts=d.drafts;s.model=d.model;s.caps=d.ceilings;if(!s.draft && !s.limitsDirty)s.limits=d.defaults; changed();}
    catch(e) {s.error=e.message; changed();}
  }
  function editor() {
    const b=s.draft.brief;
    return `<section class="brief-preview"><form id="brief-editor"><label>研究主题<input name="topic" maxlength="300" required value="${esc(b.topic)}"></label><label>核心科学问题<textarea name="core_question" maxlength="3000" required>${esc(b.core_question)}</textarea></label>${Object.entries(sections).map(([key,label])=>`<fieldset><legend>${label}</legend>${b[key].map((c,i)=>`<div class="brief-claim"><select aria-label="${label} ${i+1} 信息来源" data-claim-source="${key}:${i}">${Object.entries(sources).map(([v,l])=>`<option value="${v}" ${v===c.source?'selected':''}>${l}</option>`).join('')}</select><textarea aria-label="${label} ${i+1}" data-claim="${key}:${i}" maxlength="3000">${esc(c.text)}</textarea><button type="button" class="scope-text-button" data-scope="remove" data-section="${key}" data-index="${i}">删除</button></div>`).join('')}<button type="button" class="scope-text-button" data-scope="add" data-section="${key}">＋ 添加</button></fieldset>`).join('')}</form><div class="scope-actions"><button class="secondary-button" data-scope="save" ${s.busy?'disabled':''}>保存修改</button><button class="secondary-button" data-scope="chat" ${s.busy?'disabled':''}>返回对话</button><button class="primary-button" data-scope="launch" ${s.busy || s.draft.linked_session_id?'disabled':''}>确认并开始研究 →</button></div></section>`;
  }
  function view() {
    const d=s.draft;
    return `<section class="scoping-view" aria-busy="${s.busy}"><div class="scope-header"><button class="scope-text-button" data-entry="choose">← 返回</button>${d?`<button class="scope-text-button" data-scope="new" ${s.busy?'disabled':''}>新建讨论</button>`:''}<button class="scope-text-button" data-entry="direct">切换直接研究</button></div><h1>${s.editing && d?.brief ? '研究任务书' : '梳理思路'}</h1>${d?`<div class="scope-status">${d.use_live?esc(s.model):'Mock'}<span>${d.usage.tokens} tokens · ${Number(d.usage.run_seconds||0).toFixed(1)} 秒 · $${Number(d.usage.cost_usd).toFixed(4)}</span></div>`:''}
      ${d?.linked_session_id?`<div class="scope-notice">任务书已确认。<button class="scope-text-button" data-session="${esc(d.linked_session_id)}">打开正式研究 →</button><button class="scope-text-button" data-scope="new">新建独立讨论</button></div>`:''}
      ${s.error?`<div class="scope-error" role="alert">${esc(s.error)} <button data-scope="retry" class="scope-text-button" ${s.busy?'disabled':''}>重新生成</button></div>`:''}
      ${s.editing && d?.brief ? editor() : `<div class="scope-messages" aria-live="polite">${d?.messages.map(m=>`<article class="scope-message ${m.role}"><div class="scope-role">${m.role==='user'?'你':'研究思路助手'}</div><div class="scope-content">${esc(m.content)}</div></article>`).join('') || ''}${s.busy?'<div class="scope-loading" role="status">正在整理思路<span class="loading-line"></span></div>':''}</div>${!d?.linked_session_id?`<form id="scope-chat-form" class="scope-composer">${uploads.view()}<textarea id="scope-input" aria-label="你的研究想法" placeholder="输入研究想法…" maxlength="4000" rows="3" ${s.busy?'disabled':''}>${esc(s.input)}</textarea><input type="file" id="scope-files" hidden multiple accept=".pdf,.docx,.txt,.md,.csv,.json,.png,.jpg,.jpeg,.webp"><div class="scope-actions"><button type="button" class="scope-text-button" data-scope="attach" aria-label="上传附件" ${s.busy||uploads.busy()?'disabled':''}>＋</button>${parameters()}<button type="button" class="scope-text-button" data-scope="brief" ${s.busy || !d?.messages.length?'disabled':''}>生成研究任务书</button>${d?.brief?'<button type="button" class="scope-text-button" data-scope="preview">查看任务书</button>':''}<button type="button" class="scope-text-button" data-scope="regenerate" ${s.busy || !d?.messages.length?'disabled':''}>重新生成</button><button type="submit" class="primary-button" ${s.busy||uploads.busy()?'disabled':''}>发送 →</button></div></form>`:''}`}
      ${!d && s.drafts.length?`<div class="scope-history"><h3>讨论记录</h3>${s.drafts.map(x=>`<button class="scope-draft-link" data-scope-draft="${esc(x.id)}"><span>${esc(x.title)} · ${x.linked_session_id?'已确认':'讨论中'}</span><small>${esc(x.updated_at.slice(0,16).replace('T',' '))}</small></button>`).join('')}</div>`:''}
      </section>`;
  }
  function collect() {
    if (!s.draft?.brief || !document.querySelector('#brief-editor')) return;
    const b=s.draft.brief;
    b.topic=document.querySelector('[name="topic"]').value;
    b.core_question=document.querySelector('[name="core_question"]').value;
    document.querySelectorAll('[data-claim]').forEach(el=>{const [key,i]=el.dataset.claim.split(':'); b[key][i].text=el.value;});
    document.querySelectorAll('[data-claim-source]').forEach(el=>{const [key,i]=el.dataset.claimSource.split(':'); b[key][i].source=el.value;});
  }
  async function operate(operation, text='') {
    if (s.busy || s.draft?.busy || uploads.busy()) return;
    s.busy=true; s.error=''; s.pending={operation,text}; s.input=''; changed();
    try {
      await ensureDraft();
      syncDraft(await api(`/api/scoping/${s.draft.id}/turns`,'POST',{operation,text,version:s.draft.version,request_id:crypto.randomUUID()}));
      s.pending=null; s.editing=operation==='brief';
    } catch(e) {s.error=e.message; await reload().catch(()=>{});}
    finally {s.busy=false; changed(); if (s.editing)document.querySelector('.brief-preview')?.scrollIntoView({block:'start'}); else document.querySelector('#scope-chat-form')?.scrollIntoView({block:'end'});}
  }
  async function save() {
    collect();
    syncDraft(await api(`/api/scoping/${s.draft.id}/brief`,'PUT',{brief:s.draft.brief,version:s.draft.version}));
  }
  async function action(name, el) {
    if (s.busy || uploads.busy()) return;
    if(name==='attach')return document.querySelector('#scope-files').click();
    if(name==='save-limits'){s.busy=true;try{await ensureDraft();s.error='';}catch(e){s.error=e.message;}finally{s.busy=false;changed();}return;}
    if (['brief','regenerate'].includes(name)) return operate(name);
    if (name==='retry') {
      if(s.pending?.text && !s.draft?.messages.some(m=>m.role==='user' && m.content===s.pending.text))return operate('message',s.pending.text);
      if (s.draft?.messages.length) return operate(s.pending?.operation==='brief'?'brief':'regenerate');
      if (s.pending?.text) return operate('message',s.pending.text);
      return;
    }
    collect(); s.error='';
    try {
      if (name==='new') {if(s.editing && !s.draft?.linked_session_id)await save();s.draft=null;uploads.reset();s.limitsDirty=false; s.editing=false; s.input=''; history.replaceState(null,'','#guided'); await list();}
      if (name==='add') {s.draft.brief[el.dataset.section].push({text:'',source:'unconfirmed',message_ids:[]});}
      if (name==='remove') s.draft.brief[el.dataset.section].splice(Number(el.dataset.index),1);
      if (name==='preview') s.editing=true;
      if (name==='save' || name==='chat' || name==='launch') {
        s.busy=true;
        await save();
        if (name==='chat') s.editing=false;
        if (name==='launch') onLaunch(s.draft);
      }
    } catch(e) {s.error=e.message;}
    finally {s.busy=false; changed();}
  }
  const send=()=>{const text=document.querySelector('#scope-input').value.trim()||(uploads.files().length?'请结合附件帮我梳理研究方向。':'');if(text)operate('message',text);};
  document.addEventListener('submit', e=>{if(e.target.id==='scope-chat-form'){e.preventDefault();send();}});
  document.addEventListener('input', e=>{if(e.target.id==='scope-input')s.input=e.target.value;});
  document.addEventListener('keydown', e=>{if(e.target.id==='scope-input' && e.key==='Enter' && !e.shiftKey && !e.isComposing){e.preventDefault();send();}});
  document.addEventListener('input',e=>{if(e.target.dataset.scopeLimit){s.limits[e.target.dataset.scopeLimit]=Number(e.target.value);s.limitsDirty=true;}});
  document.addEventListener('change', e=>{if(e.target.id==='scope-files')uploads.upload(Array.from(e.target.files));if(e.target.closest('#brief-editor'))collect();});
  document.addEventListener('paste',e=>{if(e.target.id==='scope-input' && e.clipboardData?.files.length){e.preventDefault();uploads.upload(Array.from(e.clipboardData.files));}});
  document.addEventListener('dragover',e=>{if(e.target.closest('#scope-chat-form'))e.preventDefault();});
  document.addEventListener('drop',e=>{if(e.target.closest('#scope-chat-form')){e.preventDefault();uploads.upload(Array.from(e.dataTransfer.files));}});
  document.addEventListener('click', e=>{
    const el=e.target.closest('[data-scope]'); if(el)action(el.dataset.scope,el);
    const remove=e.target.closest('[data-input-remove]');if(remove?.closest('.scoping-view'))uploads.remove(remove.dataset.inputRemove);
    const draft=e.target.closest('[data-scope-draft]'); if(draft){location.hash=`guided/${draft.dataset.scopeDraft}`;}
  });
  return {view,load,list,collect,draftId:()=>s.draft?.id,hasDraft:()=>Boolean(s.draft),isBusy:()=>s.busy||uploads.busy(),
    leave:async()=>{collect();if(s.editing && s.draft?.brief && !s.draft.linked_session_id)await save();},
    confirm:async options=>{
      collect();
      if(!s.draft?.brief)throw new Error('请先生成研究任务书');
      const result=await api(`/api/scoping/${s.draft.id}/confirm`,'POST',{brief:s.draft.brief,version:s.draft.version,confirmed:true,options});
      await reload(); return result;
    }};
};
