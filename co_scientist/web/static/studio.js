/* Local research workspace. GETs only until the user submits the launch form. */
(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const defaults = window.STUDIO_DEFAULTS || {budget: 1, wall_clock: 600};
  const state = {sessions: [], data: null, id: null, view: 'research', entry:'choose', launchDraft:null, loading: 0, timer: null,
    runtime: null, runtimeTimer: null, runtimeError: false, selectedAgent: null, evidenceFilter:'all', evidenceHyp:'all'};
  const scoping = window.createScopingUI({onChange:()=>{if(!state.id && state.entry==='guided')render();},onLaunch:draft=>{
    state.launchDraft=draft;
    $('#launch-question').textContent=draft.brief.core_question;
    $('#launch-error').textContent='';
    applyLaunchDefaults();$('#launch-submit').disabled=false;
    launchPolicy(); $('#launch-dialog').showModal();
  }});
  const directInputs=window.createInputUploader({storageKey:'co-scientist-direct-inputs',onChange:()=>{
    $('#direct-attachments').innerHTML=directInputs.view();inputChanged();
  },ensureGroup:async()=>{
    const r=await fetch('/api/inputs/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();if(!r.ok)throw Error(d.detail||'无法创建附件组');return d;
  }});
  function applyLaunchDefaults(){
    let saved={};try{saved=JSON.parse(localStorage.getItem('co-scientist-launch-limits')||'{}');}catch(_){/* Optional preferences. */}
    saved={...saved,...(state.launchOptions||{})};
    const values={budget_usd:defaults.budget,budget_tokens:defaults.budget_tokens||500000,wall_clock_seconds:defaults.wall_clock,n_initial:2,research_mode:'lightweight',target_molecule:'unspecified',...saved};
    Object.entries(values).forEach(([key,value])=>{if($('#launch-form').elements[key])$('#launch-form').elements[key].value=value;});
  }
  function rememberLaunch(){
    state.launchOptions=Object.fromEntries(new FormData($('#launch-form')));
    try{localStorage.setItem('co-scientist-launch-limits',JSON.stringify(state.launchOptions));}catch(_){/* In-memory settings still work. */}
  }
  const directGoal=()=>$('#question').value.trim()||(directInputs.files().length?'基于上传资料，梳理研究问题并提出可验证的研究假设。':'');
  const agents = {
    generation: ['Generation', '提出研究假设', 'spark', '生成'],
    reflection: ['Reflection', '文献检索与审查', 'search', '审查'],
    ranking: ['Ranking', 'Tournament · Elo 排名', 'activity', '排名'],
    evolution: ['Evolution', '根据反馈迭代', 'grid', '演化'],
    proximity: ['Proximity', '相似性与聚类', 'link', '聚类'],
    metareview: ['Meta-review', '综合研究结论', 'file', '综述'],
  };
  const statusText = value => ({done:'已完成',running:'运行中',active:'运行中',paused:'已暂停',failed:'运行失败',error:'运行失败',pending:'等待中',awaiting_approval:'等待人工审核',unused:'未执行',dead:'失败',stopped:'已停止',aborted:'已终止'}[value] || value || '准备就绪');
  const fmt = value => Number(value || 0).toLocaleString('zh-CN');
  const money = value => `$${Number(value || 0).toFixed(2)}`;
  const date = value => {
    if (!value) return '—';
    const d = new Date(typeof value === 'number' && value < 1e11 ? value * 1000 : value);
    return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString('zh-CN', {month:'2-digit',day:'2-digit'});
  };
  const time = value => {
    if (!value) return '—';
    const d = new Date(typeof value === 'number' && value < 1e11 ? value * 1000 : value);
    return Number.isNaN(d.getTime()) ? '—' : d.toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  };
  function title(goal = '') {
    if (/homopolymer.*nanopore|nanopore.*homopolymer/i.test(goal)) return '纳米孔测序中的同聚物误差';
    return goal.length > 38 ? `${goal.slice(0, 38)}…` : goal || '未命名研究';
  }
  function goalText(goal) {
    if (/^Investigate two distinct, mechanistically specific hypotheses explaining homopolymer/.test(goal)) {
      return '探索纳米孔 DNA 测序中，同聚物长度估计误差的两种具体机制。结合已发表文献，寻找支持与反对证据，并设计基于公开测序数据的验证方案。';
    }
    return goal;
  }
  function hypothesisTitle(h) {
    const translations = {
      hyp_a3ebcba5e95e53ae: '马达驱动的停留时间压缩，导致同聚物长度被系统性低估',
      hyp_846e74db76618809: '解旋酶制动处的停留时间量化失效，导致同聚物长度误判',
    };
    return translations[h.id] || h.title;
  }
  const verdict = v => ({missing_piece:'仍需补充证据',supported:'证据支持',promising:'值得探索',reject:'建议重新评估',needs_revision:'需要修订',revise:'需要修订',accept:'通过审查'}[v] || v || '等待审查');
  function toast(message) {
    $('#toast').textContent = message;
    $('#toast').classList.add('visible');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => $('#toast').classList.remove('visible'), 2600);
  }
  async function get(url) {
    const response = await fetch(url, {cache:'no-store'});
    if (!response.ok) throw new Error(`读取失败（${response.status}）`);
    return response.json();
  }
  async function refreshSessions() {
    state.sessions = (await get('/api/studio/sessions')).sessions;
    renderSidebar();
  }
  function renderSidebar() {
    $('#session-count').textContent = state.sessions.length;
    $('#session-list').innerHTML = state.sessions.length ? state.sessions.map(s => `<button class="session-item ${s.id === state.id ? 'active' : ''}" data-session="${esc(s.id)}" title="${esc(s.research_goal)}"><span class="session-item-title">${esc(title(s.research_goal))}</span><span class="session-item-meta">${date(s.created_at)}<span class="dot"></span>${statusText(s.status)}<span class="dot"></span>${s.hypothesis_count} 条假设</span></button>`).join('') : '<div class="sidebar-loading">暂无研究</div>';
  }
  async function selectSession(id, {quiet = false} = {}) {
    clearTimeout(state.timer);
    clearTimeout(state.runtimeTimer);
    const seq = ++state.loading;
    if (!quiet) {
      state.id = id;
      state.data = null;
      state.runtime = null; state.runtimeError = false; state.selectedAgent = null; state.evidenceFilter='all'; state.evidenceHyp='all';
      $('#inspector').scrollTop = 0;
      renderSidebar();
      $('#breadcrumb-title').textContent = title(state.sessions.find(s => s.id === id)?.research_goal);
      $('#conversation').innerHTML = '<div class="initial-loading"><span class="loading-line"></span></div>';
    }
    try {
      const data = await get(`/api/studio/sessions/${encodeURIComponent(id)}`);
      if (seq !== state.loading) return;
      state.id = id;
      state.data = data;
      if (!state.runtime || data.runtime.server_time >= state.runtime.server_time) state.runtime = data.runtime;
      if (!quiet && data.session.status === 'running' && state.view === 'research' && !new URLSearchParams(location.search).has('view')) state.view = 'agents';
      const listed = state.sessions.find(s => s.id === id);
      if (listed) {listed.status = data.session.status; listed.hypothesis_count = data.hypotheses.length;}
      const scroll = $('#conversation-scroll').scrollTop;
      const openEvidence = quiet ? [...document.querySelectorAll('[data-evidence-card] details[open]')].map(e=>e.closest('[data-evidence-card]').dataset.evidenceCard) : [];
      render();
      openEvidence.forEach(id=>document.querySelector(`[data-evidence-card="${CSS.escape(id)}"] details`)?.setAttribute('open',''));
      $('#conversation-scroll').scrollTop = quiet ? scroll : 0;
      if (['running','active'].includes(data.session.status)) {
        state.timer = setTimeout(() => selectSession(id, {quiet:true}), 5000);
      }
      if (['running','paused'].includes(data.session.status)) scheduleRuntime(id);
    } catch (error) {
      if (seq !== state.loading) return;
      if (quiet && state.data) {
        state.runtimeError = true; updateMonitor(); scheduleRuntime(id, 5000); return;
      }
      $('#conversation').innerHTML = `<div class="empty-state">无法读取研究记录。${esc(error.message)}<br><button class="secondary-button" data-action="retry">重新连接</button></div>`;
      $('#session-state').textContent = '连接中断';
      toast(error.message);
    }
  }
  function newResearch({navigate=true}={}) {
    ++state.loading;
    clearTimeout(state.timer);
    clearTimeout(state.runtimeTimer);
    state.id = null;
    state.data = null;
    state.runtime = null; state.runtimeError = false; state.selectedAgent = null; state.evidenceFilter='all'; state.evidenceHyp='all';
    state.view = 'research';
    directInputs.reset();
    state.entry = 'choose'; state.launchDraft=null;
    const url = new URL(location.href); url.searchParams.delete('view'); history.replaceState(null, '', url);
    if(navigate)location.hash = 'new';
    inputChanged();
    $('#sidebar').classList.remove('mobile-open');
    render();
    $('#inspector').scrollTop = 0;
  }
  function render() {
    const d = state.data;
    $('.workspace').classList.toggle('monitor-mode', Boolean(d && ['agents','evidence'].includes(state.view)));
    $('#breadcrumb-title').textContent = d ? title(d.session.research_goal) : state.entry==='guided'?'研究思路梳理':'新建研究';
    $('.composer-area').hidden = !d && state.entry !== 'direct';
    $('#view-tabs').hidden = !d;
    $('#hyp-count').textContent = d?.hypotheses.length || 0;
    $('#session-state').textContent = d ? statusText(d.session.status) : '准备就绪';
    document.querySelectorAll('[data-view]').forEach(el => el.setAttribute('aria-selected', String(el.dataset.view === state.view)));
    document.querySelector('[data-action="library"]').classList.toggle('current', state.view === 'sources');
    document.querySelector('[data-action="workspace"]').classList.toggle('current', state.view !== 'sources');
    $('#conversation').innerHTML = !d ? state.entry==='guided'?scoping.view():emptyView() : state.view === 'agents' ? agentsView() : state.view === 'evidence' ? evidenceView(d) : state.view === 'hypotheses' ? hypothesesView(d) : state.view === 'activity' ? activityView(d) : state.view === 'sources' ? sourcesView(d) : researchView(d);
    $('#inspector').hidden = !d;
    document.querySelectorAll('[data-action="inspector"]').forEach(el=>el.hidden=!d);
    $('#inspector-content').innerHTML = inspector(d);
    const model = d?.calls.find(c => c.model)?.model;
    $('#model-label').textContent = model || defaults.model || '配置模型';
    renderSidebar();
  }
  const actions = {
    CreateInitialHypotheses:'提出初始研究假设', GenerateFromFeedback:'根据反馈生成新假设',
    ReviewHypothesis:'审查假设的证据与可行性', AddToTournament:'将假设加入 Tournament',
    RunTournamentBatch:'比较两条假设并更新 Elo', EvolveTopHypotheses:'根据高排名假设与反馈迭代',
    UpdateProximityGraph:'计算嵌入、相似性与聚类', GenerateSystemFeedback:'综合审查并生成系统反馈',
    GenerateFinalResearchOverview:'整理最终研究报告', ParseResearchGoal:'解析研究目标', parse_goal:'解析研究目标',
  };
  const tools = {europe_pmc_search:'检索 Europe PMC', pubmed_search:'检索 PubMed',
    arxiv_search:'检索 arXiv', web_search:'搜索文献与网页', web_fetch:'读取文献网页',
    record_hypothesis:'记录研究假设', record_review:'记录审查结果', record_system_feedback:'记录系统反馈'};
  const runtimeStatus = s => ({running:'执行中',queued:'排队中',paused:'已暂停',idle:'待命',done:'本轮已完成',failed:'存在失败任务',stopped:'已停止',unused:'本轮未触发'}[s] || s);
  const actionName = a => actions[a] || a || '等待任务调度';
  function elapsed(value) {
    const start = typeof value === 'number' ? value : Date.parse(value);
    const seconds = Math.max(0, Math.floor(((state.runtime?.server_time || Date.now()) - start) / 1000));
    if (!Number.isFinite(seconds)) return '—';
    return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds/60)} 分 ${seconds%60} 秒`;
  }
  function currentStep(a) {
    const op = a.operations.find(o => o.phase === 'tool_started') || a.operations[0];
    if (op?.phase === 'tool_started') return `${tools[op.tool] || op.tool}`;
    if (op?.phase === 'llm_retry_wait') return `重试等待 · ${op.delay_seconds} 秒`;
    if (op) return `等待 ${op.model || '模型'} 回复`;
    if (a.step) return a.step.payload.label;
    if (a.active_tasks.length) return actionName(a.active_tasks[0].action);
    if (a.status === 'queued') return '任务已入队，等待 Worker';
    if (a.status === 'paused') return '会话已暂停，等待恢复';
    if (a.status === 'unused') return '本轮没有分配任务';
    if (a.status === 'idle') return a.counts.done ? '当前任务已完成，等待下一步' : '等待 Supervisor 分配任务';
    return a.last_task ? actionName(a.last_task.action) : '暂无执行记录';
  }
  function healthText() {
    if (state.runtimeError) return '连接中断 · 正在重连';
    return ['running','paused'].includes(state.runtime?.session_status) ? '实时更新 · 每 2 秒' : '历史运行记录';
  }
  function agentList(d) {
    const items = state.runtime?.agents || (d?.stages || Object.keys(agents).map(agent => ({agent,status:'unused'})));
    return items.map(a => `<button class="agent-item agent-link ${a.status === 'unused' ? 'unused' : ''}" data-agent="${a.agent}" aria-label="查看 ${agents[a.agent][0]} 动态"><span class="agent-glyph">${icon(agents[a.agent][2])}</span><span class="agent-label"><span class="agent-name">${agents[a.agent][0]}</span><span class="agent-role">${esc(state.runtime ? currentStep({...a,operations:a.operations || [],active_tasks:a.active_tasks || []}) : agents[a.agent][1])}</span></span><span class="agent-indicator">${a.status === 'running' ? '<i class="live-pulse"></i>' : a.status === 'done' ? icon('check') : ''}${['done','running'].includes(a.status) ? '' : d ? runtimeStatus(a.status) : '待命'}</span></button>`).join('');
  }
  function agentsView() {
    return `<div class="monitor-heading"><div><h1 class="view-heading">Agent 动态</h1></div><span class="monitor-health ${state.runtimeError ? 'disconnected' : ''}" id="monitor-health"><i class="connection-dot"></i>${healthText()}</span></div><div id="monitor-content">${monitorContent()}</div>`;
  }
  function eventInfo(e) {
    const p = e.payload || {};
    const titles = {
      task_started:`开始任务 · ${actionName(p.action)}`, task_completed:'任务完成',
      task_failed:p.status === 'pending' ? '任务失败，已重新入队' : '任务执行失败',
      llm_call_started:`请求模型 · ${p.model || ''}`, llm_call_completed:'模型回复已收到',
      llm_call_failed:`模型调用失败 · ${p.error_type || ''}`, llm_call_cancelled:'模型调用已取消',
      llm_retry_wait:`等待重试 · ${p.delay_seconds} 秒`, llm_retry_started:`开始第 ${p.attempt} 次尝试`,
      tool_started:`开始${tools[p.tool] || p.tool || '工具调用'}`,
      tool_completed:`${tools[p.tool] || p.tool || '工具调用'}完成`,
      tool_failed:`${tools[p.tool] || p.tool || '工具调用'}失败`, tool_cancelled:'工具调用已取消',
      agent_recorded:tools[p.tool] || '记录结构化结果', session_started:'Supervisor 启动研究',
      agent_step:p.label || '执行任务步骤',
      session_done:'研究会话结束',
    };
    const details = [p.detail, p.error || p.err || p.error_type, p.strategy ? `演化策略：${({combine:'合并',simplify:'简化',out_of_box:'跨方向探索'})[p.strategy] || p.strategy}` : '', p.mode ? `比较模式：${p.mode}` : '', p.n_items != null ? `${p.n_items} 条假设` : '', p.hypothesis_ids?.length ? `输入：${p.hypothesis_ids.join(' / ')}` : '', p.target ? `审查对象：${p.target}` : '', p.kind ? ({hypothesis_created:'已生成假设',review_completed:'已保存审查意见',tournament_match_complete:'已完成假设比较',final_overview_generated:'已生成最终报告',proximity_updated:'已更新相似性图',system_feedback_generated:'已生成系统反馈'}[p.kind] || p.kind) : '', p.result_count != null ? `返回 ${p.result_count} 条结果` : '', p.input_tokens != null ? `${fmt(p.input_tokens + p.output_tokens)} tokens` : '', p.duration_ms != null ? `耗时 ${(p.duration_ms/1000).toFixed(1)} 秒` : ''].filter(Boolean);
    return {title:titles[e.event] || e.event, detail:details.join(' · ')};
  }
  function monitorContent() {
    const r = state.runtime;
    if (!r) return '<div class="empty-state">正在读取 Agent 状态…</div>';
    if (!state.selectedAgent) state.selectedAgent = r.agents.find(a => a.status === 'running')?.agent || 'generation';
    const a = r.agents.find(item => item.agent === state.selectedAgent) || r.agents[0];
    const coordinator = r.coordinator;
    const cOp = coordinator.operations[0];
    const cLast = coordinator.events.find(e => /^llm_call_(failed|completed|cancelled)$/.test(e.event));
    return `${cOp ? `<div class="coordinator-note">${icon('activity')}<span>Supervisor 正在${esc(cOp.action === 'ParseGoal' ? '解析研究问题' : actionName(cOp.action))} · 等待 ${esc(cOp.model)} 回复</span></div>` : cLast?.event === 'llm_call_failed' && r.session_status === 'running' ? `<div class="coordinator-note">${icon('x')}<span>Supervisor 模型调用失败 · ${esc(cLast.payload.error_type)}。请检查模型配置与后端日志。</span></div>` : ''}<div class="agent-grid">${r.agents.map(item => {
      const op = item.operations.find(o => o.phase === 'tool_started') || item.operations[0];
      const task = item.active_tasks[0];
      const extra = op?.detail || task?.target_title || (item.status === 'failed' ? item.last_task?.last_error : '') || '';
      return `<button class="agent-card ${state.selectedAgent === item.agent ? 'selected' : ''} ${item.status === 'running' ? 'is-running' : ''}" data-agent="${item.agent}" aria-pressed="${state.selectedAgent === item.agent}"><span class="agent-card-head"><span class="agent-glyph">${icon(agents[item.agent][2])}</span><strong>${agents[item.agent][0]}</strong><span class="agent-card-state">${item.status === 'running' ? '<i class="live-pulse"></i>' : ''}${runtimeStatus(item.status)}</span></span><span class="agent-card-step">${esc(currentStep(item))}</span>${extra ? `<span class="agent-card-context">${esc(extra)}</span>` : ''}<span class="agent-card-foot"><span>完成 ${item.counts.done} · 排队 ${item.counts.pending}${item.counts.dead + item.counts.failed ? ` · 失败 ${item.counts.dead + item.counts.failed}` : ''}</span><span>${op ? elapsed(op.started_at) : task?.started_at ? elapsed(task.started_at) : `${item.usage.calls} 次模型调用`}</span></span></button>`;
    }).join('')}</div><section class="agent-focus"><div class="agent-focus-head"><div><h2>${agents[a.agent][0]} <span>· ${runtimeStatus(a.status)}</span></h2></div><span class="agent-focus-usage">${fmt(a.usage.tokens)} tokens<br>估算 ${money(a.usage.cost_usd)}</span></div>${a.active_tasks.map(task => `<div class="active-task"><span>当前任务</span><strong>${esc(actionName(task.action))}</strong><small>${esc(task.target_title || task.target_id || task.id)}${task.attempts ? ` · 第 ${task.attempts+1} 次尝试` : ''}</small></div>`).join('')}${a.operations.map(op => `<div class="operation-line">${icon(op.phase === 'tool_started' ? 'search' : 'activity')}<span>${esc(op.phase === 'tool_started' ? tools[op.tool] || op.tool : op.phase === 'llm_retry_wait' ? `正在等待重试：${op.error_type}` : `等待模型回复：${op.model}`)}${op.detail ? `<small>${esc(op.detail)}</small>` : ''}</span><time>${elapsed(op.started_at)}</time></div>`).join('')}<div class="agent-focus-stats"><span>已完成 <b>${a.counts.done}</b></span><span>排队 <b>${a.counts.pending}</b></span><span>任务重试 <b>${a.retries}</b></span><span>模型调用 <b>${a.usage.calls}</b></span></div><div class="agent-timeline-title">最近步骤 </div><div class="agent-timeline">${a.events.map(e => {const info = eventInfo(e); return `<div class="agent-event ${/failed/.test(e.event) ? 'event-failed' : ''}"><span class="event-pin">${icon(/failed/.test(e.event) ? 'x' : /completed|recorded/.test(e.event) ? 'check' : 'activity')}</span><div><strong>${esc(info.title)}</strong>${info.detail ? `<p>${esc(info.detail)}</p>` : ''}<small>${esc(e.task_id || '')}</small></div><time>${time(e.ts)}</time></div>`;}).join('') || `<div class="empty-state">${a.status === 'unused' ? '本轮没有触发此 Agent。' : !r.instrumented && a.last_task ? '此历史任务没有逐步事件记录；上方显示已保存的任务状态与模型用量。' : '还没有执行步骤，等待任务开始。'}</div>`}</div></section>`;
  }
  function updateMonitor() {
    if ($('#monitor-content')) $('#monitor-content').innerHTML = monitorContent();
    if ($('#monitor-health')) {
      $('#monitor-health').innerHTML = `<i class="connection-dot"></i>${healthText()}`;
      $('#monitor-health').classList.toggle('disconnected', state.runtimeError);
    }
    if ($('#agent-team')) $('#agent-team').innerHTML = agentList(state.data);
  }
  function scheduleRuntime(id, delay = 2000) {
    clearTimeout(state.runtimeTimer);
    state.runtimeTimer = setTimeout(async () => {
      if (state.id !== id) return;
      try {
        const runtime = await get(`/api/studio/sessions/${encodeURIComponent(id)}/runtime`);
        if (state.id !== id) return;
        const recovered = state.runtimeError;
        const changedStatus = runtime.session_status !== state.data.session.status;
        if (!state.runtime || runtime.server_time >= state.runtime.server_time) state.runtime = runtime;
        state.runtimeError = false;
        updateMonitor();
        if (changedStatus || recovered) {await selectSession(id, {quiet:true}); return;}
        if (['running','paused'].includes(runtime.session_status)) scheduleRuntime(id);
      } catch (_) {
        if (state.id !== id) return;
        state.runtimeError = true; updateMonitor(); scheduleRuntime(id, 5000);
      }
    }, delay);
  }
  function emptyView() {
    if(state.entry==='direct')return `<div class="empty-view direct-entry"><button class="scope-text-button" data-entry="choose">← 返回</button><h1>直接研究</h1><button class="scope-text-button" data-entry="guided">切换引导式研究 →</button></div>`;
    return `<div class="empty-view research-entry"><h1>新建研究</h1><div class="research-entry-grid"><article class="research-entry-card"><span class="entry-icon">${icon('spark')}</span><h2>引导式研究</h2><button class="secondary-button" data-entry="guided">开始梳理思路 →</button></article><article class="research-entry-card"><span class="entry-icon">${icon('flask')}</span><h2>直接研究</h2><button class="secondary-button" data-entry="direct">直接开始研究 →</button></article></div></div>`;
  }
  const quoteLabels = {quote_found:'原文片段已匹配', quote_not_found:'返回内容中未找到原文',
    quote_too_short:'引文过短，无法可靠核对', no_excerpt:'未提供原文片段', not_checked:'未核验原文'};
  const relationLabels = {supports:'支持',contradicts:'反对',context:'背景资料',uncertain:'尚未确定'};
  const levelLabels = {direct:'直接证据',indirect:'间接证据',background:'背景信息',unknown:'未标注强度'};
  function evidenceView(d) {
    const summary = d.evidence?.summary || {total:0,quotes_matched:0,scope_warnings:0};
    const policy = d.policy || {};
    return `<h1 class="view-heading evidence-title">证据</h1><div class="evidence-metrics"><div><strong>${summary.total}</strong><span>证据条目</span></div><div><strong>${summary.quotes_matched}</strong><span>原文片段匹配</span></div><div><strong>${summary.scope_warnings}</strong><span>适用范围提示</span></div><div><strong>${d.hypotheses.length}</strong><span>新颖性待核验</span></div></div><div class="evidence-policy"><span>${icon('flask')}研究对象：${esc(d.evidence?.target_molecule === 'unknown' ? '未限定' : d.evidence?.target_molecule || '未限定')}</span><span>${policy.mode === 'lightweight' ? `轻量研究 · 每对假设最多比较 ${policy.pair_cap} 次 · 最多 ${policy.max_ideas} 条假设` : '历史 / 标准研究策略'}</span></div><div class="evidence-toolbar"><div class="evidence-filters">${[['all','全部证据'],['scope','范围提示'],['unverified','原文待核验'],['matched','原文已匹配']].map(([key,label]) => `<button data-evidence-filter="${key}" aria-pressed="${state.evidenceFilter===key}">${label}</button>`).join('')}</div><select id="evidence-hypothesis" aria-label="按假设筛选证据"><option value="all">全部假设</option>${d.hypotheses.map((h,i)=>`<option value="${esc(h.id)}" ${state.evidenceHyp===h.id?'selected':''}>H${String(i+1).padStart(2,'0')} · ${esc(hypothesisTitle(h))}</option>`).join('')}</select></div><div id="evidence-card-list">${evidenceCards(d)}</div><section class="novelty-section"><h2>检索记录与新颖性边界</h2>${(d.evidence?.novelty || []).map((n,i) => `<details class="novelty-search"><summary>H${String(i+1).padStart(2,'0')} · 新颖性尚未核验 <span>${n.searches.length} 条已保存检索</span></summary>${n.searches.length ? n.searches.map(q=>`<div class="search-audit-row"><span>${esc(q.tool)}</span><strong>${esc(q.query || '未记录检索式')}</strong><small>${q.status === 'failed' ? '请求失败' : q.status === 'empty' ? '检索成功 · 无结果' : '检索完成'}${q.result_count!=null ? ` · ${q.result_count} 条结果` : ''}</small></div>`).join('') : '<p>暂无检索记录</p>'}</details>`).join('')}</section>`;
  }
  function evidenceCards(d) {
    const cards = (d.evidence?.cards || []).filter(c => (state.evidenceHyp==='all' || c.hypothesis_id===state.evidenceHyp) && (state.evidenceFilter==='all' || state.evidenceFilter==='scope' && c.scope_warning || state.evidenceFilter==='unverified' && c.quote_status!=='quote_found' || state.evidenceFilter==='matched' && c.quote_status==='quote_found'));
    return cards.map(c => {
      const i = d.hypotheses.findIndex(h=>h.id===c.hypothesis_id);
      const hypothesis = d.hypotheses[i];
      const displayClaim = c.origin !== 'reflection' && hypothesis && c.claim === hypothesis.summary ? `关联假设：${hypothesisTitle(hypothesis)}` : c.claim || c.title;
      return `<article class="evidence-card" data-evidence-card="${esc(c.id)}"><div class="evidence-card-top"><span class="hyp-number">${esc(c.reference)}</span><span>H${String(i+1).padStart(2,'0')} · ${esc(agents[c.origin]?.[0] || c.origin)}</span><span class="evidence-quote-state ${c.quote_status==='quote_found'?'matched':''}">${icon(c.quote_status==='quote_found'?'check':'clock')}${quoteLabels[c.quote_status] || '未核验原文'}</span></div><h2 title="${esc(c.claim || c.title)}">${esc(displayClaim)}</h2>${(c.claim || c.title).length > 240 ? `<details class="claim-detail"><summary>展开完整论断</summary><p>${esc(c.claim || c.title)}</p></details>` : ''}<a class="evidence-source" href="${esc(c.url)}" target="_blank" rel="noopener noreferrer">${icon('link')}${esc(c.title)} ↗</a><div class="evidence-judgment"><span>Agent 标注：${relationLabels[c.relation] || '尚未确定'}</span><span>${levelLabels[c.support_level] || '未标注强度'}</span><span>来源涉及 ${esc(({unknown:'未识别对象',mixed:'DNA / RNA'})[c.source_molecule] || c.source_molecule)}</span></div>${c.scope_warning ? `<div class="scope-warning">${icon('activity')}<span>${esc(c.scope_warning)}</span></div>` : ''}${c.scope ? `<p class="source-scope">Agent 标注的研究条件：${esc(c.scope)}</p>` : ''}<details class="quote-detail"><summary>${c.excerpt ? '查看引用的原文片段' : '尚无原文片段'}${c.legacy ? '<span>历史记录 · 未重新核验</span>' : `<span>${{fetched_text:'网页提取内容',abstract:'数据库摘要',search_snippet:'搜索摘要',unknown:'未保存可核验内容'}[c.retrieval_level] || ''}</span>`}</summary>${c.excerpt ? `<blockquote>${esc(c.excerpt)}</blockquote>` : '<p>当前条目仅有链接，无法核对原文与论断之间的关系。</p>'}</details><div class="evidence-card-bottom"><span>论断是否被支持：待人工核验</span><button data-hypothesis="${esc(c.hypothesis_id)}">查看假设 ${icon('chevron')}</button></div></article>`;
    }).join('') || '<div class="empty-state">当前筛选下没有证据条目。没有已匹配原文，表示仍需核验。</div>';
  }
  function renderEvidenceCards() {
    if ($('#evidence-card-list')) $('#evidence-card-list').innerHTML = evidenceCards(state.data);
    document.querySelectorAll('[data-evidence-filter]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.evidenceFilter===state.evidenceFilter)));
  }
  function launchPolicy() {
    const light = $('#research-mode').value === 'lightweight';
    $('#launch-form').elements.n_initial.min = light ? 2 : 1;
    $('#launch-form').elements.n_initial.max = light ? 6 : 20;
    const initial = $('#launch-form').elements.n_initial;
    initial.value = Math.min(Number(initial.max), Math.max(Number(initial.min), Number(initial.value || 2)));
  }
  document.addEventListener('change', event => {
    if (event.target.id==='evidence-hypothesis') {state.evidenceHyp=event.target.value; renderEvidenceCards();}
    if (event.target.id==='research-mode') launchPolicy();
  });
  function card(h, index) {
    return `<button class="hypothesis-card" data-hypothesis="${esc(h.id)}"><div class="hyp-card-top"><span class="hyp-number">H${String(index+1).padStart(2,'0')}</span><span class="hyp-tag">${h.reviews.length ? '已审查' : '候选假设'}</span><span class="hyp-elo">${Math.round(h.elo || 0)} <span>Elo</span></span></div><h3>${esc(hypothesisTitle(h))}</h3><p>${esc(h.summary || h.title)}</p><div class="hyp-card-bottom"><span>${icon('check')}${esc(verdict(h.reviews[0]?.verdict))}</span><span>${fmt(h.matches)} 场比较 ${icon('chevron')}</span></div></button>`;
  }
  function researchView(d) {
    const completed = d.session.status === 'done';
    const count = d.hypotheses.length;
    const parsed = new DOMParser().parseFromString(d.report.html, 'text/html');
    const summary = parsed.querySelector('p')?.textContent || 'Agent 将围绕你的研究问题，检索文献、提出假设，并通过独立审查与比较逐步完善研究方向。';
    return `<div class="user-label">研究问题 · ${date(d.session.created_at)} ${icon('clock')}</div><div class="user-question">${esc(goalText(d.session.research_goal))}${goalText(d.session.research_goal) !== d.session.research_goal ? '<br><button class="question-expand" data-action="original">查看原始问题 ↗</button>' : ''}</div><article class="assistant-message"><div class="assistant-heading"><span class="assistant-mark">${icon('flask')}</span><strong>Co-Scientist</strong><span class="completion-badge">${icon(completed ? 'check' : 'activity')}${statusText(d.session.status)}</span></div><div class="assistant-body"><div class="agent-pipeline" aria-label="Agent 执行状态">${d.stages.map((s, i) => `${i ? `<span class="pipeline-arrow">${icon('chevron')}</span>` : ''}<span class="pipeline-step ${esc(s.status)}" title="${agents[s.agent][0]} · ${statusText(s.status)}">${icon(s.status === 'done' ? 'check' : s.status === 'unused' ? 'minus' : 'activity')}${agents[s.agent][3]}</span>`).join('')}</div><h1 class="result-heading">${count ? `${count === 2 ? '两' : count}条可验证的研究假设` : '研究正在展开'}</h1><div class="result-summary">${esc(summary)}</div><div class="hypothesis-cards">${d.hypotheses.map(card).join('') || '<div class="empty-state">正在等待第一条假设…</div>'}</div><button class="evidence-entry" data-view="evidence">${icon('book')}<span>检查证据与适用范围<small>${d.evidence?.summary.total || 0} 条证据 · ${d.evidence?.summary.scope_warnings || 0} 条范围提示 · 新颖性待核验</small></span>${icon('chevron')}</button>${d.report.markdown ? `<button class="report-link" data-action="report">${icon('file')} 阅读完整研究报告 ${icon('chevron')}</button><div class="message-actions"><button class="icon-button" data-action="copy" aria-label="复制研究报告" title="复制报告">${icon('copy')}</button><button class="icon-button" data-action="download" aria-label="下载 Markdown 报告" title="下载 Markdown">${icon('download')}</button><span>${d.sources.length} 篇参考资料</span></div>` : ''}</div></article>`;
  }
  function hypothesesView(d) {
    return `<h1 class="view-heading">研究假设</h1><div class="hypothesis-cards">${d.hypotheses.map(card).join('') || '<div class="empty-state">还没有假设，等待 Generation Agent 开始工作。</div>'}</div>`;
  }
  function activityView(d) {
    return `<h1 class="view-heading">运行记录</h1><div class="activity-group-label">模型调用</div>${d.calls.map(c => `<div class="call-row"><span class="call-avatar">${icon(agents[c.agent]?.[2] || 'activity')}</span><div class="call-info"><strong>${esc(agents[c.agent]?.[0] || c.agent)} <span class="hyp-tag">${esc(c.action)}</span></strong><p>${esc(c.model)} · ${time(c.started_at)}</p></div><div class="call-meta">${fmt(Number(c.input_tokens || 0) + Number(c.output_tokens || 0))} tokens<br>${money(c.cost_usd)}</div></div>`).join('') || '<div class="empty-state">暂无模型调用记录。</div>'}<div class="activity-group-label">假设比较</div>${d.matches.slice(0,8).map(m => `<div class="event-row"><time>${time(m.created_at)}</time><details><summary>${m.winner === 'tie' ? '平局' : '已完成假设比较'} · ${esc(m.mode)}</summary><pre>${esc(m.rationale)}\n\n${esc(m.hyp_a)}: ${m.elo_a_after}\n${esc(m.hyp_b)}: ${m.elo_b_after}</pre></details></div>`).join('') || ''}<div class="activity-group-label">运行事件</div>${d.events.map(e => `<div class="event-row"><time>${time(e.ts)}</time><details><summary>${esc(e.agent || 'Supervisor')} · ${esc(e.event)}</summary><pre>${esc(JSON.stringify(e.payload, null, 2))}</pre></details></div>`).join('')}`;
  }
  function sourcesView(d) {
    return `<h1 class="view-heading">参考文献</h1>${d.sources.map((s,i) => {
      let domain = ''; try { domain = new URL(s.url).hostname; } catch (_) { /* validated server-side */ }
      return `<div class="source-item"><span class="source-index">S${i+1}</span><div><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title === s.url ? domain + ' · 研究资料' : s.title)} ↗</a><small>${esc(s.url)}</small></div></div>`;
    }).join('') || '<div class="empty-state">还没有收录文献。</div>'}`;
  }
  function inspector(d) {
    if(!d)return '';
    const budget = d?.session.budget_usd ?? defaults.budget;
    const cost = d?.usage.cost_usd || 0;
    return `<div class="topic-icon">${icon('flask')}</div><h2 class="overview-topic">${d ? esc(title(d.session.research_goal)) : '下一项研究，<br>从这里开始。'}</h2><span class="panel-state">${icon(d?.session.status === 'done' ? 'check' : 'clock')}${d ? statusText(d.session.status) : '等待研究问题'}</span><hr class="panel-rule"><div class="panel-label">研究产出</div><div class="metric-row"><span>候选假设</span><strong>${d?.hypotheses.length || '—'}</strong></div><div class="metric-row"><span>参考资料</span><strong>${d?.sources.length || '—'}</strong></div><div class="metric-row"><span>假设比较</span><strong>${d ? d.matches.length + (d.matches.length === 40 ? '+' : '') : '—'}</strong></div><hr class="panel-rule"><div class="panel-label">运行预算</div><div class="metric-row"><span>已用 / 上限</span><strong>${money(cost)} / ${money(budget)}</strong></div><div class="progress-track"><i style="width:${Math.min(100, budget > 0 ? cost / budget * 100 : 0)}%"></i></div><hr class="panel-rule"><div class="panel-label">Agent</div><div class="agent-list" id="agent-team">${agentList(d)}</div>${d ? `<hr class="panel-rule"><div class="panel-label">研究文件</div>${(d.attachments||[]).map(f=>`<a class="panel-doc" href="${esc(f.url)}" title="${esc(f.name)}">${icon('file')}<span class="file-text">${esc(f.name)}</span></a>`).join('')}${d.report.markdown ? `<button class="panel-doc" data-action="report">${icon('file')}<span class="file-text">研究报告</span>${icon('chevron')}</button>` : ''}<button class="panel-doc" data-action="library">${icon('book')}<span class="file-text">参考文献</span>${icon('chevron')}</button><button class="panel-doc" data-view="activity">${icon('activity')}<span class="file-text">模型调用</span>${icon('chevron')}</button>` : ''}`;
  }
  function detail(label, content) {
    $('#detail-eyebrow').textContent = label;
    $('#detail-content').innerHTML = content;
    $('#detail-dialog').showModal();
    $('#detail-dialog').scrollTop = 0;
  }
  function showHypothesis(id) {
    const h = state.data?.hypotheses.find(item => item.id === id);
    if (!h) return toast('结果正在同步，请稍后再查看');
    detail('假设与审查', `<div class="dialog-actions"><button class="secondary-button" data-evidence-hyp="${esc(h.id)}">${icon('book')}查看证据卡</button></div><div class="document"><span class="hyp-tag">新颖性待核验</span><div class="hyp-card-top">Elo ${Math.round(h.elo)} · ${h.matches} 场比较</div><h1>${esc(hypothesisTitle(h))}</h1>${hypothesisTitle(h) !== h.title ? `<p>${esc(h.title)}</p>` : ''}${h.html.replace(/^<h1>[\s\S]*?<\/h1>\s*/, '') || `<p>${esc(h.summary)}</p>`}${h.reviews.map(r => `<div class="review-heading">REFLECTION · 独立审查 · ${esc(verdict(r.verdict))}</div>${r.html}`).join('')}</div>`);
  }
  function showReport() {
    if (!state.data?.report.markdown) return toast('研究报告尚未生成');
    detail('研究报告', `<div class="dialog-actions"><button class="secondary-button" data-action="copy">${icon('copy')}复制</button><button class="secondary-button" data-action="download">${icon('download')}下载 Markdown</button></div><div class="document">${state.data.report.html}</div>`);
  }
  function showSettings() {
    const model = state.data?.calls.find(c => c.model)?.model || defaults.model || '配置模型';
    detail('设置', `<div class="settings-row"><span>当前模型</span><strong>${esc(model)}</strong></div><div class="settings-row"><span>默认预算上限</span><strong>${money(defaults.budget)}</strong></div><div class="settings-row"><span>默认运行时限</span><strong>${defaults.wall_clock} 秒</strong></div><div class="settings-row"><span>数据保存位置</span><strong>本机 · SQLite + artifacts</strong></div>`);
  }
  function openLaunch() {
    const value = directGoal();
    if (!value) return $('#question').focus();
    state.launchDraft=null;
    $('#launch-question').textContent = value;
    $('#launch-error').textContent = '';
    applyLaunchDefaults();$('#launch-submit').disabled=false;
    launchPolicy();
    $('#launch-dialog').showModal();
  }
  function inputChanged() {
    const el = $('#question');
    $('#send').disabled = (!el.value.trim() && !directInputs.files().length)||directInputs.busy();
    el.style.height = 'auto';
    el.style.height = `${Math.min(140, Math.max(44, el.scrollHeight))}px`;
  }
  function search() {
    const query = $('#search-input').value.trim().toLowerCase();
    const list = state.sessions.filter(s => `${s.research_goal} ${title(s.research_goal)}`.toLowerCase().includes(query));
    $('#search-results').innerHTML = list.map(s => `<button class="search-result" data-session="${esc(s.id)}">${esc(title(s.research_goal))}<small>${date(s.created_at)} · ${statusText(s.status)} · ${s.hypothesis_count} 条假设</small></button>`).join('') || '<div class="empty-state">没有找到相关研究。</div>';
  }
  function setView(view) {
    if (!state.data && view !== 'research') return toast('选择一项研究后，即可查看相关资料');
    state.view = view;
    const url = new URL(location.href); url.searchParams.set('view', view); history.replaceState(null, '', url);
    render();
    $('#conversation-scroll').scrollTop = 0;
  }
  document.addEventListener('click', async event => {
    const entry=event.target.closest('[data-entry]');
    if(entry){
      scoping.collect();
      if(scoping.isBusy())return toast('正在保存回复，请稍后切换');
      try{await scoping.leave();}catch(e){return toast(e.message);}
      if(state.id)newResearch();
      state.entry=entry.dataset.entry;
      const hash=state.entry==='guided'?(scoping.draftId()?`guided/${scoping.draftId()}`:'guided'):state.entry==='direct'?'direct':'new';
      location.hash=hash; render();
      if(state.entry==='guided')await scoping.list();
      if(state.entry==='direct')$('#question').focus();
      return;
    }
    const session = event.target.closest('[data-session]');
    if (session) {
      $('#search-dialog').close();
      $('#sidebar').classList.remove('mobile-open');
      state.view = 'research';
      const url = new URL(location.href); url.searchParams.delete('view'); history.replaceState(null, '', url);
      location.hash = `session/${session.dataset.session}`;
      return;
    }
    const agent = event.target.closest('[data-agent]');
    if (agent) {
      state.selectedAgent = agent.dataset.agent;
      if (state.view === 'agents') updateMonitor(); else setView('agents');
      $('.agent-focus')?.scrollIntoView({behavior:'smooth', block:'start'}); return;
    }
    const evidenceFilter = event.target.closest('[data-evidence-filter]');
    if (evidenceFilter) {state.evidenceFilter=evidenceFilter.dataset.evidenceFilter; renderEvidenceCards(); return;}
    const evidenceHyp = event.target.closest('[data-evidence-hyp]');
    if (evidenceHyp) {state.evidenceHyp=evidenceHyp.dataset.evidenceHyp; state.evidenceFilter='all'; $('#detail-dialog').close(); setView('evidence'); return;}
    const hyp = event.target.closest('[data-hypothesis]');
    if (hyp) return showHypothesis(hyp.dataset.hypothesis);
    const suggestion = event.target.closest('[data-suggestion]');
    if (suggestion) {
      $('#question').value = suggestion.dataset.suggestion;
      inputChanged();
      return $('#question').focus();
    }
    const view = event.target.closest('[data-view]');
    if (view) return setView(view.dataset.view);
    const action = event.target.closest('[data-action]')?.dataset.action;
    switch (action) {
      case 'attach':return $('#direct-files').click();
      case 'parameters':
        state.launchDraft=null;applyLaunchDefaults();launchPolicy();
        $('#launch-question').textContent=directGoal()||'新建研究';$('#launch-error').textContent='';
        $('#launch-submit').disabled=!directGoal();return $('#launch-dialog').showModal();
      case 'new': return newResearch();
      case 'retry': return state.id ? selectSession(state.id) : location.reload();
      case 'workspace': return setView('research');
      case 'library': return setView('sources');
      case 'report': return showReport();
      case 'settings': return showSettings();
      case 'original': return detail('RESEARCH QUESTION · 原始问题', `<div class="document"><h1>研究问题</h1><p>${esc(state.data.session.research_goal)}</p></div>`);
      case 'close-detail': return $('#detail-dialog').close();
      case 'close-launch': rememberLaunch();return $('#launch-dialog').close();
      case 'search':
        $('#search-input').value = ''; search(); $('#search-dialog').showModal(); return $('#search-input').focus();
      case 'sidebar':
        if (innerWidth <= 700) $('#sidebar').classList.toggle('mobile-open');
        else document.body.classList.toggle('sidebar-hidden');
        return;
      case 'inspector':
        if (innerWidth <= 1000) $('#inspector').classList.toggle('mobile-open');
        else document.body.classList.toggle('inspector-hidden');
        return;
      case 'copy':
        try { await navigator.clipboard.writeText(state.data?.report.markdown || ''); toast('研究报告已复制'); }
        catch (_) { toast('复制失败，请下载 Markdown 文件'); }
        return;
      case 'download': {
        if (!state.data?.report.markdown) return;
        const url = URL.createObjectURL(new Blob([state.data.report.markdown], {type:'text/markdown;charset=utf-8'}));
        const a = document.createElement('a'); a.href = url; a.download = `Co-Scientist-${state.id}.md`; a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000); return;
      }
    }
  });
  $('#question').addEventListener('input', inputChanged);
  $('#question').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault(); openLaunch();}
  });
  $('#send').addEventListener('click', openLaunch);
  $('#direct-files').addEventListener('change',e=>{const files=Array.from(e.target.files);e.target.value='';directInputs.upload(files);});
  $('#composer').addEventListener('dragover',e=>e.preventDefault());
  $('#composer').addEventListener('drop',e=>{e.preventDefault();directInputs.upload(Array.from(e.dataTransfer.files));});
  $('#question').addEventListener('paste',e=>{if(e.clipboardData?.files.length){e.preventDefault();directInputs.upload(Array.from(e.clipboardData.files));}});
  document.addEventListener('click',e=>{const remove=e.target.closest('#direct-attachments [data-input-remove]');if(remove)directInputs.remove(remove.dataset.inputRemove);});
  $('#launch-form').addEventListener('change',rememberLaunch);
  $('#launch-form').addEventListener('input',rememberLaunch);
  $('#launch-dialog').addEventListener('close',rememberLaunch);
  $('#search-input').addEventListener('input', search);
  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && ['k','n'].includes(event.key.toLowerCase())) {
      event.preventDefault();
      if (document.querySelector('dialog[open]')) return;
      if (event.key.toLowerCase() === 'n') newResearch();
      else {$('#search-input').value = ''; search(); $('#search-dialog').showModal(); $('#search-input').focus();}
    }
  });
  document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const r = dialog.getBoundingClientRect();
    if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) dialog.close();
  }));
  $('#launch-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = $('#launch-submit');
    if (button.disabled) return;
    button.disabled = true; button.textContent = '正在创建研究…';
    const body = new FormData(event.target);
    rememberLaunch();
    body.set('goal', directGoal());
    if(!state.launchDraft && directInputs.group())body.set('upload_group_id',directInputs.group());
    body.set('preferences', '请使用中文撰写最终研究报告，保留必要的英文术语。请区分有文献支持的事实、推测与待验证假设。');
    try {
      let result;
      if(state.launchDraft){
        const options=Object.fromEntries(body);
        delete options.goal; delete options.preferences;
        ['budget_usd','budget_tokens','n_initial','wall_clock_seconds'].forEach(k=>options[k]=Number(options[k]));
        result=await scoping.confirm(options);
      }else{
        const response = await fetch('/sessions/new', {method:'POST', body,headers:{Accept:'application/json'}});
        if (!response.ok) {const error = await response.json().catch(() => ({})); throw new Error(typeof error.detail === 'string' ? error.detail : `创建失败（${response.status}）`);}
        result=await response.json();
      }
      $('#launch-dialog').close();
      $('#question').value = ''; inputChanged();
      if(!state.launchDraft)directInputs.reset();
      toast(defaults.preview_only?'离线预览已保存 Session，科研执行保持关闭':'研究已创建，正在启动多 Agent 工作流');
      state.launchDraft=null;
      await refreshSessions();
      state.view='agents'; location.hash=`session/${result.session_id}`;
    } catch (error) {$('#launch-error').textContent = error.message;}
    finally {button.disabled = false; button.innerHTML = `开始研究 ${icon('arrow')}`;}
  });
  function fromHash() {
    const id = location.hash.match(/^#session\/(.+)$/)?.[1];
    if (id) selectSession(decodeURIComponent(id));
    else if(location.hash.startsWith('#guided')){
      if(state.id!==null)newResearch({navigate:false});
      state.entry='guided'; render();
      const draft=location.hash.match(/^#guided\/(.+)$/)?.[1];
      if(draft)scoping.load(decodeURIComponent(draft)); else scoping.list();
    }
    else if(location.hash==='#direct'){if(state.id!==null)newResearch({navigate:false}); state.entry='direct';render();}
    else if (location.hash === '#new') {if (state.id !== null) newResearch(); else {state.entry='choose';render();}}
    else render();
  }
  window.addEventListener('hashchange', fromHash);
  async function init() {
    try {
      await refreshSessions();
      await directInputs.restore();
      const view = new URLSearchParams(location.search).get('view');
      if (['research','agents','hypotheses','activity','evidence'].includes(view)) state.view = view;
      if (location.hash) fromHash();
      else if (state.sessions.length) {history.replaceState(null, '', `#session/${state.sessions[0].id}`); await selectSession(state.sessions[0].id);}
      else render();
    } catch (error) {
      render(); toast('无法连接本地服务，请刷新后重试');
      $('#session-state').textContent = '连接中断';
    }
  }
  init();
})();
