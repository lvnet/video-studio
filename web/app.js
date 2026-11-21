/* AI 短剧工坊 · 工作台前端（零依赖，原生 JS）
 * 通过 /api/* 与后端交互；所有算力为桩，不产生真实费用。 */
'use strict';

const state = {
  view: 'script',          // 默认从「剧本 / 分集管理」起步：先建整部多集，再走八步精加工
  wfStep: 0,               // 八步流程当前步骤
  projects: [],
  projectId: '',
  episodes: [],
  episodeId: '',
  shots: [],
  assets: [],
  tasks: [],
  prompts: [],
  detail: {},
};

// 方案定稿八步：项目 → 剧本 → 拆镜 → 参考资产 → 提示词 → 提交 → 挑片 → 精修成片
const WF_STEPS = ['创建项目', '剧本', '拆镜', '参考资产', '提示词', '提交生成', '监控挑片', '精修成片'];
const WF_KEYS = ['project', 'script', 'storyboard', 'reference', 'prompt', 'submit', 'review', 'finalize'];

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
  t._timer = setTimeout(() => { t.className = 'toast'; }, 3000);
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

// 八步 stepper
function stepper(current) {
  return `<div class="stepper">${WF_STEPS.map((s, i) => {
    const cls = i < current ? 'done' : (i === current ? 'current' : '');
    const circle = i < current ? '✓' : (i + 1);
    return `<div class="step-node ${cls}" data-step="${i}" style="cursor:pointer">
      <div class="step-circle">${circle}</div><div class="step-label">${s}</div></div>`;
  }).join('')}</div>`;
}

// ---------- 数据加载 ----------
async function loadBase() {
  try { await api('GET', '/api/health'); setConn(true); }
  catch (e) { setConn(false); throw e; }
  const [projects] = await Promise.all([api('GET', '/api/projects')]);
  state.projects = projects.projects || [];
  const sel = $('#projectSelect');
  sel.innerHTML = state.projects.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('') ||
    '<option value="">（无项目，请载入示例）</option>';
  if (state.projects.length) {
    state.projectId = state.projects[0].id;
    sel.value = state.projectId;
  }
}

async function loadViewData() {
  const pid = state.projectId;
  if (!pid) {
    state.episodes = []; state.shots = []; state.assets = []; state.tasks = []; state.prompts = [];
    return;
  }
  const [eps, shots, assets, tasks, prompts] = await Promise.all([
    api('GET', '/api/episodes/tree?project_id=' + encodeURIComponent(pid)),
    api('GET', '/api/shots'),
    api('GET', '/api/assets?project_id=' + encodeURIComponent(pid)),
    api('GET', '/api/tasks'),
    api('GET', '/api/prompts'),
  ]);
  state.episodes = eps.episodes || [];
  state.shots = shots.shots || [];
  state.assets = assets.assets || [];
  state.tasks = tasks.tasks || [];
  state.prompts = prompts.prompts || [];
  if (!state.episodeId && state.episodes.length) state.episodeId = state.episodes[0].id;
}

function setConn(on) {
  const dot = $('.status-dot');
  dot.classList.toggle('on', on);
  $('#connStatus').textContent = on ? '服务已连接' : '连接失败';
}

function curEpisode() { return state.episodes.find(e => e.id === state.episodeId) || state.episodes[0]; }

// 确保有当前项目：无项目时先按表单新建，避免空 projectId 拼出非法 URL / 空字段
async function ensureProject(form) {
  if (state.projectId) return state.projectId;
  const payload = {
    name: (form?.name || '').trim() || '未命名项目',
    aspect_ratio: form?.aspect_ratio || '9:16',
    style_preset: form?.style_preset || 'realistic_3d',
    budget_total: form?.budget_total || 0,
  };
  const r = await api('POST', '/api/projects', payload);
  await loadBase();                       // 刷新项目下拉与 state.projects
  state.projectId = r.project.id;
  const sel = $('#projectSelect'); if (sel) sel.value = state.projectId;
  toast(`已新建项目「${r.project.name}」`, 'ok');
  return state.projectId;
}

// ---------- 视图：总览 ----------
function viewOverview() {
  const p = state.projects.find(x => x.id === state.projectId);
  const approved = state.assets.filter(a => a.approved).length;
  const succeeded = state.tasks.filter(t => t.status === 'succeeded').length;
  const totalShots = state.episodes.reduce((n, e) => n + (e.shot_count || 0), 0);
  return `
    <div class="notice">当前为本地工作台：所有生成算力为桩，不调用外部 API、不产生真实费用；版权守卫与成本闸门全程生效。真实算力接入按规划后置。</div>
    <div class="section-title"><span class="bar"></span>${esc(p ? p.name : '尚无项目')}</div>
    <div class="grid grid-4">
      <div class="card"><div class="card-title">剧集 / 镜头</div><div class="stat-num accent">${state.episodes.length} / ${totalShots}</div><div class="stat-sub">已建剧集与拆分镜头</div></div>
      <div class="card"><div class="card-title">已批准资产</div><div class="stat-num">${approved}/${state.assets.length}</div><div class="stat-sub">来源合规 + 人工批准</div></div>
      <div class="card"><div class="card-title">提示词版本</div><div class="stat-num">${state.prompts.length}</div><div class="stat-sub">权威 H3 Schema 产出</div></div>
      <div class="card"><div class="card-title">已完成生成</div><div class="stat-num">${succeeded}</div><div class="stat-sub">桩任务（采用版）</div></div>
    </div>
    <div class="card mt"><div class="card-title">制作链路</div>
      ${stepper(0)}
      <p class="muted small">左侧「八步制作流程」逐环节展开：项目 → 剧本 → 拆镜 → 参考资产 → 提示词 → 提交生成 → 监控挑片 → 精修成片。「剧本 / 分集管理」维护多集目录，「合成完整视频」按版本拼接成片。全程原创示例，零版权风险。</p>
    </div>
    <div class="card mt"><div class="card-title">快速开始</div>
      <p class="small">首次使用点右上角 <b>载入示例</b>，一键生成多集原创示例（剧本、拆镜、资产、提示词、桩任务、合成记录），再按左侧流程逐步查看。</p>
    </div>`;
}

// ---------- 视图：剧本 / 分集管理 ----------
function viewScript() {
  const eps = state.episodes;
  const rows = eps.map(e => `<tr class="ep-row ${e.id === state.episodeId ? 'ep-active' : ''}" data-eid="${esc(e.id)}" style="cursor:pointer">
    <td>${e.index}</td><td>${esc(e.title || ('第' + e.index + '集'))}</td>
    <td>${badge(e.status, e.status === '已拆镜' ? 'green' : 'amber')}</td>
    <td>${e.shot_count || 0}</td><td>${e.prompt_count || 0} / ${e.approved_count || 0}</td>
    <td>${e.duration_target || 60}s</td>
    <td><button class="btn btn-sm btn-ghost act-enter-flow" data-eid="${esc(e.id)}">进入流程</button></td></tr>`).join('');
  const ep = curEpisode();
  return `
    <div class="notice">本页负责<b>整部剧本的批量建设</b>：设定制作参数、批量生成/导入多集，生成后目录即时更新。单集的拆镜、提示词、生成等<b>逐步精加工</b>请到左侧「八步制作流程」——点击剧集行的「进入流程」即可带着该集过去。</div>
    <div class="section-title"><span class="bar"></span>本项目制作设置</div>
    <div class="card">
      <div class="inline small">
        ${badge('横版 1920×1080', 'blue')}${badge('写实风格', 'gray')}
        <span class="spacer"></span><span class="muted">预算 ¥${state.projects.find(p=>p.id===state.projectId)?.budget_total || 0}</span>
      </div>
    </div>
    <div class="section-title mt"><span class="bar"></span>剧集目录（${eps.length} 集）</div>
    <div class="card"><div class="table-wrap"><table class="table">
      <thead><tr><th>集</th><th>标题</th><th>状态</th><th>镜头</th><th>提示词/批准</th><th>目标时长</th><th>操作</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7">${empty('▤','暂无剧集，先在下方生成或导入，或载入示例')}</td></tr>`}</tbody>
    </table></div></div>
    <div class="card mt">
      <div class="tabs">
        <div class="tab active" data-tab="ai">AI 生成多集剧本</div>
        <div class="tab" data-tab="import">导入整部剧本</div>
      </div>
      <div id="tab-ai">
        <p class="small muted">输入故事梗概，规划集数，逐集生成正文并自动保存到左侧分集目录。此为桩：生成逻辑由本地规则演示，真实 LLM 后置。</p>
        <div class="field"><textarea id="storyIdea" class="textarea" placeholder="输入故事梗概…（示例：一个旅人十年后回到江边码头，寻找失散的故人）"></textarea></div>
        <div class="field-row mt">
          <div class="field" style="max-width:160px"><label>生成 / 续写集数</label><input id="epCount" class="input" type="number" value="1" min="1"></div>
          <div class="field" style="max-width:160px"><label>每集目标时长（秒）</label><input id="epDuration" class="input" type="number" value="60" min="10"></div>
        </div>
        <div class="inline mt"><button class="btn" id="btnGenEpisodes">AI 新建多集剧本</button>
          <span class="muted small">续写方向可留空，按已有剧本承接。当前选中：${esc(ep ? ep.title : '无')}</span></div>
      </div>
      <div id="tab-import" class="hidden">
        <p class="small muted">整剧本自动按节 / 章 / 集分集。每一行场景自动拆为镜头。支持 TXT / Markdown。</p>
        <div class="field"><textarea id="fullScript" class="textarea" placeholder="粘贴整部剧本…"></textarea></div>
        <button class="btn mt" id="btnImportFull">导入并自动分集</button>
      </div>
    </div>`;
}

// ---------- 视图：八步制作流程（方案定稿：项目→剧本→拆镜→参考资产→提示词→提交→挑片→精修成片）----------
function viewWorkflow() {
  const ep = curEpisode();
  const step = state.wfStep;
  const builders = [wfProject, wfScript, wfStoryboard, wfReference, wfPrompt, wfSubmit, wfReview, wfFinalize];
  const body = builders[step](ep);
  return `
    <div class="notice-warn notice">当前剧集：<b>${esc(ep ? ep.title : '未选择')}</b> · ${ep ? ep.shot_count : 0} 个镜头。可在顶部切换项目 / 到「剧本 / 分集管理」切换剧集。</div>
    ${stepper(step)}
    <div id="wfBody">${body}</div>`;
}

function stepHead(no, title) {
  return `<div class="section-title"><span class="bar"></span>${esc(title)} · 第 ${no} 步</div>`;
}
function pointList(points) {
  return `<div class="fp-list">${points.map(p => `<span class="fp">${esc(p)}</span>`).join('')}</div>`;
}

// 第 1 步：创建项目（制作设置 → 生成剧集骨架）
function wfProject(ep) {
  const p = state.projects.find(x => x.id === state.projectId) || {};
  const eps = state.episodes;
  const epsRows = eps.map(e => `<tr><td>${e.index}</td><td>${esc(e.title || ('第'+e.index+'集'))}</td>
    <td>${e.duration_target || 60}s</td><td>${badge(e.status||'待制作', e.status==='已拆镜'?'green':'amber')}</td></tr>`).join('');
  return `
    ${stepHead(1, '创建项目')}
    ${pointList(['新建项目', '制作设置', '生成剧集骨架'])}
    <div class="card"><div class="card-title">当前项目</div>
      <div class="inline small">${badge('画幅 ' + (p.aspect_ratio||'—'),'blue')}${badge('风格 ' + (p.style_preset||'—'),'gray')}
        <span class="spacer"></span><span class="muted">预算 ¥${p.budget_total||0}</span></div>
      <div class="field-row mt">
        <div class="field"><label>项目名</label><input id="prjName" class="input" value="${esc(p.name||'')}"></div>
        <div class="field"><label>画幅</label><select id="prjAspect" class="select">
          <option value="9:16" ${p.aspect_ratio==='9:16'?'selected':''}>9:16 竖版</option>
          <option value="16:9" ${p.aspect_ratio==='16:9'?'selected':''}>16:9 横版</option></select></div>
        <div class="field"><label>风格</label><select id="prjStyle" class="select">
          <option value="realistic_3d" ${p.style_preset==='realistic_3d'?'selected':''}>3D 写实</option>
          <option value="comic" ${p.style_preset==='comic'?'selected':''}>漫剧</option></select></div>
      </div>
      <div class="field-row mt">
        <div class="field"><label>集数</label><input id="prjEpCount" class="input" type="number" value="${eps.length||3}" min="1"></div>
        <div class="field"><label>单集时长（秒）</label><input id="prjDuration" class="input" type="number" value="60" min="10"></div>
        <div class="field"><label>总预算（¥）</label><input id="prjBudget" class="input" type="number" value="${p.budget_total||0}"></div>
      </div>
      <div class="inline mt"><button class="btn btn-green" id="btnSaveProject">保存设置并生成骨架</button>
        <span class="muted small">已有剧集保持不变，仅补齐缺少的集数。</span></div>
    </div>
    <div class="card mt"><div class="card-title">剧集骨架（${eps.length} 集）</div>
      <div class="table-wrap"><table class="table"><thead><tr><th>集</th><th>标题</th><th>时长</th><th>状态</th></tr></thead>
      <tbody>${epsRows || `<tr><td colspan="4">${empty('▤','暂无剧集')}</td></tr>`}</tbody></table></div>
    </div>`;
}

// 第 2 步：剧本（梗概生成 / 整本导入 / AI 改写 / 版本链）
function wfScript(ep) {
  const eid = state.episodeId;
  const versions = state.detail.scriptVersions || [];
  const curText = ep?.script_text || '';
  const verRows = versions.map(v => `<tr>
    <td>v${v.version}</td><td class="small">${esc(v.source||'—')}</td>
    <td>${v.confirmed?badge('已确认','green'):badge('未确认','amber')}</td>
    <td class="small">${esc((v.instruction||'').slice(0,24)||'—')}</td>
    <td>${v.confirmed?'':`<button class="btn btn-sm btn-green act-confirm-ver" data-vid="${esc(v.id)}">确认</button>`}
      <button class="btn btn-sm btn-ghost act-view-ver" data-vid="${esc(v.id)}">查看</button></td></tr>`).join('');
  return `
    ${stepHead(2, '剧本')}
    ${pointList(['梗概生成', '导入原稿', 'AI 改写', '版本历史', '人工确认'])}
    <div class="notice">本步面向<b>当前选中剧集</b>做剧本精加工与版本管理。若要<b>批量新建整部多集</b>，请到左侧「剧本 / 分集管理」——那里生成后目录即时更新，不会跳转。可先在此用梗概/导入建好剧集，再逐集改写确认。</div>
    <div class="card"><div class="tabs">
      <div class="tab active" data-tab="gen">梗概生成</div>
      <div class="tab" data-tab="imp">导入原稿</div>
      <div class="tab" data-tab="rw">AI 改写</div>
    </div>
      <div id="tab-gen">
        <p class="small muted">输入原创梗概，LLM 桩按集生成大纲并保存版本链；真实 LLM 后置。全程原创、合规守卫前置。</p>
        <div class="field"><textarea id="genSynopsis" class="textarea" placeholder="输入故事梗概…"></textarea></div>
        <div class="field-row mt">
          <div class="field" style="max-width:150px"><label>生成集数</label><input id="genEpisodes" class="input" type="number" value="1" min="1"></div>
          <div class="field" style="max-width:170px"><label>单集时长（秒）</label><input id="genDuration" class="input" type="number" value="60"></div>
        </div>
        <div class="inline mt"><button class="btn" id="btnGenOutline">生成分集大纲</button></div>
      </div>
      <div id="tab-imp" class="hidden">
        <p class="small muted">粘贴整部原稿，按「第 X 集」标题自动分集，每集正文落库并保存版本。</p>
        <div class="field"><textarea id="impBook" class="textarea" placeholder="第一集 初遇&#10;场景：江边&#10;林远：我回来了。&#10;&#10;第二集 夜渡…"></textarea></div>
        <div class="inline mt"><button class="btn" id="btnImportBook">导入并自动分集</button></div>
      </div>
      <div id="tab-rw" class="hidden">
        <p class="small muted">当前剧集：<b>${esc(ep?ep.title:'无')}</b>。改写保留前后对照，写入版本链。</p>
        <div class="field"><textarea id="rwInstruction" class="textarea" placeholder="改写指令，如：对白更口语化、节奏更紧凑…"></textarea></div>
        <div class="inline mt"><button class="btn" id="btnRewrite">AI 改写并保存版本</button></div>
      </div>
    </div>
    <div class="card mt"><div class="card-title">当前正文（${esc(ep?ep.title:'无')}）</div>
      <pre class="code mt" style="max-height:180px;overflow:auto">${esc(curText||'（暂无正文，先生成或导入）')}</pre></div>
    <div class="card mt"><div class="card-title">版本历史</div>
      <div class="table-wrap"><table class="table"><thead><tr><th>版本</th><th>来源</th><th>确认</th><th>改写指令</th><th>操作</th></tr></thead>
      <tbody>${verRows || `<tr><td colspan="5">${empty('▤','暂无版本，先生成或改写')}</td></tr>`}</tbody></table></div>
    </div>`;
}

// 第 3 步：拆镜（自动拆镜 / 缺字段高亮 / 镜头编辑）
function wfStoryboard(ep) {
  const shots = state.shots.filter(s => s.episode_id === state.episodeId);
  const missingMap = {};
  (state.detail.shotMissing?.missing_fields || []).forEach(x => { missingMap[x.shot_id] = x.missing; });
  const rows = shots.map(s => {
    const miss = missingMap[s.id] || [];
    return `<tr>
      <td>${s.index}</td><td>${esc(s.location||'—')}</td>
      <td class="small">${esc((s.action||'—').slice(0,50))}</td>
      <td class="small">${esc((s.dialogue||'—').slice(0,50))}</td>
      <td>${s.duration}s</td>
      <td>${miss.length?badge('缺：'+miss.join('/'),'amber'):badge('完整','green')}</td>
      <td><button class="btn btn-sm btn-ghost act-edit-shot" data-sid="${esc(s.id)}">编辑</button></td></tr>`;
  }).join('');
  return `
    ${stepHead(3, '拆镜')}
    ${pointList(['自动拆镜', '缺字段高亮', '镜头编辑'])}
    <div class="notice">按场景 / 动作 / 对白把正文拆成结构化镜头；缺字段高亮可人工补全。拆镜可重跑（覆盖本集镜头）。</div>
    <div class="card"><div class="field"><label>拆镜用正文（留空则取当前剧集正文）</label>
      <textarea id="splitText" class="textarea" style="min-height:90px" placeholder="场景：江边旧码头&#10;林远：我回来了。&#10;（他望向江面）"></textarea></div>
      <div class="inline mt"><button class="btn" id="btnSplitShots">自动拆镜</button>
        <button class="btn btn-ghost" id="btnCheckMissing">检查缺字段</button>
        <span class="muted small">默认每段 5 秒，可在镜头编辑中调整。</span></div>
    </div>
    <div class="card mt"><div class="card-title">镜头表（${shots.length} 个镜头）</div>
      <div class="table-wrap"><table class="table"><thead><tr><th>#</th><th>场景</th><th>动作</th><th>对白</th><th>时长</th><th>检查</th><th>操作</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7">${empty('▤','暂无镜头，点「自动拆镜」')}</td></tr>`}</tbody></table></div>
    </div>`;
}

// 第 4 步：参考资产（登记 / 批准 / 绑定参考图并锁版本）
function wfReference(ep) {
  const epshots = state.shots.filter(s => s.episode_id === state.episodeId);
  const approved = state.assets.filter(a => a.approved && a.project_id === state.projectId);
  const cards = epshots.map(s => {
    const refs = (s.asset_refs || []).map(id => state.assets.find(a => a.id === id)).filter(Boolean);
    const thumbs = refs.map((a, i) => `<div class="shot-thumb" style="height:70px">▧<span class="thumb-tag">v${a.version||1} · ${esc(a.name)}</span></div>`).join('') ||
      '<div class="shot-thumb" style="height:70px">＋<span class="thumb-tag">未设参考图</span></div>';
    const opts = approved.map(a => `<label class="cfg-check"><input type="checkbox" class="ref-opt" data-sid="${esc(s.id)}" value="${esc(a.id)}" ${ (s.asset_refs||[]).includes(a.id)?'checked':''}> ${esc(a.name)} (${esc(a.type)})</label>`).join('');
    return `<div class="card mt">
      <div class="inline"><b>镜头 ${s.index}</b> ${badge(s.duration+'s','gray')} ${badge('锁定 v'+(s.asset_version||1),'blue')}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(90px,1fr));gap:6px" class="mt">${thumbs}</div>
      <div class="small muted mt">可绑定资产（仅已批准）：${opts || '（资产库暂无已批准资产）'}</div>
      <div class="inline mt"><button class="btn btn-sm btn-green act-bind-refs" data-sid="${esc(s.id)}">保存参考图</button>
        <button class="btn btn-sm btn-ghost act-goto-assets">去资产库登记/批准</button></div>
    </div>`;
  }).join('');
  return `
    ${stepHead(4, '参考资产')}
    ${pointList(['登记资产', '人工批准', '绑定参考图', '锁定版本'])}
    <div class="notice">未批准资产禁止引用；绑定时锁定 asset_version 保证一致性。到「资产库」登记角色/场景并记录来源声明。</div>
    ${cards || empty('▦','暂无镜头')}`;
}

// 第 5 步：规范提示词
function wfPrompt(ep) {
  const eid = state.episodeId;
  const epshots = state.shots.filter(s => s.episode_id === eid);
  const promptsByShot = {};
  state.prompts.forEach(p => { (promptsByShot[p.shot_id] = promptsByShot[p.shot_id] || []).push(p); });
  const roles = state.assets.filter(a => a.type === 'role' && a.project_id === state.projectId);
  const roleChips = roles.map(r => `${esc(r.name)} ${badge((r.appearance_json?.gender==='男'?'男':'女') + ' · 已识别','green')}`).join('　');
  const cards = epshots.map(s => {
    const vers = promptsByShot[s.id] || [];
    const ap = vers.find(p => p.approved) || vers[vers.length - 1];
    return `<div class="card mt">
      <div class="inline"><b>镜头 ${s.index}</b> ${badge('R2V','blue')} ${badge(s.duration+'s','gray')}
        <span class="spacer"></span>
        ${ap ? badge('v'+ap.version+(ap.approved?' · approved':''),'green') : badge('未扩写','amber')}</div>
      <div class="small muted mt">${esc((s.action||s.location||'').slice(0,80))}</div>
      ${ap ? `<pre class="code mt">${esc(ap.content)}</pre>
        <div class="inline mt">
          ${badge('评分 '+(ap.score||'—'),'blue')}
          <button class="btn btn-sm btn-ghost act-prompt-view" data-pid="${esc(ap.id)}">查看版本</button>
          <button class="btn btn-sm btn-ghost act-score" data-pid="${esc(ap.id)}">评分</button>
          ${ap.approved ? badge('已批准','green') : `<button class="btn btn-sm btn-green act-approve-prompt" data-pid="${esc(ap.id)}">批准</button>`}
        </div>` : `<div class="inline mt"><button class="btn btn-sm act-expand-one" data-sid="${esc(s.id)}">扩写此镜头</button></div>`}
    </div>`;
  }).join('');
  return `
    ${stepHead(5, '提示词')}
    ${pointList(['规范扩写', '批量扩写', '批准评分'])}
    <div class="notice">角色性别识别：${roleChips || '（无角色资产）'}</div>
    <div class="card"><div class="inline">
      <button class="btn" id="btnBatchPrompt">+ 生成 H3 规范提示词（批量）</button>
      <button class="btn btn-ghost" id="btnClearPrompts">清空未批准提示词</button>
      <span class="muted small">扩写前自动过版权守卫 + 内容安全过滤。</span>
    </div></div>
    ${cards || empty('✎','暂无镜头')}`;
}

// 第 6 步：提交生成
function wfSubmit(ep) {
  const epshots = state.shots.filter(s => s.episode_id === state.episodeId);
  const done = epshots.filter(s => state.tasks.some(t => t.shot_id === s.id && t.status === 'succeeded'));
  const cards = epshots.map(s => {
    const t = state.tasks.find(x => x.shot_id === s.id);
    return `<div class="shot-card"><div class="shot-thumb">▤<span class="thumb-tag">${esc((s.location||'').slice(0,10))}</span></div>
      <div class="shot-card-body"><div class="shot-card-title"><span>镜头 ${s.index}</span>${statusBadge(t?.status)}</div>
      <div class="shot-card-meta">R2V · ${s.duration}s · 1MP</div></div></div>`;
  }).join('');
  const pct = epshots.length ? Math.round(done.length / epshots.length * 100) : 0;
  return `
    ${stepHead(6, '提交生成')}
    ${pointList(['成本闸门', '批量提交', '停止任务'])}
    <div class="card">
      <label class="cfg-check"><input type="checkbox" id="optChain" checked> 链式锁（上段未锁 → 下段前断）</label>
      <label class="cfg-check"><input type="checkbox" id="optReview"> 连接审核（需人工确认上一段）</label>
      <div class="field-row mt">
        <div class="field"><label>成片模式</label><select id="subMode" class="select"><option value="r2v">R2V 语义参考</option><option value="t2v">T2V 文生视频</option><option value="i2v">I2V 图生视频</option></select></div>
        <div class="field" style="max-width:120px"><label>步数</label><input id="subSteps" class="input" type="number" value="20"></div>
        <div class="field" style="max-width:150px"><label>分辨率</label><select id="subRes" class="select"><option>768P</option><option>1080P</option><option>2K</option></select></div>
        <div class="field" style="max-width:140px"><label>预估成本（分/段）</label><input id="subEst" class="input" type="number" value="50"></div>
      </div>
      <div class="inline mt"><label class="cfg-check" style="margin:0"><input type="checkbox" id="optAll" checked> 全选</label>
        <button class="btn" id="btnSubmitBatch">提交任务</button>
        <button class="btn btn-danger" id="btnStopAll">一键停止</button>
        <span class="muted small">算力为桩，不调用真实 API。</span></div>
      <div class="mt" style="background:#eef2f7;border-radius:8px;height:8px;overflow:hidden"><div style="height:100%;width:${pct}%;background:var(--green)"></div></div>
      <div class="small muted mt">已处理 ${epshots.length} 段 · 成功 ${done.length} · 失败 0</div>
    </div>
    <div class="shot-grid mt">${cards || empty('⚙','暂无镜头')}</div>`;
}

// 第 7 步：监控挑片（候选 / 采用 / 失败诊断 / 重试）
function wfReview(ep) {
  const groups = state.detail.candidates?.groups || [];
  const failures = state.detail.failures?.failures || [];
  const shots = {}; state.shots.forEach(s => shots[s.id] = s);
  const shotCards = state.shots.filter(s => s.episode_id === state.episodeId).map(s => `
    <div class="inline" style="padding:8px 0;border-bottom:1px dashed var(--border)">
      <b class="small">镜头 ${s.index}</b> <span class="muted small">${esc((s.location||s.action||'').slice(0,30))}</span>
      <span class="spacer"></span>
      <button class="btn btn-sm act-gen-cand" data-sid="${esc(s.id)}">生成候选</button>
    </div>`).join('');
  const groupCards = groups.map(g => `
    <div class="card mt"><div class="inline"><b class="small">镜头 ${shots[g.shot_id]?.index ?? '—'}</b>
      ${statusBadge(g.task_status)} ${badge(g.candidates.length+' 个候选','blue')}</div>
      <div class="cand-grid mt">${g.candidates.map(c => `
        <div class="cand-item ${c.id===g.adopted?'cand-adopted':''}">
          <div class="shot-thumb" style="height:66px">▤<span class="thumb-tag">v${c.version||1}</span></div>
          <div class="small">候选 ${c.version||1} ${c.id===g.adopted?badge('采用版','green'):''}</div>
          <button class="btn btn-sm ${c.id===g.adopted?'btn-ghost':'btn-green'} act-adopt" data-oid="${esc(c.id)}" ${c.id===g.adopted?'disabled':''}>${c.id===g.adopted?'已采用':'挑片采用'}</button>
        </div>`).join('')}</div>
    </div>`).join('');
  const failRows = failures.map(f => `<tr>
    <td class="small">${esc(shots[f.task.shot_id]?.location || ('镜头'+(shots[f.task.shot_id]?.index||'')))}</td>
    <td>${badge(f.code,'red')}</td>
    <td class="small">${esc(f.spec?.diagnostic||'—')}</td>
    <td class="small">${esc(f.spec?.action||'—')}</td>
    <td><button class="btn btn-sm act-retry" data-tid="${esc(f.task.id)}">一键重试</button></td></tr>`).join('');
  return `
    ${stepHead(7, '监控挑片')}
    ${pointList(['生成候选', '挑片采用', '失败诊断', '一键重试'])}
    <div class="card"><div class="card-title">逐镜头生成候选（每段 2–4 个，桩）</div>${shotCards || empty('▤','暂无镜头')}</div>
    ${groupCards || empty('▦','暂无候选。点上方「生成候选」')}
    <div class="card mt"><div class="card-title">失败诊断与重试</div>
      <div class="table-wrap"><table class="table"><thead><tr><th>镜头</th><th>失败码</th><th>诊断</th><th>建议动作</th><th>操作</th></tr></thead>
      <tbody>${failRows || `<tr><td colspan="5">${empty('✓','暂无失败任务')}</td></tr>`}</tbody></table></div>
    </div>`;
}

// 第 8 步：精修成片（装配计划 / 字幕 / 升清 / 导出）
function wfFinalize(ep) {
  const d = state.detail.finalize || {};
  const plan = d.plan || {};
  const up = plan.upscale || '—';
  const cueRows = (d.cues || []).map((c,i) => `<tr><td>${i+1}</td><td>${c.start}s → ${c.end}s</td><td>${esc(c.text||'')}</td></tr>`).join('');
  return `
    ${stepHead(8, '精修成片')}
    ${pointList(['装配计划', '字幕文件', '升清策略', '导出成片'])}
    <div class="notice">按采用版镜头生成「裁头 + 拼接 + 字幕 + CTA + 升清」装配计划；真实转码依赖 ffmpeg / 升清算力，已后置，此处生成可审查计划与成片记录。</div>
    <div class="card"><div class="field-row">
      <div class="field"><label>算力后端（决定升清策略）</label><select id="finBackend" class="select">
        <option value="api">云端 API → 768P 重生 2K</option>
        <option value="local_comfyui">本地 ComfyUI → SeedVR2 超分</option></select></div>
      <div class="field"><label>片尾 CTA</label><input id="finCta" class="input" value="关注我们 · 下集见"></div>
      <div class="field" style="display:flex;align-items:flex-end"><button class="btn" id="btnFinPlan">生成装配计划</button></div>
    </div></div>
    <div class="card mt"><div class="inline">${badge('采用片段 '+(d.adopted_clips||0),'blue')}${badge('升清 '+up,'gray')}</div>
      <div class="field mt"><label>SRT 字幕（可复制保存为 subtitles.srt）</label>
        <pre class="code" style="max-height:160px;overflow:auto">${esc(d.srt||'（点「生成装配计划」）')}</pre></div>
      <div class="table-wrap mt"><table class="table"><thead><tr><th>#</th><th>时间</th><th>字幕</th></tr></thead>
      <tbody>${cueRows || `<tr><td colspan="3">${empty('▤','暂无字幕')}</td></tr>`}</tbody></table></div>
    </div>
    <div class="card mt"><div class="card-title">导出成片记录</div>
      <div class="field-row">
        <div class="field"><label>成片名</label><input id="finName" class="input" value="精修成片"></div>
        <div class="field"><label>版本</label><input id="finVer" class="input" value="v1"></div>
        <div class="field" style="display:flex;align-items:flex-end"><button class="btn btn-green" id="btnFinExport">导出成片</button></div>
      </div>
      <p class="small muted mt">成片文件不入库，只记录引用；完整历史见「合成完整视频」。</p>
    </div>`;
}

// ---------- 视图：资产库 ----------
let assetFilter = 'all';
function viewAssetLib() {
  const st = state.detail.assetStats?.stats || { all:0, role:0, scene:0, prop:0, image:0 };
  let assets = state.detail.assetStats?.assets || [];
  if (assetFilter !== 'all') assets = assets.filter(a => a.type === assetFilter || (assetFilter==='image'&&a.type==='storyboard'));
  const tabs = [['all','全部'],['role','角色'],['scene','场景'],['prop','道具'],['image','图像']]
    .map(([k,l]) => `<div class="tab ${assetFilter===k?'active':''}" data-af="${k}">${l} ${st[k]||0}</div>`).join('');
  const cards = assets.map(a => `<div class="shot-card"><div class="shot-thumb">${a.type==='role'?'👤':(a.type==='scene'?'▧':'◈')}<span class="thumb-tag">v${a.version||1}</span></div>
    <div class="shot-card-body"><div class="shot-card-title"><span>${esc(a.name)}</span>${a.approved?badge('已批准','green'):badge('待批准','amber')}</div>
    <div class="shot-card-meta">类型：${esc(a.type)} · 来源：${esc(a.provenance?.source||'—')}</div>
    ${a.appearance_json?.gender?`<div class="small">性别：${esc(a.appearance_json.gender)}</div>`:''}</div></div>`).join('');
  return `
    <div class="section-title"><span class="bar"></span>资产库</div>
    <div class="notice">资产只属于当前账号；保存后的角色、场景、道具图像可被项目复制引用，原项目不被修改。来源声明 + 人工批准双门槛。</div>
    <div class="card"><div class="tabs">${tabs}</div>
      <div class="field" style="max-width:320px"><input id="assetSearch" class="input" placeholder="搜索资产名称或描述…"></div>
      <div class="shot-grid mt">${cards || empty('🖼','暂无匹配的资产。可在第 3 步参考资产卡上存入资产库。')}</div>
    </div>`;
}

// ---------- 视图：合成完整视频 ----------
function viewCompose() {
  const recs = state.detail.composeRecords || [];
  const rows = recs.map(r => `<div class="col" style="min-width:240px;flex:1">
    <div class="card">
      <div class="shot-thumb" style="border-radius:8px">▶<span class="thumb-tag">${esc(r.version)}</span></div>
      <div class="mt"><b class="small">${esc(r.name)}</b></div>
      <div class="small muted">${esc(r.episode_range)} · ${r.duration}s · ${r.size_mb}MB</div>
      <div class="small muted">${esc((r.created_at||'').replace('T',' ').slice(0,19))}</div>
      <div class="inline mt"><button class="btn btn-sm btn-ghost act-compose-open" data-url="${esc(r.output_url)}">新窗口打开</button>
        <button class="btn btn-sm btn-danger act-compose-del" data-id="${esc(r.id)}">删除</button></div>
    </div></div>`).join('');
  return `
    <div class="section-title"><span class="bar"></span>合成完整视频</div>
    <div class="notice">把所有已采用的视频片段按剧集顺序拼接成完整成片（真实转码依赖 ffmpeg，已后置；此处为记录管理）。</div>
    <div class="card"><div class="field-row">
      <div class="field"><label>合成范围</label><select id="composeRange" class="select"><option>第 1 → 全部</option><option>第 1 → 第 3 集</option></select></div>
      <div class="field" style="max-width:160px"><label>版本</label><input id="composeVer" class="input" value="v2"></div>
      <div class="field" style="display:flex;align-items:flex-end"><button class="btn btn-green" id="btnComposeCreate">一键合成完整视频</button></div>
    </div></div>
    <div class="section-title mt"><span class="bar"></span>合成历史（可反复合成，全部保留）</div>
    <div style="display:flex;gap:16px;flex-wrap:wrap">${rows || empty('▶','暂无合成记录')}</div>`;
}

// ---------- 视图：视频生成配置 ----------
function viewBackend() {
  const cfg = state.detail.videoCfg?.config?.params || state.detail.videoCfg?.defaults || {};
  const f = (key, label) => `<div class="cfg-row"><label>${label}</label><input class="input" data-cfg="${key}" value="${esc(cfg[key]??'')}"></div>`;
  const chk = (key, label) => `<label class="cfg-check"><input type="checkbox" data-cfg="${key}" ${cfg[key]?'checked':''}> ${label}</label>`;
  return `
    <div class="section-title"><span class="bar"></span>视频生成配置</div>
    <div class="notice notice-warn">仅存配置参数，不打包模型权重 / 密钥；真实算力接入后置。指定场景换场景不等于换工作流。</div>
    <div class="dark-panel">
      <div class="cfg-grid">
        <div>
          <div class="cfg-group-title">H3 R2V + VAD</div>
          ${f('ref2va_diffusion','Ref2VA 扩散模型')}
          ${chk('ref2va_attention_accel','Ref2VA Attention 加速')}
          ${f('cache_reuse_threshold','成片缓存复用阈值')}
          ${f('cache_result_ratio','成片缓存结果比例')}
          ${f('ref2va_text_encoder','Ref2VA 文本编码器')}
          ${chk('video_easycache','视频 EasyCache（每 N 步计算，会损失细节）')}
          ${f('cache_start_ratio','成片缓存开始比例')}
        </div>
        <div>
          <div class="cfg-group-title">SeedVR2 超分</div>
          ${f('seedvr2_main','SeedVR2 主模型')}
          ${chk('use_seedvr2','视频生成后使用 SeedVR2 超分')}
          ${f('seedvr2_vae','SeedVR2 VAE')}
          ${f('superres_target','超分目标')}
          ${f('superres_tile','超分块大小')}
          ${f('vram_limit','显存占用上限')}
          ${f('cpu_offload_tiles','超分 CPU 卸载块数')}
        </div>
      </div>
      <div class="inline mt"><button class="btn btn-green" id="btnSaveCfg">保存配置</button>
        <span style="color:#9ca3af;font-size:12px">配置写入本地数据目录，不入库代码仓库。</span></div>
    </div>`;
}

// ---------- 视图：算力管理 ----------
function viewCompute() {
  const st = state.detail.compute || { available: [], active: '', backends: [] };
  const rows = (st.backends || []).map(b => {
    const caps = b.capabilities || {};
    const cfg = b.config || {};
    const capText = caps.mode === 'stub'
      ? `桩 · ${caps.node_count || caps.agents?.length || 0} 项能力`
      : `实时 · ${caps.node_count || caps.agents?.agents?.length || 0} 项能力`;
    return `<tr>
      <td><b>${esc(b.name)}</b>${b.active ? badge('当前', 'green') : ''}</td>
      <td>${esc(cfg.base_url || '—')}</td>
      <td>${esc(cfg.model || '—')}</td>
      <td>${badge(cfg.dry_run ? '内存桩' : '真实连接', cfg.dry_run ? 'amber' : 'green')}</td>
      <td>${b.healthy ? badge('就绪', 'green') : badge('不可用', 'red')}</td>
      <td class="small muted">${esc(capText)}</td>
      <td>${b.active ? badge('使用中', 'blue') : `<button class="btn btn-sm btn-ghost act-switch-be" data-kind="${esc(b.kind)}">切换</button>`}</td>
    </tr>`;
  }).join('');

  const comfy = (st.backends || []).find(b => b.kind === 'local_comfyui') || {};
  const wf = state.detail.comfyWorkflow;
  return `
    <div class="section-title"><span class="bar"></span>算力管理 · 可插拔后端</div>
    <div class="notice notice-warn">三种算力接口已做实并默认走内存桩：<b>零外连、零成本、零版权风险</b>。要接真实算力需服务端设 VS_COMPUTE_DRY_RUN=0 并配置端点与密钥引用（密钥只存环境变量名，绝不下发浏览器）。切换后端不改剧本 / 提示词 / 项目数据。</div>

    <div class="card"><div class="card-title">后端全景 <span class="muted small">当前激活：${esc(st.active || '—')}</span></div>
      <div class="table-wrap"><table class="table">
        <thead><tr><th>后端</th><th>端点</th><th>模型</th><th>模式</th><th>健康</th><th>能力</th><th>操作</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="7">${empty('⚙','暂无后端')}</td></tr>`}</tbody>
      </table></div>
    </div>

    <div class="card mt"><div class="card-title">接口自检 <span class="muted small">内存桩跑通 submit → poll → fetch，不调用真实算力</span></div>
      <div class="inline">
        <select id="simKind" class="select" style="max-width:200px">
          <option value="local_comfyui">本地 ComfyUI</option>
          <option value="cloud_api">云端 API</option>
          <option value="cloud_gpu">云 GPU</option>
        </select>
        <button class="btn" id="btnSimulate">运行自检</button>
        <button class="btn btn-ghost" id="btnRefreshCompute">刷新状态</button>
      </div>
      ${state.detail.simResult ? `<div class="mt"><div class="inline">${badge('任务 '+esc(state.detail.simResult.task_id),'blue')}
        ${badge(state.detail.simResult.states.join(' → '),'gray')}
        ${state.detail.simResult.artifact ? badge('产物 OK · sha256 '+esc(state.detail.simResult.artifact.sha256.slice(0,10)),'green') : badge('无产物','red')}</div>
        <pre class="code mt">${esc(JSON.stringify(state.detail.simResult.artifact || {}, null, 2))}</pre></div>` : ''}
    </div>

    <div class="card mt"><div class="card-title">ComfyUI 工作流预览 <span class="muted small">prompt graph 构建（纯函数，不提交）</span></div>
      <div class="inline">
        <input id="wfPrompt" class="input" style="max-width:360px" placeholder="提示词，如：原创测试短片">
        <select id="wfMode" class="select" style="max-width:110px"><option>r2v</option><option>t2v</option><option>i2v</option></select>
        <button class="btn btn-ghost" id="btnBuildWf">构建工作流</button>
      </div>
      ${wf ? `<pre class="code mt">${esc(JSON.stringify(wf, null, 2))}</pre>` : `<p class="small muted mt">点击「构建工作流」查看生成的 ComfyUI prompt graph（节点类型为默认模板，用户部署的节点可用 payload.workflow 覆盖）。</p>`}
    </div>

    <div class="card mt"><p class="small muted">诚实边界：接口做实 ≠ 已接真实算力。默认桩用于验证协议与流程闭环；真实调用需用户自备端点、密钥与模型权重，本平台不下载/打包任何权重，不产生任何版权素材复制。详见 ARCHITECTURE.md。</p></div>`;
}

// ---------- 视图：编辑专家 ----------
function viewExpert() {
  const experts = state.detail.experts || [];
  const sel = state.detail.selectedExpert || experts[0];
  const list = experts.map(e => `<div class="expert-item ${sel&&e.id===sel.id?'active':''}" data-eid="${esc(e.id)}">
    <div>${esc(e.name)} ${e.builtin?badge('内置','gray'):''}</div>
    <div class="ei-sub">${esc(e.category)} · ${esc(e.description)}</div></div>`).join('');
  return `
    <div class="section-title"><span class="bar"></span>编辑专家</div>
    <div class="notice">可编辑的 prompt 专家库：剧本生成 / 续写 / 拆解 / 提示词扩写 / 资产规划等。内置专家只读，可另存为自定义副本。</div>
    <div class="expert-layout">
      <div class="expert-list">${list || empty('✎','暂无专家')}</div>
      <div class="card expert-editor">
        ${sel ? `<div class="field"><label>名称</label><input id="expName" class="input" value="${esc(sel.name)}" ${sel.builtin?'disabled':''}></div>
        <div class="field-row mt"><div class="field"><label>分类</label><input id="expCat" class="input" value="${esc(sel.category)}" ${sel.builtin?'disabled':''}></div>
        <div class="field"><label>说明</label><input id="expDesc" class="input" value="${esc(sel.description)}" ${sel.builtin?'disabled':''}></div></div>
        <div class="field mt"><label>System Prompt</label><textarea id="expPrompt" class="textarea" style="min-height:340px" ${sel.builtin?'disabled':''}>${esc(sel.system_prompt)}</textarea></div>
        <div class="inline mt">${sel.builtin ? badge('内置专家 · 只读','gray') : `<button class="btn" id="btnSaveExpert">保存专家</button>`}
          ${sel.builtin ? `<button class="btn btn-ghost" id="btnCopyExpert">另存为自定义副本</button>` : ''}</div>`
        : empty('✎','从左侧选择专家')}
      </div>
    </div>`;
}

// ---------- 视图：成本闸门 / 合规审计 ----------
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

function viewCompliance() {
  const events = state.detail.auditEvents || [];
  const rows = events.slice().reverse().map(e => `<tr>
    <td>${e.seq}</td><td class="small">${esc(e.ts.replace('T',' ').slice(0,19))}</td>
    <td>${esc(e.event_type)}</td><td>${esc(e.subject)}</td>
    <td>${e.passed ? badge('放行','green') : badge('拦截','red')}</td>
    <td class="small">${esc((e.findings || []).map(f => f.message).join('；') || '—')}</td></tr>`).join('');
  const v = state.detail.auditVerify;
  return `
    <div class="section-title"><span class="bar"></span>合规审计日志（append-only + 哈希链）</div>
    <div class="card"><div class="inline">
      ${v ? (v.intact ? badge('哈希链完好','green') : badge('检测到篡改','red')) : ''}
      <span class="muted small">${esc(v?.detail || '')}</span><span class="spacer"></span>
      <button class="btn btn-sm btn-ghost" id="btnVerifyAudit">校验完整性</button></div></div>
    <div class="card mt"><div class="table-wrap"><table class="table">
      <thead><tr><th>#</th><th>时间(UTC)</th><th>事件</th><th>主体</th><th>判定</th><th>命中详情</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6">${empty('🛡','暂无审计记录')}</td></tr>`}</tbody>
    </table></div></div>
    <div class="card mt"><p class="small muted">诚实边界：守卫为「最小规则 + 抗绕过归一化」，提高门槛但非绝对保证；文件级 append-only 依赖哈希链检测篡改，真要防篡改需外部 WORM / 签名服务。详见 COMPLIANCE.md。</p></div>`;
}

// ---------- 视图路由 ----------
const VIEWS = {
  overview: { title: '总览', desc: '项目全流程一览', render: viewOverview },
  script: { title: '剧本 / 分集管理', desc: '多集目录 · AI 生成 · 导入分集', render: viewScript },
  workflow: { title: '八步制作流程', desc: '项目 → 剧本 → 拆镜 → 参考资产 → 提示词 → 提交 → 挑片 → 精修成片', render: viewWorkflow },
  assetlib: { title: '资产库', desc: '账号级资产 · 分类 · 来源合规', render: viewAssetLib },
  compose: { title: '合成完整视频', desc: '按版本拼接 · 合成历史', render: viewCompose },
  backend: { title: '视频生成配置', desc: 'H3 R2V / SeedVR2 模型参数（桩）', render: viewBackend },
  compute: { title: '算力管理', desc: '可插拔后端 · 接口自检 · ComfyUI 工作流', render: viewCompute },
  expert: { title: '编辑专家', desc: '可编辑的 prompt 专家库', render: viewExpert },
  cost: { title: '成本闸门', desc: '累计成本原子校验', render: viewCost },
  compliance: { title: '合规审计', desc: '只追加日志 + 哈希链防篡改', render: viewCompliance },
};

async function render() {
  const v = VIEWS[state.view];
  $('#viewTitle').textContent = v.title;
  $('#viewDesc').textContent = v.desc;
  document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === state.view));

  // 视图专属数据
  if (state.view === 'workflow') {
    const ep = curEpisode();
    if (state.wfStep === 1 && ep) {
      state.detail.scriptVersions = (await api('GET', '/api/script/versions?episode_id=' + encodeURIComponent(ep.id))).versions;
    } else if (state.wfStep === 2 && ep) {
      state.detail.shotMissing = await api('GET', '/api/shots/missing?episode_id=' + encodeURIComponent(ep.id));
    } else if (state.wfStep === 6 && ep) {
      const [cands, fails] = await Promise.all([
        api('GET', '/api/candidates?episode_id=' + encodeURIComponent(ep.id)),
        api('GET', '/api/tasks/failures?episode_id=' + encodeURIComponent(ep.id)),
      ]);
      state.detail.candidates = cands; state.detail.failures = fails;
    } else if (state.wfStep === 7) {
      state.detail.finalize = await api('GET', '/api/finalize/plan?project_id=' +
        encodeURIComponent(state.projectId) + '&episode_id=' + encodeURIComponent(state.episodeId));
    }
  } else if (state.view === 'assetlib') {
    state.detail.assetStats = await api('GET', '/api/asset-stats');
  } else if (state.view === 'compose') {
    state.detail.composeRecords = (await api('GET', '/api/compose/records?project_id=' + encodeURIComponent(state.projectId))).records;
  } else if (state.view === 'backend') {
    state.detail.videoCfg = await api('GET', '/api/video-config');
  } else if (state.view === 'compute') {
    state.detail.compute = await api('GET', '/api/backends');
  } else if (state.view === 'expert') {
    state.detail.experts = (await api('GET', '/api/experts')).experts;
    if (!state.detail.selectedExpert || !state.detail.experts.find(e=>e.id===state.detail.selectedExpert.id)) {
      state.detail.selectedExpert = state.detail.experts[0];
    }
  } else if (state.view === 'cost') {
    const d = await api('GET', '/api/cost/overview?project_id=' + encodeURIComponent(state.projectId));
    state.detail.ledger = d.ledger; state.detail.price_rules = d.price_rules;
  } else if (state.view === 'compliance') {
    const [ev, vf] = await Promise.all([api('GET', '/api/audit'), api('GET', '/api/audit/verify')]);
    state.detail.auditEvents = ev.events || []; state.detail.auditVerify = vf;
  }
  content().innerHTML = v.render();
  bindViewEvents();
}

// ---------- 事件绑定 ----------
function bindViewEvents() {
  // 八步 stepper 点击
  content().querySelectorAll('.step-node').forEach(el => el.onclick = () => {
    if (state.view === 'overview') { state.view = 'workflow'; }
    state.wfStep = parseInt(el.dataset.step);
    render().catch(e => toast(e.message, 'err'));
  });

  // === 算力管理 ===
  if (state.view === 'compute') {
    const btnSim = $('#btnSimulate');
    if (btnSim) btnSim.onclick = async () => {
      try {
        toast('运行中…');
        state.detail.simResult = await api('POST', '/api/compute/simulate',
          { kind: $('#simKind').value, mode: 't2v' });
        await render();
        toast('接口自检通过（内存桩，零成本）', 'ok');
      } catch (e) { toast(e.message, 'err'); }
    };
    const btnRefresh = $('#btnRefreshCompute');
    if (btnRefresh) btnRefresh.onclick = async () => {
      state.detail.compute = await api('GET', '/api/backends');
      await render(); toast('算力状态已刷新', 'ok');
    };
    const btnWf = $('#btnBuildWf');
    if (btnWf) btnWf.onclick = async () => {
      try {
        state.detail.comfyWorkflow = (await api('POST', '/api/comfyui/workflow', {
          prompt: $('#wfPrompt').value || '原创测试短片',
          mode: $('#wfMode').value,
        })).workflow;
        await render();
        toast('工作流已构建', 'ok');
      } catch (e) { toast(e.message, 'err'); }
    };
    document.querySelectorAll('.act-switch-be').forEach(el => el.onclick = async () => {
      try {
        await api('POST', '/api/compute/switch', { kind: el.dataset.kind });
        state.detail.simResult = null;
        await render();
        toast('已切换当前算力后端（上层数据不变）', 'ok');
      } catch (e) { toast(e.message, 'err'); }
    });
  }

  // 剧本步骤 tabs（梗概/导入/改写）
  if (state.view === 'workflow' && state.wfStep === 1) bindGroupTabs('gen');

  // === 第 1 步：项目 ===
  const btnSaveProject = $('#btnSaveProject');
  if (btnSaveProject) btnSaveProject.onclick = async () => {
    try {
      const form = {
        name: $('#prjName').value,
        aspect_ratio: $('#prjAspect').value,
        style_preset: $('#prjStyle').value,
        budget_total: parseFloat($('#prjBudget').value) || 0,
      };
      // 无项目时先新建，拿到真实 id 再补骨架，杜绝 /api/projects//settings
      const pid = await ensureProject(form);
      const r = await api('POST', '/api/projects/' + encodeURIComponent(pid) + '/settings', {
        name: form.name,
        aspect_ratio: form.aspect_ratio,
        style_preset: form.style_preset,
        episode_count: parseInt($('#prjEpCount').value) || 0,
        duration_target: parseInt($('#prjDuration').value) || 60,
        budget_total: form.budget_total,
      });
      toast(`已保存：补齐 ${r.created_episodes} 集骨架`, 'ok');
      await loadBase(); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };

  // === 第 2 步：剧本 ===
  const btnGenOutline = $('#btnGenOutline');
  if (btnGenOutline) btnGenOutline.onclick = async () => {
    try {
      const pid = await ensureProject();
      const r = await api('POST', '/api/script/generate-outline', {
        project_id: pid,
        synopsis: $('#genSynopsis').value || '原创短剧梗概',
        episodes: parseInt($('#genEpisodes').value) || 1,
        duration_target: parseInt($('#genDuration').value) || 60,
      });
      toast(`已生成 ${r.episodes.length} 集大纲（桩）`, 'ok');
      await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };
  const btnImportBook = $('#btnImportBook');
  if (btnImportBook) btnImportBook.onclick = async () => {
    try {
      const pid = await ensureProject();
      const r = await api('POST', '/api/script/import-book', {
        project_id: pid, text: $('#impBook').value,
      });
      if (!r.chapters) throw new Error('剧本文本为空');
      toast(`已导入并分集：${r.chapters} 集`, 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };
  const btnRewrite = $('#btnRewrite');
  if (btnRewrite) btnRewrite.onclick = async () => {
    try {
      const r = await api('POST', '/api/script/rewrite', {
        episode_id: state.episodeId, instruction: $('#rwInstruction').value || '更简洁',
      });
      toast(`已改写并保存 v${r.version.version}（桩）`, 'ok'); await refresh();
      showModal('改写结果（前后对照）', r.version.before + '\n\n--- 改写后 ---\n\n' + r.text);
    } catch (e) { toast(e.message, 'err'); }
  };
  content().querySelectorAll('.act-confirm-ver').forEach(el => el.onclick = async () => {
    try { await api('POST', '/api/script/versions/' + encodeURIComponent(el.dataset.vid) + '/confirm', {});
      toast('已确认该版本', 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  });
  content().querySelectorAll('.act-view-ver').forEach(el => el.onclick = () => {
    const v = (state.detail.scriptVersions || []).find(x => x.id === el.dataset.vid);
    showModal('剧本版本 v' + (v?.version||''), (v?.before ? '【改写前】\n'+v.before+'\n\n【改写后】\n' : '') + (v?.text||''));
  });

  // === 第 3 步：拆镜 ===
  const btnSplit = $('#btnSplitShots');
  if (btnSplit) btnSplit.onclick = async () => {
    try {
      const r = await api('POST', '/api/shots/split', {
        episode_id: state.episodeId, text: $('#splitText').value || undefined,
      });
      toast(`拆镜完成：${r.total} 个镜头，缺字段 ${r.missing_fields.length} 处`, 'ok');
      state.detail.shotMissing = { missing_fields: r.missing_fields, total_shots: r.total };
      await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };
  const btnCheckMissing = $('#btnCheckMissing');
  if (btnCheckMissing) btnCheckMissing.onclick = async () => {
    try {
      state.detail.shotMissing = await api('GET', '/api/shots/missing?episode_id=' + encodeURIComponent(state.episodeId));
      await render();
      toast(`检查完成：缺字段 ${state.detail.shotMissing.missing_fields.length} 处`, 'ok');
    } catch (e) { toast(e.message, 'err'); }
  };
  content().querySelectorAll('.act-edit-shot').forEach(el => el.onclick = () => {
    const s = state.shots.find(x => x.id === el.dataset.sid);
    if (!s) return;
    $('#modalBody').innerHTML =
      `<div class="field"><label>场景</label><input class="input" id="edLoc" value="${esc(s.location||'')}"></div>` +
      `<div class="field mt"><label>动作</label><textarea class="textarea" id="edAct">${esc(s.action||'')}</textarea></div>` +
      `<div class="field mt"><label>对白</label><textarea class="textarea" id="edDia">${esc(s.dialogue||'')}</textarea></div>` +
      `<div class="field mt"><label>时长（秒）</label><input class="input" id="edDur" type="number" value="${s.duration||5}"></div>` +
      `<div class="inline mt"><button class="btn btn-green" id="btnSaveShot">保存镜头</button></div>`;
    $('#modalTitle').textContent = '编辑镜头 ' + s.index;
    $('#modal').classList.remove('hidden');
    $('#btnSaveShot').onclick = async () => {
      try {
        const r = await api('POST', '/api/shots/' + encodeURIComponent(s.id) + '/update', {
          location: $('#edLoc').value, action: $('#edAct').value,
          dialogue: $('#edDia').value, duration: parseFloat($('#edDur').value) || 5,
        });
        $('#modal').classList.add('hidden');
        toast(r.missing.length?('已保存，仍缺：'+r.missing.join('/')):'已保存，字段完整', 'ok');
        await refresh();
      } catch (e) { toast(e.message, 'err'); }
    };
  });

  // === 第 4 步：参考资产 ===
  content().querySelectorAll('.act-bind-refs').forEach(el => el.onclick = async () => {
    try {
      const ids = Array.from(content().querySelectorAll('.ref-opt[data-sid="'+CSS.escape(el.dataset.sid)+'"]:checked')).map(x => x.value);
      const r = await api('POST', '/api/shots/' + encodeURIComponent(el.dataset.sid) + '/refs', { asset_ids: ids });
      toast(`已绑定 ${r.bound} 个参考图并锁定版本`, 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  });
  content().querySelectorAll('.act-goto-assets').forEach(el => el.onclick = () => { state.view='assetlib'; render().catch(e=>toast(e.message,'err')); });

  // 剧集行点击：选中该集（停留在当前视图）
  content().querySelectorAll('.ep-row').forEach(el => el.onclick = (ev) => {
    if (ev.target.closest('.act-enter-flow')) return;  // 按钮单独处理
    state.episodeId = el.dataset.eid;
    render().catch(e => toast(e.message, 'err'));
  });
  // 「进入流程」：带该集跳到八步制作流程的剧本步骤做精加工（显式入口，非默认跳转）
  content().querySelectorAll('.act-enter-flow').forEach(el => el.onclick = (ev) => {
    ev.stopPropagation();
    state.episodeId = el.dataset.eid;
    state.view = 'workflow'; state.wfStep = 1;
    render().catch(e => toast(e.message, 'err'));
  });

  // 剧本管理 tabs（ai / import）
  if (state.view === 'script') bindGroupTabs('ai');
  // 批量生成多集：本页原地完成，不再跳转到八步流程（职责边界见 wfScript 顶部说明）
  const btnGen = $('#btnGenEpisodes');
  if (btnGen) btnGen.onclick = async () => {
    const idea = ($('#storyIdea')?.value || '').trim();
    if (!idea) { toast('请先输入故事梗概', 'err'); return; }
    if (!$('#epCount').value || parseInt($('#epCount').value) < 1) { toast('集数至少为 1', 'err'); return; }
    try {
      const pid = await ensureProject();
      const r = await api('POST', '/api/script/generate-outline', {
        project_id: pid,
        synopsis: idea,
        episodes: parseInt($('#epCount').value) || 1,
        duration_target: parseInt($('#epDuration').value) || 60,
      });
      toast(`已在本页生成 ${r.episodes.length} 集并写入剧集目录`, 'ok');
      await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };
  const btnImportFull = $('#btnImportFull');
  if (btnImportFull) btnImportFull.onclick = async () => {
    const text = ($('#fullScript')?.value || '').trim();
    if (!text) { toast('请先粘贴整部剧本', 'err'); return; }
    try {
      const pid = await ensureProject();
      const r = await api('POST', '/api/script/import-book', { project_id: pid, text });
      toast(`已导入并分集：${r.chapters} 集`, 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };

  // 提示词
  const btnBatch = $('#btnBatchPrompt');
  if (btnBatch) btnBatch.onclick = async () => {
    try {
      const r = await api('POST', '/api/prompts/batch', { episode_id: state.episodeId, mode: 'r2v' });
      toast(`批量扩写完成：生成 ${r.generated}，拦截 ${r.blocked.length}`, 'ok');
      await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };
  const btnClear = $('#btnClearPrompts');
  if (btnClear) btnClear.onclick = async () => {
    try {
      const r = await api('POST', '/api/prompts/clear-unapproved?episode_id=' + encodeURIComponent(state.episodeId), {});
      toast(`已清理 ${r.removed} 个未批准版本`, 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };

  content().querySelectorAll('.act-expand-one').forEach(el => el.onclick = async () => {
    try {
      const r = await api('POST', '/api/prompts/expand', { shot_id: el.dataset.sid, mode: 'r2v' });
      toast('扩写完成（合规通过）', 'ok'); await refresh();
      showModal('提示词已生成', r.expanded);
    } catch (e) { toast(e.message, 'err'); }
  });
  content().querySelectorAll('.act-prompt-view').forEach(el => el.onclick = () => {
    const p = state.prompts.find(x => x.id === el.dataset.pid);
    showModal('提示词版本 v' + (p?.version||''), p?.content || '');
  });
  content().querySelectorAll('.act-approve-prompt').forEach(el => el.onclick = async () => {
    try { await api('POST', '/api/prompts/approve', { prompt_id: el.dataset.pid, score: 4.5 });
      toast('提示词已批准并评分', 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  });
  content().querySelectorAll('.act-score').forEach(el => el.onclick = async () => {
    try { await api('POST', '/api/prompts/approve', { prompt_id: el.dataset.pid, score: 4.0 });
      toast('评分已更新', 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  });

  // 提交生成
  const btnSubmitBatch = $('#btnSubmitBatch');
  if (btnSubmitBatch) btnSubmitBatch.onclick = async () => {
    try {
      const r = await api('POST', '/api/submit/batch', {
        episode_id: state.episodeId, project_id: state.projectId,
        mode: $('#subMode').value, steps: parseInt($('#subSteps').value)||20,
        resolution: $('#subRes').value, estimate_cents: parseInt($('#subEst').value)||50,
        chain_lock: $('#optChain').checked, connect_review: $('#optReview').checked,
      });
      toast(`提交完成：成功 ${r.succeeded}/${r.total}`, 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };
  const btnStop = $('#btnStopAll');
  if (btnStop) btnStop.onclick = async () => {
    try {
      const r = await api('POST', '/api/tasks/cancel', { episode_id: state.episodeId });
      toast(`已取消 ${r.cancelled} 个未完成任务`, 'ok'); await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };

  // === 第 7 步：监控挑片 ===
  content().querySelectorAll('.act-gen-cand').forEach(el => el.onclick = async () => {
    try {
      const r = await api('POST', '/api/candidates/generate', { shot_id: el.dataset.sid, count: 3 });
      toast(`已生成 ${r.total} 个候选`, 'ok');
      state.detail.candidates = await api('GET', '/api/candidates?episode_id=' + encodeURIComponent(state.episodeId));
      await render();
    } catch (e) { toast(e.message, 'err'); }
  });
  content().querySelectorAll('.act-adopt').forEach(el => el.onclick = async () => {
    try { await api('POST', '/api/outputs/' + encodeURIComponent(el.dataset.oid) + '/adopt', {});
      toast('已设为采用版', 'ok');
      state.detail.candidates = await api('GET', '/api/candidates?episode_id=' + encodeURIComponent(state.episodeId));
      await render();
    } catch (e) { toast(e.message, 'err'); }
  });
  content().querySelectorAll('.act-retry').forEach(el => el.onclick = async () => {
    try {
      const r = await api('POST', '/api/tasks/' + encodeURIComponent(el.dataset.tid) + '/retry', {});
      toast('重试完成（桩）', 'ok');
      state.detail.failures = await api('GET', '/api/tasks/failures?episode_id=' + encodeURIComponent(state.episodeId));
      await render();
    } catch (e) { toast(e.message, 'err'); }
  });

  // === 第 8 步：精修成片 ===
  const btnFinPlan = $('#btnFinPlan');
  if (btnFinPlan) btnFinPlan.onclick = async () => {
    try {
      const qs = '/api/finalize/plan?project_id=' + encodeURIComponent(state.projectId) +
        '&episode_id=' + encodeURIComponent(state.episodeId) +
        '&backend=' + encodeURIComponent($('#finBackend').value) +
        '&cta=' + encodeURIComponent($('#finCta').value || '关注我们 · 下集见');
      state.detail.finalize = await api('GET', qs);
      await render();
      toast(`装配计划已生成：${state.detail.finalize.adopted_clips} 个采用片段`, 'ok');
    } catch (e) { toast(e.message, 'err'); }
  };
  const btnFinExport = $('#btnFinExport');
  if (btnFinExport) btnFinExport.onclick = async () => {
    try {
      await api('POST', '/api/finalize/export', {
        project_id: state.projectId, episode_id: state.episodeId,
        name: $('#finName').value || '精修成片', version: $('#finVer').value || 'v1',
      });
      toast('成片记录已导出（真实转码后置）', 'ok');
    } catch (e) { toast(e.message, 'err'); }
  };

  // 记录导出
  const btnExport = $('#btnExportCsv');
  if (btnExport) btnExport.onclick = async () => {
    try {
      const r = await api('GET', '/api/records/export?project_id=' + encodeURIComponent(state.projectId));
      const blob = new Blob(['\ufeff'+r.csv], { type: 'text/csv;charset=utf-8' });
      const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
      a.download = r.filename; a.click(); URL.revokeObjectURL(a.href);
      toast('CSV 已导出', 'ok');
    } catch (e) { toast(e.message, 'err'); }
  };
  const btnRefreshRecords = $('#btnRefreshRecords');
  if (btnRefreshRecords) btnRefreshRecords.onclick = () => refresh();

  // 资产库搜索
  const assetSearch = $('#assetSearch');
  if (assetSearch) assetSearch.oninput = () => {
    const kw = assetSearch.value.trim();
    content().querySelectorAll('.shot-card').forEach(c => {
      const t = (c.querySelector('.shot-card-title')?.textContent||'') + (c.querySelector('.shot-card-meta')?.textContent||'');
      c.style.display = (!kw || t.includes(kw)) ? '' : 'none';
    });
  };

  // 合成
  const btnComposeCreate = $('#btnComposeCreate');
  if (btnComposeCreate) btnComposeCreate.onclick = async () => {
    try {
      await api('POST', '/api/compose/records', { project_id: state.projectId,
        name: '示例短剧·江边 合成', version: $('#composeVer').value||'v2',
        episode_range: $('#composeRange').value, duration: 135, size_mb: 44.2 });
      toast('合成记录已创建（真实转码后置）', 'ok'); await render();
    } catch (e) { toast(e.message, 'err'); }
  };
  content().querySelectorAll('.act-compose-open').forEach(el => el.onclick = () => showModal('合成产物', '输出引用：'+el.dataset.url+'\n（桩数据，无真实文件）'));
  content().querySelectorAll('.act-compose-del').forEach(el => el.onclick = async () => {
    try { await api('DELETE', '/api/compose/records/'+encodeURIComponent(el.dataset.id), undefined);
      toast('已删除合成记录', 'ok'); await render();
    } catch (e) { toast(e.message, 'err'); }
  });

  // 视频配置
  const btnSaveCfg = $('#btnSaveCfg');
  if (btnSaveCfg) btnSaveCfg.onclick = async () => {
    const params = {};
    content().querySelectorAll('[data-cfg]').forEach(el => {
      params[el.dataset.cfg] = el.type === 'checkbox' ? el.checked :
        (isNaN(Number(el.value)) || el.value === '' ? el.value : Number(el.value));
    });
    try { await api('POST', '/api/video-config', { params });
      toast('配置已保存到本地数据目录', 'ok'); await render();
    } catch (e) { toast(e.message, 'err'); }
  };

  // 编辑专家
  content().querySelectorAll('.expert-item').forEach(el => el.onclick = () => {
    state.detail.selectedExpert = state.detail.experts.find(e => e.id === el.dataset.eid);
    render().catch(e => toast(e.message, 'err'));
  });
  const btnSaveExpert = $('#btnSaveExpert');
  if (btnSaveExpert) btnSaveExpert.onclick = async () => {
    const sel = state.detail.selectedExpert;
    try { await api('POST', '/api/experts', { id: sel.id, name: $('#expName').value,
      category: $('#expCat').value, description: $('#expDesc').value,
      system_prompt: $('#expPrompt').value });
      toast('专家已保存', 'ok');
      state.detail.experts = (await api('GET', '/api/experts')).experts;
      await render();
    } catch (e) { toast(e.message, 'err'); }
  };
  const btnCopyExpert = $('#btnCopyExpert');
  if (btnCopyExpert) btnCopyExpert.onclick = async () => {
    const sel = state.detail.selectedExpert;
    try { await api('POST', '/api/experts', { name: sel.name+'（自定义）', category: sel.category,
      description: sel.description, system_prompt: sel.system_prompt });
      state.detail.experts = (await api('GET', '/api/experts')).experts;
      toast('已另存为自定义副本', 'ok'); await render();
    } catch (e) { toast(e.message, 'err'); }
  };

  // 成本校验
  const btnCost = $('#btnCostCheck');
  if (btnCost) btnCost.onclick = async () => {
    try {
      const r = await api('POST', '/api/cost/check', { project_id: state.projectId,
        estimate_cents: parseInt($('#costEst').value) || 0 });
      $('#costResult').innerHTML = r.allowed
        ? badge('放行：' + (r.detail || '在预算内'), 'green') : badge('拦截：' + r.detail, 'red');
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

// 通用 tab 组：点击某个 .tab[data-tab=k] 激活它，并只显示 #tab-k，隐藏同组其他 #tab-*。
// 页面上同一时刻只有一个 tab 组，因此统一按 data-tab 处理即可。
function bindGroupTabs(defaultKey) {
  const tabs = Array.from(content().querySelectorAll('.tab[data-tab]'));
  if (!tabs.length) return;
  // 本组的 key 集合；只操作属于本组的面板，避免影响其他视图的 tab
  const keys = tabs.map(t => t.dataset.tab);
  const activeEl = tabs.find(t => t.classList.contains('active'));
  const initial = activeEl ? activeEl.dataset.tab : (keys.includes(defaultKey) ? defaultKey : keys[0]);
  const activate = (key) => {
    tabs.forEach(x => x.classList.toggle('active', x.dataset.tab === key));
    keys.forEach(k => {
      const panel = document.getElementById('tab-' + k);
      if (panel) panel.classList.toggle('hidden', k !== key);
    });
  };
  activate(initial);
  tabs.forEach(t => t.onclick = () => activate(t.dataset.tab));
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
      await loadBase(); state.episodeId = ''; await refresh();
    } catch (e) { toast(e.message, 'err'); }
  };
  $('#projectSelect').onchange = (e) => { state.projectId = e.target.value; state.episodeId=''; refresh().catch(err => toast(err.message, 'err')); };
  $('#modalClose').onclick = () => $('#modal').classList.add('hidden');
  $('#modal').onclick = (e) => { if (e.target === $('#modal')) $('#modal').classList.add('hidden'); };

  // 支持 URL hash 深链：
  //   #workflow/step=6  打开八步流程指定步骤
  //   #compute / #view=compute  直接打开某视图（任意 VIEWS key）
  const hash = (location.hash || '').replace(/^#/, '');
  const m = hash.match(/^workflow\/step=(\d+)$/);
  if (m) { state.view = 'workflow'; state.wfStep = Math.max(0, Math.min(7, parseInt(m[1]))); }
  else {
    const key = hash.startsWith('view=') ? hash.slice(5) : hash;
    if (key && VIEWS[key]) state.view = key;
  }

  try {
    await loadBase();
    if (state.projects.length) await loadViewData();
    await render();
  } catch (e) {
    content().innerHTML = empty('⚠', '无法连接后端服务：' + e.message + '。请先运行 python start.py');
  }
}

init();
