/* Local, bounded file uploads shared by Direct and Guided Research. */
'use strict';
window.createInputUploader = ({ensureGroup, onChange, afterChange=async()=>{}, storageKey=null}) => {
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const s={group:null,files:[],busy:false,error:''};
  const remember=()=>{if(storageKey){try{if(s.group)sessionStorage.setItem(storageKey,s.group);else sessionStorage.removeItem(storageKey);}catch(_){/* Optional local preference. */}}};
  async function response(r){const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:`上传失败 (${r.status})`);return d;}
  function view(){return `${s.files.length?`<div class="input-files">${s.files.map(f=>`<div class="input-file">${f.media_type.startsWith('image/')?`<a href="${esc(f.url)}?preview=true" target="_blank" rel="noopener"><img src="${esc(f.url)}?preview=true" alt="${esc(f.name)}"></a>`:'<span class="input-file-icon">▤</span>'}<div class="input-file-info"><a href="${esc(f.url)}" title="${esc(f.name)}">${esc(f.name)}</a><small>${Math.ceil(f.size_bytes/1024)} KB · ${f.parse_status==='image'?(f.vision_enabled?'图片':'仅预览'):f.parse_status==='text'?(f.extraction_truncated?'部分文字':'已提取文字'):'未提取文字'}</small></div><button type="button" class="input-remove" aria-label="移除 ${esc(f.name)}" data-input-remove="${esc(f.id)}" ${s.busy?'disabled':''}>×</button></div>`).join('')}</div>`:''}${s.busy?'<span class="input-status" role="status">上传中…</span>':''}${s.error?`<span class="input-error" role="alert">${esc(s.error)}</span>`:''}`;}
  async function upload(files){
    if(!files.length||s.busy)return;
    s.busy=true;s.error='';onChange();
    try{
      if(!s.group){const g=await ensureGroup();s.group=g.id;s.files=g.files||[];remember();}
      const body=new FormData();Array.from(files).forEach(f=>body.append('files',f));
      const d=await response(await fetch(`/api/inputs/groups/${s.group}/files`,{method:'POST',body}));
      s.files=d.files;await afterChange();
    }catch(e){s.error=e.message;}finally{s.busy=false;onChange();}
  }
  async function remove(id){
    if(s.busy)return;s.busy=true;s.error='';onChange();
    try{const d=await response(await fetch(`/api/inputs/groups/${s.group}/files/${encodeURIComponent(id)}`,{method:'DELETE'}));s.files=d.files;await afterChange();}
    catch(e){s.error=e.message;}finally{s.busy=false;onChange();}
  }
  return {view,upload,remove,group:()=>s.group,files:()=>s.files,busy:()=>s.busy,
    set:(group,files=[])=>{s.group=group;s.files=files;s.error='';remember();},
    reset:()=>{s.group=null;s.files=[];s.error='';remember();onChange();},
    restore:async()=>{if(!storageKey)return;try{const id=sessionStorage.getItem(storageKey);if(!id)return;const d=await response(await fetch(`/api/inputs/groups/${encodeURIComponent(id)}`));if(d.session_id){sessionStorage.removeItem(storageKey);return;}s.group=d.id;s.files=d.files;onChange();}catch(_){sessionStorage.removeItem(storageKey);}}
  };
};
