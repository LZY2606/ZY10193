const state = { curves: [], samples: [], priors: [], selectedSample: null, selectedBranch: null };
const $ = (id) => document.getElementById(id);
const fmt = (value, digits = 4) => Number(value).toLocaleString('zh-CN', { maximumFractionDigits: digits });
const pct = (value) => `${(value * 100).toFixed(2)}%`;
async function api(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return response.json();
}
function setStatus(message, ok = true) { $('status').textContent = message; $('status').style.color = ok ? '#166344' : '#b42318'; }
async function loadBase() {
  const [curves, priors, samples] = await Promise.all([api('/api/curves'), api('/api/priors'), api('/api/samples')]);
  state.curves = curves; state.priors = priors.priors; state.samples = samples;
  $('curve').innerHTML = curves.map(c => `<option value="${c.curve_id}">${c.version} · ${c.knot_count} knots · ${c.fingerprint_sha256.slice(0,10)}</option>`).join('');
  $('prior').innerHTML = state.priors.map(p => `<option value="${p.key}">${p.label}</option>`).join('');
  renderCurveSummary(); renderSamples();
}
function renderCurveSummary() {
  const curve = state.curves.find(c => c.curve_id === $('curve').value);
  if (!curve) return;
  $('curveSummary').innerHTML = `<span class="pill">${curve.version}</span><span class="pill">${curve.support_start_bp}-${curve.support_stop_bp} cal BP</span><span class="pill">固定 ${curve.grid_step_years} 年/格</span><br>${curve.summary}`;
}
function renderSamples() {
  $('samples').innerHTML = state.samples.map(s => `<tr><td><strong>${s.code}</strong></td><td>${fmt(s.age_bp,1)} ± ${fmt(s.sigma_bp,1)} BP</td><td>${s.material}<br>库校正 ${fmt(s.reservoir_correction,1)}</td><td><button class="secondary" data-sample="${s.sample_id}">选择</button></td></tr>`).join('') || '<tr><td colspan="4">暂无样本</td></tr>';
  document.querySelectorAll('[data-sample]').forEach(button => button.addEventListener('click', () => selectSample(Number(button.dataset.sample))));
}
async function selectSample(sampleId) {
  state.selectedSample = sampleId;
  setStatus(`已选择样本 #${sampleId}，请选择曲线版本、先验和方向。`);
  const data = await api(`/api/branches?sample_id=${sampleId}`);
  renderBranches(data.branches);
}
function renderBranches(branches) {
  if (!branches.length) { $('branches').textContent = '尚无分支；创建后旧版本参数会保留。'; return; }
  $('branches').innerHTML = branches.map(b => `<div class="branchline"><span><span class="pill">${b.curve.version}</span>${b.prior_key}<br><small>${b.direction.label}</small></span><button class="secondary" data-branch="${b.branch_id}">查看</button></div>`).join('');
  document.querySelectorAll('[data-branch]').forEach(button => button.addEventListener('click', () => showBranch(Number(button.dataset.branch))));
}
async function createBranch() {
  if (!state.selectedSample) return setStatus('请先选择一个样本。', false);
  const coverages = $('coverages').value.split(',').map(x => Number(x.trim())).filter(Boolean);
  const body = { curve_id: $('curve').value, prior_key: $('prior').value, direction: $('direction').value, coverages };
  try {
    const data = await api(`/api/samples/${state.selectedSample}/branches`, { method: 'POST', body: JSON.stringify(body) });
    setStatus(data.replayed ? '相同参数分支已存在：已重放旧结果。' : '已创建新参数分支；未与其他曲线版本合并。');
    await selectSample(state.selectedSample);
    renderBranch(data.branch);
  } catch (error) { setStatus(error.message, false); }
}
async function showBranch(id) { renderBranch(await api(`/api/branches/${id}`)); }
function displayValue(bp, direction) { return direction === 'FORWARD_YOUNGER_POSITIVE' ? 1950 - bp : bp; }
function directionLabel(direction) { return direction === 'FORWARD_YOUNGER_POSITIVE' ? '公元纪年（越右越年轻；负=BCE，正=CE）' : 'cal BP（越右越老，向过去为正）'; }
function renderBranch(branch) {
  state.selectedBranch = branch;
  const r = branch.result;
  $('branchTitle').textContent = `样本 #${branch.sample_id} · 分支 #${branch.branch_id}`;
  $('branchMeta').innerHTML = `<span class="pill">${branch.curve.version}</span><span class="pill">${branch.prior_key}</span><span class="pill warnpill">${branch.direction.label}</span><span class="pill">指纹 ${branch.fingerprint_sha256.slice(0,12)}</span>`;
  drawChart(branch);
  let rows = '';
  for (const region of r.highest_density_regions) {
    region.intervals.forEach((interval, index) => {
      rows += `<tr><td>${index === 0 ? `请求 ${pct(region.requested_probability)}<br>实含 ${pct(region.actual_probability)}` : ''}</td><td>${fmt(interval.start_display,1)}</td><td>${fmt(interval.stop_display,1)}</td><td><strong>${pct(interval.mass)}</strong></td><td>${fmt(interval.peak_center_bp,1)}</td><td>${interval.cell_count}</td><td>${interval.touches_grid_start || interval.touches_grid_stop ? '是' : '否'}</td></tr>`;
    });
  }
  $('regions').innerHTML = rows;
  const t = r.truncation;
  $('diagnostics').innerHTML = `
    <span class="pill">固定网格 ${t.grid_start_bp}-${t.grid_stop_bp} BP，步长 ${t.fixed_grid_step_years} 年</span>
    <span class="pill">离散格数 ${t.grid_stop_bp / t.fixed_grid_step_years}</span>
    <span class="pill">边缘整格质量 ${pct(t.edge_cell_mass)}</span>
    <span class="pill">曲线外诊断尾质量 ${t.analytic_outside_curve_tail_mass.toExponential(3)}</span>
    <span class="pill">中点/线性积分诊断 ${t.midpoint_vs_linear_grid_residual.toExponential(3)}</span>
    <span class="pill ${t.truncated_at_grid_edge ? 'warnpill' : ''}">触边截断：${t.truncated_at_grid_edge ? '是' : '否'}</span>
    <p>跨目标概率的最后一整格保留，不切开格子；密度截止值在每个 HPD 组内单独报告。归一化总和恒为 1，显示缩放不改变质量。</p>`;
  const sources = (r.prior_profile.sources || []).map(s => `<li><strong>${s.name}</strong>：${s.detail}</li>`).join('');
  const layer = r.prior_profile.selected_layer ? `<p>选中材料层：${r.prior_profile.selected_layer.label}（${r.prior_profile.selected_layer.start}-${r.prior_profile.selected_layer.stop} cal BP）</p>` : `<p>先验在全部固定支撑域恒定。</p>`;
  $('priorSources').innerHTML = `${layer}<ul>${sources}</ul>`;
}
function filledPathFrom(values, centers, direction, maxDensity) {
  const left = displayValue(12000, direction), right = displayValue(0, direction);
  const x = (bp) => 62 + (displayValue(bp, direction) - left) / (right - left) * 890;
  const y = (v) => 285 - v / maxDensity * 235;
  const step = Math.max(1, Math.floor(values.length / 700));
  let d = `M ${x(centers[0])} ${y(values[0])}`;
  for (let i = step; i < values.length; i += step) d += ` L ${x(centers[i])} ${y(values[i])}`;
  d += ` L ${x(centers[values.length - 1])} ${y(0)} L ${x(centers[0])} ${y(0)} Z`;
  return d;
}
function lineFrom(values, centers, direction, scale) {
  const left = displayValue(12000, direction), right = displayValue(0, direction);
  const x = (bp) => 62 + (displayValue(bp, direction) - left) / (right - left) * 890;
  const y = (v) => 285 - v * 235 / scale;
  const step = Math.max(1, Math.floor(values.length / 700));
  let d = `M ${x(centers[0])} ${y(values[0])}`;
  for (let i = step; i < values.length; i += step) d += ` L ${x(centers[i])} ${y(values[i])}`;
  return d;
}
function drawChart(branch) {
  const r = branch.result, direction = branch.direction.key;
  const centers = r.centers_bp, posterior = r.posterior_density, prior = r.prior_density;
  const maxDensity = Math.max(...posterior) * 1.08;
  const left = displayValue(12000, direction), right = displayValue(0, direction);
  const x = (bp) => 62 + (displayValue(bp, direction) - left) / (right - left) * 890;
  let rects = '';
  r.highest_density_regions.forEach((region, ri) => region.intervals.forEach(interval => {
    const x1 = x(interval.stop_bp), x2 = x(interval.start_bp);
    rects += `<rect x="${Math.min(x1,x2)}" y="45" width="${Math.abs(x2-x1)}" height="240" fill="#f4a261" opacity="${ri === 0 ? 0.42 : 0.22}"></rect>`;
  }));
  const ticks = [0,2000,4000,6000,8000,10000,12000].map(bp => `<line x1="${x(bp)}" x2="${x(bp)}" y1="285" y2="291" stroke="#333"/><text x="${x(bp)}" y="310" text-anchor="middle" font-size="11">${fmt(displayValue(bp,direction),0)}</text>`).join('');
  $('chart').innerHTML = `
    <rect x="62" y="45" width="890" height="240" fill="white" stroke="#ccd6cf"></rect>${rects}
    <path d="${filledPathFrom(posterior, centers, direction, maxDensity)}" fill="#111" opacity=".88"></path>
    <path d="${lineFrom(prior, centers, direction, maxDensity)}" fill="none" stroke="#1769aa" stroke-width="2"></path>
    <path d="${lineFrom(r.cdf_on_edges, r.edges_bp, direction, 1)}" fill="none" stroke="#2b8a3e" stroke-width="2" stroke-dasharray="5 4"></path>
    <line x1="62" x2="952" y1="285" y2="285" stroke="#333"></line><line x1="62" x2="62" y1="45" y2="285" stroke="#333"></line>${ticks}
    <text x="507" y="327" text-anchor="middle" font-size="12">${directionLabel(direction)} → 正方向</text>
    <text x="20" y="165" transform="rotate(-90 20 165)" text-anchor="middle" font-size="12">密度</text>
    <text x="983" y="165" transform="rotate(90 983 165)" text-anchor="middle" font-size="12" fill="#2b8a3e">CDF</text>`;
}
async function loadRuns() {
  const runs = await api('/api/runs?limit=100');
  $('runs').innerHTML = runs.map(run => `<tr><td>${run.run_id}</td><td>${run.action}</td><td>${run.status}</td><td>${run.created_at}</td></tr>`).join('') || '<tr><td colspan="4">暂无</td></tr>';
}
$('fixtureBtn').onclick = async () => { try { await api('/api/import/fixture', { method: 'POST' }); setStatus('固定 fixture 已导入/复核。'); await loadBase(); } catch (e) { setStatus(e.message, false); } };
$('importBtn').onclick = async () => {
  const text = $('importText').value.trim();
  try {
    let payload;
    if (text.startsWith('[')) payload = { records: JSON.parse(text), source: 'manual-json' };
    else payload = { csv_text: text, source: 'manual-csv' };
    const result = await api('/api/import', { method: 'POST', body: JSON.stringify(payload) });
    setStatus(`导入完成：新增 ${result.inserted.length}，替换 ${result.replaced.length}`);
    await loadBase();
  } catch (e) { setStatus(e.message, false); }
};
$('curve').onchange = renderCurveSummary;
$('branchBtn').onclick = createBranch;
$('refreshRuns').onclick = loadRuns;
$('exportRuns').onclick = () => window.location = '/api/runs/export';
$('clearBtn').onclick = async () => {
  if (!confirm('确认清空样本、分支和运行记录，并重新播种固定曲线？')) return;
  await api('/api/admin/clear', { method: 'POST' });
  state.selectedSample = null; state.selectedBranch = null; await loadBase(); loadRuns();
  setStatus('数据库已清空，固定曲线已重新播种；可重新导入 fixture 复核。');
};
loadBase().catch(e => setStatus(e.message, false));
loadRuns();
