/* AI 短剧工坊 · 工作台前端（零依赖，原生 JS）
 * 通过 /api/* 与后端交互；所有算力为桩，不产生真实费用。 */
'use strict';

const state = {
  view: 'overview',
  projects: [],
  projectId: '',
  shots: [],
  assets: [],
  tasks: [],
  prompts: [],
  pipeline: null,
  detail: {},
};

const $ = (sel) => document.querySelector(sel);
const content = () => $('#content');

// ---------- 通用工具 ----------
async function api(method, path, body) {
  const opt = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const resp = await fetch(path, opt);
  let data;
  try { data = await resp.json(); } catch (e) { data = { error: '响应解析失败' }; }
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function toast(msg, type = '') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show ' + type;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.className = 'toast'; }, 2800);
}

function showModal(title, text) {
  $('#modalTitle').textContent = title;
  $('#modalBody').textContent = typeof text === 'string' ? text : JSON.stringify(text, null, 2);
  $('#modal').classList.remove('hidden');
}

function badge(text, kind = 'gray') {
  return `<span class="badge ${kind}">${esc(text)}</span>`;
}

function statusBadge(status) {
  const map = {
    created: ['created','gray'], queued: ['queued','blue'], leased: ['leased','amber'],
    running: ['running','blue'], uploaded: ['uploaded','blue'], succeeded: ['succeeded','green'],
    failed: ['failed','red'], dead_letter: ['dead_letter','red'], cancelled: ['cancelled','gray'],
  };
  const [label, kind] = map[status] || [status || '—', 'gray'];
  return badge(label, kind);
}

function empty(icon, text) {
  return `<div class="empty"><div class="ico">${icon}</div><div>${esc(text)}</div></div>`;
}

// ---------- 数据加载 ----------
async function loadBase() {
  try {
    await api('GET', '/api/health');
    setConn(true);
  } catch (e) { setConn(false); throw e; }
  const [projects, pipeline] = await Promise.all([
    api('GET', '/api/projects'),
    api('GET', '/api/pipeline'),
  ]);
  state.projects = projects.projects || [];
  state.pipeline = pipeline;
  const sel = $('#projectSelect');
  sel.innerHTML = state.projects.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  if (state.projects.length) {
    state.projectId = state.projects[0].id;
    sel.value = state.projectId;
  }
}

async function loadViewData() {
  const pid = state.projectId;
  if (!pid) { state.shots = []; state.assets = []; state.tasks = []; state.prompts = []; return; }
  const url = (p) => p + (p.includes('?') ? '&' : '?') + 'project_id=' + encodeURIComponent(pid);
  const [shots, assets, tasks, prompts] = await Promise.all([
    api('GET', '/api/shots'),
    api('GET', '/api/assets'),
    api('GET', '/api/tasks'),
    api('GET', '/api/prompts'),
  ]);
  // 按项目过滤（shots/tasks 通过 episode 归属，简化为全量；后续视图内再筛）
  state.shots = shots.shots || [];
  state.assets = (assets.assets || []).filter(a => a.project_id === pid);
  state.tasks = tasks.tasks || [];
  state.prompts = prompts.prompts || [];
}

function setConn(on) {
  const dot = $('.status-dot');
  dot.classList.toggle('on', on);
  $('#connStatus').textContent = on ? '服务已连接' : '连接失败';
}

// ---------- 视图：总览 ----------
function viewOverview() {
  const p = state.projects.find(x => x.id === state.projectId);
  const approved = state.assets.filter(a => a.approved).length;
  const queued = state.tasks.filter(t => t.status === 'queued').length;
  const steps = state.pipeline ? state.pipeline.steps.map((s, i) =>
    `<span class="step-chip"><b>${i + 1}</b>${esc(stepName(s))}</span>`).join('') : '';
  return `
    <div class="section-title"><span class="bar"></span>${esc(p ? p.name : '尚无项目')}</div>
    <div class="grid grid-4">
      <div class="card"><div class="card-title">镜头数</div><div class="stat-num accent">${state.shots.length}</div><div class="stat-sub">已拆分的制作镜头</div></div>
      <div class="card"><div class="card-title">已批准资产</div><div class="stat-num">${approved}/${state.assets.length}</div><div class="stat-sub">来源合规 + 人工批准</div></div>
      <div class="card"><div class="card-title">提示词版本</div><div class="stat-num">${state.prompts.length}</div><div class="stat-sub">权威 H3 Schema 产出</div></div>
      <div class="card"><div class="card-title">排队任务</div><div class="stat-num">${queued}</div><div class="stat-sub">待派单（算力为桩）</div></div>
    </div>
    <div class="card mt"><div class="card-title">八步制作链</div><div class="steps">${steps}</div>
      <p class="muted small">当前工作台对接本地核心库与桩后端，全流程不调用任何外部算力、不产生真实费用。真实 H3 / ComfyUI / 云 GPU 接入已按规划后置。</p>
    </div>
    <div class="card mt"><div class="card-title">快速开始</div>
      <p class="small">首次使用请点击右上角 <b>载入示例</b>，一键生成原创示例项目（剧本、拆镜、资产、提示词）。然后按左侧八步环节逐步查看与操作。</p>
    </div>`;
}

function stepName(s) {
  const m = { idea:'创意', script:'剧本', character:'角色设定', storyboard:'分镜',
    keyframe:'关键帧', video:'动画/视频', audio:'配音/字幕', compose:'合成成片' };
  return m[s] || s;
}

// ---------- 视图：剧本与拆镜 ----------
function viewScript() {
  const eps = state.detail.episodes || [];
  const ep = eps[0];
  let importBox = '';
  if (ep) {
    importBox = `
      <div class="card mt">
        <div class="section-title"><span class="bar"></span>导入剧本并自动拆镜</div>
        <div class="field-row">
          <div class="field"><label>格式</label>
            <select id="scriptFmt" class="select"><option value="txt">TXT</option><option value="md">Markdown</option><option value="json">JSON</option></select></div>
          <div class="field"><label>每镜时长（秒）</label><input id="scriptDur" class="input" type="number" value="5" min="1"></div>
        </div>
        <div class="field"><label>剧本正文</label><textarea id="scriptText" class="textarea" placeholder="粘贴剧本文本，每行一个场景/动作…"></textarea></div>
        <button class="btn" id="btnImport">导入并拆镜</button>
      </div>`;
  }
  const rows = state.shots.map(s => {
    const miss = (s.prompt_text || '').match(/\[缺字段：([^\]]+)\]/);
    return `<tr>
      <td>${s.index}</td>
      <td>${esc(s.location || '—')}</td>
      <td>${esc(s.action || '—')}</td>
      <td>${esc(s.dialogue || '—')}</td>
      <td>${miss ? badge('缺' + miss[1], 'amber') : badge('完整', 'green')}</td>
      <td>${statusBadge(s.task_status)}</td>
    </tr>`;
  }).join('');
  return `
    <div class="section-title"><span class="bar"></span>已拆镜头（${state.shots.length}）</div>
    <div class="card"><div class="table-wrap"><table class="table">
      <thead><tr><th>#</th><th>场景</th><th>动作</th><th>对白</th><th>字段</th><th>任务</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6">${empty('📄','暂无镜头，先载入示例或导入剧本')}</td></tr>`}</tbody>
    </table></div></div>
    ${importBox}`;
}

// ---------- 视图：提示词工程 ----------
function viewPrompt() {
  const rows = state.shots.map(s => {
    const vers = state.prompts.filter(p => p.shot_id === s.id);
    const latest = vers[vers.length - 1];
    return `<tr>
      <td>${s.index}</td>
      <td>${esc((s.action || s.location || '').slice(0, 26))}</td>
      <td>${latest ? badge('v' + latest.version, 'blue') : badge('未扩写','gray')}</td>
      <td>${latest ? `<span class="link act-expand" data-id="${esc(s.id)}">查看提示词</span>` : `<span class="link act-expand" data-id="${esc(s.id)}">扩写</span>`}</td>
    </tr>`;
  }).join('');
  return `
    <div class="section-title"><span class="bar"></span>权威 H3 提示词（三段式 / 六段式）</div>
    <div class="card"><div class="table-wrap"><table class="table">
      <thead><tr><th>#</th><th>镜头主题</th><th>版本</th><th>操作</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="4">${empty('✎','暂无镜头')}</td></tr>`}</tbody>
    </table></div></div>
    <div class="card mt">
      <div class="card-title">合规守卫</div>
      <p class="small muted">所有提示词扩写前自动经过版权守卫与内容安全过滤，命中红线直接拦截并写入审计日志。可在此手动检查任意文本：</p>
      <div class="field mt"><textarea id="checkText" class="textarea" placeholder="输入要检查的文本…"></textarea></div>
      <button class="btn btn-ghost" id="btnCheckText">合规检查</button>
      <div id="checkResult" class="mt"></div>
    </div>`;
}

// ---------- 视图：参考资产 ----------
function viewAsset() {
  const rows = state.assets.map(a => `<tr>
    <td>${esc(a.name)}</td>
    <td>${badge(a.type, 'blue')}</td>
    <td>${a.version}</td>
    <td>${esc(a.provenance?.source || '—')}</td>
    <td>${a.approved ? badge('已批准','green') : badge('待批准','amber')}</td>
    <td>${a.approved ? '—' : `<button class="btn btn-sm act-approve" data-id="${esc(a.id)}">批准</button>`}</td>
  </tr>`).join('');
  return `
    <div class="section-title"><span class="bar"></span>参考资产（批准 + 来源合规双门槛）</div>
    <div class="card"><div class="table-wrap"><table class="table">
      <thead><tr><th>名称</th><th>类型</th><th>版本</th><th>来源</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6">${empty('🖼','暂无资产')}</td></tr>`}</tbody>
    </table></div></div>
    <div class="card mt"><div class="card-title">新增资产</div>
      <div class="field-row">
        <div class="field"><label>名称</label><input id="assetName" class="input" placeholder="角色/场景名"></div>
        <div class="field"><label>类型</label><select id="assetType" class="select"><option value="role">角色</option><option value="scene">场景</option><option value="storyboard">分镜</option></select></div>
      </div>
      <div class="field"><label>来源声明</label><select id="assetSource" class="select">
        <option value="self_created">原创</option><option value="public_domain">公有领域</option>
        <option value="licensed">已授权</option><option value="ai_generated">AI 生成</option></select></div>
      <button class="btn" id="btnAddAsset">添加资产</button>
    </div>`;
}

// ---------- 视图：生成任务 ----------
function viewTask() {
  const rows = state.tasks.map(t => `<tr>
    <td class="small">${esc(t.id.slice(0, 16))}</td>
    <td>${badge(t.mode, 'blue')}</td>
    <td>${statusBadge(t.status)}</td>
    <td>${esc(t.failure_code || '—')}</td>
    <td>${t.cost_reserved_cents || 0} 分</td>
    <td class="inline">
      ${t.status === 'queued' ? `<button class="btn btn-sm act-dispatch" data-id="${esc(t.id)}">派单</button>` : ''}
      ${['leased','queued','running'].includes(t.status) ? `<button class="btn btn-sm btn-danger act-fail" data-id="${esc(t.id)}">模拟失败</button>` : ''}
    </td>
  </tr>`).join('');
  const canSubmit = state.shots.length > 0;
  return `
    <div class="section-title"><span class="bar"></span>任务队列（本地调度：幂等 / 租约 / 重试）</div>
    <div class="card"><div class="table-wrap"><table class="table">
      <thead><tr><th>任务 ID</th><th>模式</th><th>状态</th><th>失败码</th><th>冻结</th><th>操作</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6">${empty('⚙','暂无任务')}</td></tr>`}</tbody>
    </table></div></div>
    <div class="card mt"><div class="card-title">提交生成任务（算力为桩，不产生真实费用）</div>
      <p class="small muted">提交前自动过合规守卫 + 成本闸门；失败可归类重试，三次进死信。选择镜头提交：</p>
      <div class="field-row mt">
        <div class="field"><label>镜头序号</label>
          <select id="taskShot" class="select">${state.shots.map(s => `<option value="${esc(s.id)}">镜头 ${s.index}</option>`).join('')}</select></div>
        <div class="field"><label>生成模式</label><select id="taskMode" class="select"><option value="t2v">T2V</option><option value="i2v">I2V</option><option value="r2v">R2V</option></select></div>
        <div class="field"><label>预估成本（分）</label><input id="taskEst" class="input" type="number" value="50" min="0"></div>
      </div>
      <button class="btn" id="btnSubmitTask" ${canSubmit ? '' : 'disabled'}>提交任务</button>
    </div>`;
}

// ---------- 视图：后期成片 ----------
function viewPost() {
  return `
    <div class="section-title"><span class="bar"></span>成片装配（裁头 / 拼接 / 字幕 / 片尾 CTA）</div>
    <div class="card">
      <p class="small muted">基于当前镜头生成装配计划与 SRT 字幕。纯逻辑，不执行 ffmpeg；真实成片渲染依赖外部工具，已后置。</p>
      <div class="field mt"><label>片尾 CTA 文案</label><input id="ctaText" class="input" value="关注我们 · 下集见"></div>
      <button class="btn" id="btnCompose" ${state.shots.length ? '' : 'disabled'}>生成成片计划 + 字幕</button>
      <div id="composeResult" class="mt"></div>
    </div>`;
}

// ---------- 视图：成本闸门 ----------
function viewCost() {
  const ledger = state.detail.ledger;
  const spent = ledger ? (ledger.spent_cents / 100).toFixed(2) : '0.00';
  const reserved = ledger ? (ledger.reserved_cents / 100).toFixed(2) : '0.00';
  const p = state.projects.find(x => x.id === state.projectId);
  const total = p ? p.budget_total : 0;
  return `
    <div class="section-title"><span class="bar"></span>累计成本闸门（预估 + 已花 + 在途 ≤ 上限）</div>
    <div class="grid grid-3">
      <div class="card"><div class="card-title">已花</div><div class="stat-num">¥${spent}</div></div>
      <div class="card"><div class="card-title">在途冻结</div><div class="stat-num accent">¥${reserved}</div></div>
      <div class="card"><div class="card-title">项目总预算</div><div class="stat-num">${total ? '¥' + total : '不限'}</div></div>
    </div>
    <div class="card mt"><div class="card-title">闸门校验</div>
      <p class="small muted">输入预估成本，验证是否会被闸门放行（原子校验，不实际冻结）：</p>
      <div class="field-row mt">
        <div class="field"><label>预估成本（分）</label><input id="costEst" class="input" type="number" value="100"></div>
        <div class="field" style="display:flex;align-items:flex-end"><button class="btn" id="btnCostCheck">校验</button></div>
      </div>
      <div id="costResult" class="mt"></div>
    </div>
    <div class="card mt"><div class="card-title">单价表（演示口径，真实计价后置）</div>
      <table class="table"><thead><tr><th>后端</th><th>模型</th><th>单位</th><th>单价</th></tr></thead>
      <tbody>${(state.detail.price_rules || []).map(r => `<tr><td>${esc(r.backend_type)}</td><td>${esc(r.model)}</td><td>${esc(r.unit)}</td><td>${r.unit_price_cents} 分</td></tr>`).join('')}</tbody></table>
    </div>`;
}

// ---------- 视图：合规审计 ----------
function viewCompliance() {
  const events = state.detail.auditEvents || [];
  const rows = events.slice().reverse().map(e => `<tr>
    <td>${e.seq}</td>
    <td class="small">${esc(e.ts.replace('T',' ').slice(0,19))}</td>
    <td>${esc(e.event_type)}</td>
    <td>${esc(e.subject)}</td>
    <td>${e.passed ? badge('放行','green') : badge('拦截','red')}</td>
    <td class="small">${esc((e.findings || []).map(f => f.message).join('；') || '—')}</td>
  </tr>`).join('');
  const v = state.detail.auditVerify;
  return `
    <div class="section-title"><span class="bar"></span>合规审计日志（append-only + 哈希链）</div>
    <div class="card">
      <div class="inline">
        ${v ? (v.intact ? badge('哈希链完好','green') : badge('检测到篡改','red')) : ''}
        <span class="muted small">${esc(v?.detail || '')}</span>
        <span class="spacer"></span>
        <button class="btn btn-sm btn-ghost" id="btnVerifyAudit">校验完整性</button>
      </div>
    </div>
    <div class="card mt"><div class="table-wrap"><table class="table">
      <thead><tr><th>#</th><th>时间(UTC)</th><th>事件</th><th>主体</th><th>判定</th><th>命中详情</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6">${empty('🛡','暂无审计记录')}</td></tr>`}</tbody>
    </table></div></div>
    <div class="card mt"><p class="small muted">诚实边界：守卫为「最小规则 + 抗绕过归一化」，提高门槛但非绝对保证；文件级 append-only 依赖哈希链检测篡改，真要防篡改需外部 WORM / 签名服务。详见 COMPLIANCE.md。</p></div>`;
}

// ---------- 视图路由 ----------
const VIEWS = {
  overview: { title: '总览', desc: '项目全流程一览', render: viewOverview },
  script: { title: '剧本与拆镜', desc: '导入剧本 → 自动拆分镜头', render: viewScript },
  prompt: { title: '提示词工程', desc: '权威 H3 Schema 扩写 + 合规守卫', render: viewPrompt },
  asset: { title: '参考资产', desc: '来源声明 + 人工批准双门槛', render: viewAsset },
  task: { title: '生成任务', desc: '本地调度：幂等 / 租约 / 重试（算力为桩）', render: viewTask },
  post: { title: '后期成片', desc: '裁头 / 拼接 / SRT 字幕 / 片尾 CTA', render: viewPost },
  cost: { title: '成本闸门', desc: '累计成本原子校验', render: viewCost },
  compliance: { title: '合规审计', desc: '只追加日志 + 哈希链防篡改', render: viewCompliance },
};

async function render() {
  const v = VIEWS[state.view];
  $('#viewTitle').textContent = v.title;
  $('#viewDesc').textContent = v.desc;
  document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === state.view));

  // 视图专属数据
  if (state.view === 'overview') {
    const d = await api('GET', '/api/projects/' + encodeURIComponent(state.projectId)).catch(() => ({}));
    state.detail.episodes = d.episodes || [];
  } else if (state.view === 'cost') {
    const d = await api('GET', '/api/cost/overview?project_id=' + encodeURIComponent(state.projectId));
    state.detail.ledger = d.ledger; state.detail.price_rules = d.price_rules;
  } else if (state.view === 'compliance') {
    const [ev, vf] = await Promise.all([
      api('GET', '/api/audit'), api('GET', '/api/audit/verify'),
    ]);
    state.detail.auditEvents = ev.events || []; state.detail.auditVerify = vf;
  }
  content().innerHTML = v.render();
  bindViewEvents();
}

// ---------- 事件绑定 ----------
function bindViewEvents() {
  // 提示词：扩写 / 查看
  content().querySelectorAll('.act-expand').forEach(el => el.onclick = async () => {
    const sid = el.dataset.id;
    const s = state.shots.find(x => x.id === sid);
    const vers = state.prompts.filter(p => p.shot_id === sid);
    if (vers.length) {
      showModal('镜头提示词版本（' + vers.length + ' 版）', vers.map(p =>
        `--- v${p.version} ---\n${p.content}`).join('\n\n'));
    } else {
      try {
        const r = await api('POST', '/api/prompts/expand', { shot_id: sid, mode: 'r2v' });
        toast('提示词扩写完成（R2V 六段式）', 'ok');
        await refresh();
        showModal('合规守卫已通过', r.expanded);
      } catch (e) { toast(e.message, 'err'); }
    }
  });

  // 资产批准
  content().querySelectorAll('.act-approve').forEach(el => el.onclick = async () => {
    try {
      await api('POST', '/api/assets/' + encodeURIComponent(el.dataset.id) + '/approve', {});
      toast('资产已批准，审计已留痕', 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  });

  // 任务派单 / 模拟失败
  content().querySelectorAll('.act-dispatch').forEach(el => el.onclick = async () => {
    try { const r = await api('POST', '/api/tasks/' + encodeURIComponent(el.dataset.id) + '/dispatch', {});
      toast(r.reason === 'leased' ? '派单成功（已租约）' : ('派单：' + r.reason), 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  });
  content().querySelectorAll('.act-fail').forEach(el => el.onclick = async () => {
    try { const r = await api('POST', '/api/tasks/' + encodeURIComponent(el.dataset.id) + '/fail',
      { failure_code: 'PROVIDER_TRANSIENT' });
      toast('失败已归类：' + r.new_state, 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  });

  // 剧本导入
  const btnImport = $('#btnImport');
  if (btnImport) btnImport.onclick = async () => {
    const text = $('#scriptText').value.trim();
    if (!text) { toast('请先粘贴剧本文本', 'err'); return; }
    try {
      const r = await api('POST', '/api/script/import', {
        episode_id: state.detail.episodes[0].id, text,
        format: $('#scriptFmt').value, duration: parseFloat($('#scriptDur').value) || 5,
      });
      toast('导入成功，拆出 ' + r.shots.length + ' 个镜头', 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };

  // 合规检查
  const btnCheck = $('#btnCheckText');
  if (btnCheck) btnCheck.onclick = async () => {
    const text = $('#checkText').value.trim();
    if (!text) { toast('请输入文本', 'err'); return; }
    try {
      const r = await api('POST', '/api/compliance/check', { text, subject: 'manual' });
      $('#checkResult').innerHTML = r.passed
        ? badge('通过：未命中版权/内容安全红线', 'green')
        : badge('拦截：' + [...r.guard.findings, ...r.safety.findings].map(f => f.message).join('；'), 'red');
    } catch (e) { toast(e.message, 'err'); }
  };

  // 新增资产
  const btnAddAsset = $('#btnAddAsset');
  if (btnAddAsset) btnAddAsset.onclick = async () => {
    const name = $('#assetName').value.trim();
    if (!name) { toast('请输入资产名称', 'err'); return; }
    try {
      await api('POST', '/api/assets', { project_id: state.projectId, name,
        type: $('#assetType').value, source: $('#assetSource').value });
      toast('资产已添加（待批准）', 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };

  // 提交任务
  const btnSubmit = $('#btnSubmitTask');
  if (btnSubmit) btnSubmit.onclick = async () => {
    try {
      await api('POST', '/api/tasks', { shot_id: $('#taskShot').value,
        project_id: state.projectId, mode: $('#taskMode').value,
        estimate_cents: parseInt($('#taskEst').value) || 0 });
      toast('任务已提交（合规+成本闸门已校验）', 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };

  // 成片计划
  const btnCompose = $('#btnCompose');
  if (btnCompose) btnCompose.onclick = async () => {
    try {
      const r = await api('POST', '/api/compose/plan', {
        shots: state.shots.map(s => ({ index: s.index, dialogue: s.dialogue, duration: s.duration })),
        cta_text: $('#ctaText').value,
      });
      $('#composeResult').innerHTML = `
        <div class="inline">${badge('SRT 字幕 ' + r.cues.length + ' 条', 'green')}${badge('装配 ' + r.plan.trims.length + ' 段', 'blue')}</div>
        <div class="code mt">${esc(r.srt)}</div>`;
      toast('成片计划与字幕已生成', 'ok');
    } catch (e) { toast(e.message, 'err'); }
  };

  // 成本校验
  const btnCost = $('#btnCostCheck');
  if (btnCost) btnCost.onclick = async () => {
    try {
      const r = await api('POST', '/api/cost/check', { project_id: state.projectId,
        estimate_cents: parseInt($('#costEst').value) || 0 });
      $('#costResult').innerHTML = r.allowed
        ? badge('放行：' + r.detail || '在预算内', 'green')
        : badge('拦截：' + r.detail, 'red');
    } catch (e) { toast(e.message, 'err'); }
  };

  // 审计校验
  const btnVerify = $('#btnVerifyAudit');
  if (btnVerify) btnVerify.onclick = async () => {
    try { const r = await api('GET', '/api/audit/verify');
      toast(r.detail, r.intact ? 'ok' : 'err'); await render();
    } catch (e) { toast(e.message, 'err'); }
  };
}

async function refresh() {
  await loadViewData();
  await render();
}

// ---------- 初始化 ----------
async function init() {
  document.querySelectorAll('.nav-item').forEach(b => {
    b.onclick = () => { state.view = b.dataset.view; render().catch(e => toast(e.message, 'err')); };
  });
  $('#btnRefresh').onclick = () => refresh().catch(e => toast(e.message, 'err'));
  $('#btnDemo').onclick = async () => {
    try {
      const r = await api('POST', '/api/demo/init', {});
      toast(r.initialized ? '示例项目已载入' : '已有数据，未重复载入', 'ok');
      await loadBase(); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };
  $('#projectSelect').onchange = (e) => { state.projectId = e.target.value; refresh().catch(err => toast(err.message, 'err')); };
  $('#modalClose').onclick = () => $('#modal').classList.add('hidden');
  $('#modal').onclick = (e) => { if (e.target === $('#modal')) $('#modal').classList.add('hidden'); };

  try {
    await loadBase();
    if (state.projects.length) await loadViewData();
    await render();
  } catch (e) {
    content().innerHTML = empty('⚠', '无法连接后端服务：' + e.message + '。请先运行 python start.py');
  }
}

init();
