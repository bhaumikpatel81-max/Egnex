'use strict';
// ============================================================
// Egnex SPA — app.js
// Hash-based routing: #/login  #/dashboard  #/requisitions
// #/requisitions/new  #/requisitions/:id  #/candidates
// #/interviews  #/reports
// ============================================================

// ── State ────────────────────────────────────────────────────
const S = {
  token: localStorage.getItem('eg_token') || null,
  user:  (() => { try { return JSON.parse(localStorage.getItem('eg_user')); } catch(e) { return null; } })(),
};

// ── API ──────────────────────────────────────────────────────
async function apiFetch(method, path, body) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json',
                ...(S.token ? { 'Authorization': `Bearer ${S.token}` } : {}) },
  };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (res.status === 401) { doLogout(false); return null; }
  if (!res.ok) {
    const e = await res.json().catch(() => ({}));
    throw new Error(e.detail || e.error || res.statusText);
  }
  return res.json();
}
const GET  = p       => apiFetch('GET', p);
const POST = (p, b)  => apiFetch('POST', p, b);

// ── Router ───────────────────────────────────────────────────
function nav(hash) { window.location.hash = hash; }

function route() {
  if (!S.token) { mountLogin(); return; }
  const h = window.location.hash || '#/';
  if (h === '#/' || h === '') { nav('#/dashboard'); return; }
  const m = h.match(/^#\/requisitions\/([^/]+)$/);
  if (m && m[1] !== 'new') { mountKanban(m[1]); return; }
  switch(h) {
    case '#/dashboard':       mountDashboard();     break;
    case '#/requisitions':    mountRequisitions();  break;
    case '#/requisitions/new':mountNewRequisition();break;
    case '#/candidates':      mountCandidates();    break;
    case '#/interviews':      mountInterviews();    break;
    case '#/reports':         mountReports();       break;
    default: mountDashboard();
  }
}

window.addEventListener('hashchange', route);
window.addEventListener('load', route);

// ── Helpers ──────────────────────────────────────────────────
const R = () => document.getElementById('root');
const $  = (sel, ctx=document) => ctx.querySelector(sel);
const $$ = (sel, ctx=document) => [...ctx.querySelectorAll(sel)];
function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function initials(n) { return (n||'?').split(' ').slice(0,2).map(w=>w[0]).join('').toUpperCase(); }
function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-IN', {day:'numeric',month:'short',year:'numeric'});
}
function fmtScore(n) { return n != null ? Math.round(n) : '—'; }

function statusBadge(s) {
  const m = { open:'badge-green', draft:'badge-grey', on_hold:'badge-yellow', closed:'badge-grey',
    cancelled:'badge-red', applied:'badge-blue', screening:'badge-blue', screen_passed:'badge-green',
    screen_rejected:'badge-red', interviewing:'badge-orange', selected:'badge-green',
    rejected:'badge-red', offer_stage:'badge-orange', offered:'badge-orange',
    joined:'badge-green', dropped:'badge-grey' };
  return `<span class="badge ${m[s]||'badge-grey'}">${esc(s?.replace(/_/g,' '))}</span>`;
}

// ── SVG icons ────────────────────────────────────────────────
const ic = {
  grid:      `<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1" y="1" width="6" height="6" rx="1"/><rect x="9" y="1" width="6" height="6" rx="1"/><rect x="1" y="9" width="6" height="6" rx="1"/><rect x="9" y="9" width="6" height="6" rx="1"/></svg>`,
  brief:     `<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="5" width="12" height="9" rx="1"/><path d="M5 5V3.5A1.5 1.5 0 016.5 2h3A1.5 1.5 0 0111 3.5V5"/><line x1="2" y1="9" x2="14" y2="9"/></svg>`,
  users:     `<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="6" cy="5" r="2.5"/><path d="M1 14c0-3 2-4.5 5-4.5s5 1.5 5 4.5"/><path d="M11 7c1.5 0 3 1 3 3.5"/><path d="M12.5 2.5a2 2 0 010 4"/></svg>`,
  cal:       `<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1" y="3" width="14" height="12" rx="1"/><line x1="1" y1="7" x2="15" y2="7"/><line x1="5" y1="1" x2="5" y2="5"/><line x1="11" y1="1" x2="11" y2="5"/></svg>`,
  chart:     `<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><polyline points="2,12 5,8 8,10 11,5 14,7"/><rect x="1" y="1" width="14" height="14" rx="1"/></svg>`,
  person:    `<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="5" r="3"/><path d="M2 15c0-4 2-5.5 6-5.5s6 1.5 6 5.5"/></svg>`,
  plus:      `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2"><line x1="7" y1="2" x2="7" y2="12"/><line x1="2" y1="7" x2="12" y2="7"/></svg>`,
  trash:     `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 4h10M5 4V2.5h4V4M4 4l.5 7.5h5L10 4"/></svg>`,
  arrow:     `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 7h12M8 2l5 5-5 5"/></svg>`,
};

// ── App shell ─────────────────────────────────────────────────
function shell(activeHash, screenTitle, contentHtml) {
  const u = S.user || {};
  const isAdmin = u.role === 'ta_manager' || u.role === 'admin';
  const navs = [
    ['#/dashboard','Dashboard',ic.grid],
    ['#/requisitions','Requisitions',ic.brief],
    ['#/candidates','Candidates',ic.users],
    ['#/interviews','Interviews',ic.cal],
    ['#/reports','Reports',ic.chart],
    ...(isAdmin ? [['#/team','Team',ic.person]] : []),
  ];
  const navHtml = navs.map(([h,label,icon]) =>
    `<a class="nav-item${activeHash===h?' active':''}" href="${esc(h)}">${icon}<span>${esc(label)}</span></a>`
  ).join('');
  R().innerHTML = `
    <div class="app-layout">
      <nav class="sidebar">
        <div class="sidebar-brand">
          <img src="/assets/egnex-logo.png" alt="Egnex" class="sidebar-logo">
        </div>
        <div class="sidebar-nav">${navHtml}</div>
      </nav>
      <div class="main-area">
        <header class="topbar">
          <div class="topbar-left">
            <span class="topbar-screen">${esc(screenTitle)}</span>
            <span class="topbar-fy">FY25-26</span>
          </div>
          <div class="topbar-right">
            <span class="gcal-status" id="gcal-top"></span>
            <div class="avatar" title="${esc(u.full_name)}" onclick="openUserMenu(this)">${initials(u.full_name)}</div>
          </div>
        </header>
        <main class="page-content" id="sc">${contentHtml}</main>
      </div>
    </div>`;
  // async: calendar status
  if (S.user?.user_id) {
    GET(`/api/google/status?recruiter_id=${S.user.user_id}`).then(r => {
      const el = document.getElementById('gcal-top');
      if (!el) return;
      if (r?.linked) {
        el.innerHTML = `<span class="gcal-dot"></span> ${esc(r.google_email||'Calendar linked')}`;
        el.className = 'gcal-status linked';
      } else {
        el.innerHTML = `<span class="gcal-dot"></span> Calendar not linked`;
        el.className = 'gcal-status';
      }
    }).catch(()=>{});
  }
}

function setContent(html) { const el=document.getElementById('sc'); if(el) el.innerHTML=html; }

// ── User menu ────────────────────────────────────────────────
window.openUserMenu = function(avatar) {
  const old = document.getElementById('umenu');
  if (old) { old.remove(); return; }
  const rect = avatar.getBoundingClientRect();
  const u = S.user || {};
  const m = document.createElement('div');
  m.id = 'umenu'; m.className = 'user-menu';
  m.style.cssText = `top:${rect.bottom+4}px;right:${window.innerWidth-rect.right}px`;
  m.innerHTML = `
    <div class="user-menu-info">
      <div class="name">${esc(u.full_name)}</div>
      <div class="email">${esc(u.email)}</div>
      <div class="role-tag">${esc(u.role)}</div>
    </div>
    <button class="user-menu-item danger" onclick="doLogout()">Sign out</button>`;
  document.body.appendChild(m);
  setTimeout(() => document.addEventListener('click', function h(e){
    if(!m.contains(e.target)){m.remove();document.removeEventListener('click',h);}
  }), 0);
};

// ── Auth ─────────────────────────────────────────────────────
function doLogout(redirect=true) {
  if (S.token) POST('/api/auth/logout').catch(()=>{});
  S.token = null; S.user = null;
  localStorage.removeItem('eg_token');
  localStorage.removeItem('eg_user');
  if (redirect) nav('#/login');
}

// ============================================================
// SCREEN: Login
// ============================================================
function mountLogin() {
  R().innerHTML = `
    <div class="login-page">
      <div class="login-card">
        <div class="login-header">
          <img src="/assets/egnex-logo.png" alt="Egnex" class="login-logo">
          <span class="login-tagline">One click hire</span>
        </div>
        <div class="login-body">
          <h2 class="login-title">Sign in</h2>
          <div id="lerr" class="alert-error" style="display:none"></div>
          <div class="form-group">
            <label class="form-label">Work email</label>
            <input id="lemail" type="email" class="form-input" placeholder="you@amnex.com" autocomplete="email">
          </div>
          <div class="form-group">
            <label class="form-label">Password</label>
            <input id="lpwd" type="password" class="form-input" placeholder="••••••••" autocomplete="current-password">
          </div>
          <button id="lbtn" class="btn btn-primary btn-full" onclick="doLogin()">Sign in</button>
          <p class="form-helper">Your role is detected automatically after sign in.</p>
        </div>
      </div>
    </div>`;
  $('#lpwd').addEventListener('keydown', e => { if(e.key==='Enter') doLogin(); });
  $('#lemail').addEventListener('keydown', e => { if(e.key==='Enter') $('#lpwd').focus(); });
}

window.doLogin = async function() {
  const email = $('#lemail').value.trim();
  const pw    = $('#lpwd').value;
  const errEl = $('#lerr');
  const btn   = $('#lbtn');
  if (!email || !pw) { errEl.textContent='Enter your email and password.'; errEl.style.display='block'; return; }
  btn.textContent='Signing in…'; btn.disabled=true; errEl.style.display='none';
  try {
    const d = await POST('/api/auth/login', {email, password: pw});
    S.token = d.token;
    S.user  = {user_id:d.user_id, full_name:d.full_name, email:d.email, role:d.role};
    localStorage.setItem('eg_token', S.token);
    localStorage.setItem('eg_user', JSON.stringify(S.user));
    nav('#/dashboard');
  } catch(err) {
    errEl.textContent = err.message || 'Invalid email or password.';
    errEl.style.display='block';
    btn.textContent='Sign in'; btn.disabled=false;
  }
};

// ============================================================
// SCREEN: Dashboard
// ============================================================
async function mountDashboard() {
  shell('#/dashboard','Dashboard','<div class="loader">Loading pipeline…</div>');
  try {
    const [stats, reqs] = await Promise.all([
      GET('/api/dashboard/stats'),
      GET('/api/requisitions'),
    ]);
    let gender = null; try { gender = await GET('/api/reports/gender'); } catch(e){}
    const isAdmin = ['ta_manager','admin'].includes(S.user?.role);
    let load = null;
    if (isAdmin) try { load = await GET('/api/reports/recruiter-load'); } catch(e){}

    const stages = [
      {k:'open_reqs',        label:'Open requisitions'},
      {k:'applications',     label:'Applications received'},
      {k:'under_screening',  label:'Under screening'},
      {k:'screening_cleared',label:'Screening cleared'},
      {k:'ai_interview',     label:'AI interview'},
      {k:'panel_interview',  label:'Panel interview'},
      {k:'selected',         label:'Selected'},
      {k:'offer_stage',      label:'Offer stage'},
      {k:'joined',           label:'Joined', cls:'joined'},
    ];
    const pipeHtml = stages.map(s =>
      `<div class="stage-card${s.cls?' '+s.cls:''}">
        <div class="stage-label">${esc(s.label)}</div>
        <div class="stage-count">${stats[s.k]??0}</div>
      </div>`
    ).join('');
    const tatHtml = `
      <div class="tat-card">
        <div class="stage-label">Avg time to hire</div>
        <div class="tat-count">${stats.avg_days_to_hire!=null?stats.avg_days_to_hire+'d':'—'}</div>
        <div class="tat-label">Target: 3–4 days</div>
      </div>`;

    const topReqs = (isAdmin ? reqs : reqs.filter(r=>r.status==='open')).slice(0,10);
    const reqsHtml = topReqs.length===0
      ? `<div class="empty-state"><p class="empty-title">No requisitions yet</p></div>`
      : `<div class="table-wrap"><table>
          <thead><tr><th>Role</th><th>Business unit</th><th>Band</th><th>In pipeline</th><th>Status</th></tr></thead>
          <tbody>${topReqs.map(r=>`
            <tr class="row-link" onclick="nav('#/requisitions/${esc(r.id)}')">
              <td class="fw-600">${esc(r.title)}</td>
              <td>${esc(r.business_unit)}</td>
              <td><span class="badge badge-grey">${esc(r.band)}</span></td>
              <td>${r.in_pipeline??0}</td>
              <td>${statusBadge(r.status)}</td>
            </tr>`).join('')}
          </tbody></table></div>`;

    let genderHtml = '';
    if (gender?.length) {
      const total = gender.reduce((s,g)=>s+parseInt(g.total||0),0);
      const f = gender.find(g=>g.gender==='female');
      const fPct = total>0 ? Math.round((parseInt(f?.total||0)/total)*100) : 0;
      genderHtml = `
        <div class="card mt-16">
          <div class="card-title">Diversity (gender)</div>
          <div class="diversity-bar">
            <div class="div-female" style="width:${fPct}%"></div>
            <div class="div-male"></div>
          </div>
          <div class="div-legend">
            <span class="div-dot-orange">▪ Female ${fPct}%</span>
            <span class="div-dot-blue">▪ Male ${100-fPct}%</span>
          </div>
        </div>`;
    }

    let loadHtml = '';
    if (isAdmin && load?.length) {
      loadHtml = `
        <div class="card mt-16">
          <div class="card-title">Recruiter load</div>
          <div class="table-wrap"><table>
            <thead><tr><th>Recruiter</th><th>Open reqs</th><th>Candidates</th></tr></thead>
            <tbody>${load.map(r=>`
              <tr><td>${esc(r.recruiter_name||r.recruiter||'—')}</td>
              <td>${r.open_reqs??0}</td><td>${r.total_candidates??r.applications??0}</td></tr>`).join('')}
            </tbody></table></div>
        </div>`;
    }

    setContent(`
      <div class="page-header">
        <h1 class="page-title">${isAdmin?'All pipelines':'My pipeline'}</h1>
      </div>
      <div class="pipeline-row">${pipeHtml}${tatHtml}</div>
      <div class="section-label">${isAdmin?'All requisitions':'My requisitions'}</div>
      <div class="card card-flush">${reqsHtml}</div>
      ${genderHtml}${loadHtml}`);
  } catch(err) {
    setContent(`<div class="alert-error">${esc(err.message)}</div>`);
  }
}

// ============================================================
// SCREEN: Requisitions list
// ============================================================
async function mountRequisitions() {
  shell('#/requisitions','Requisitions','<div class="loader">Loading…</div>');
  try {
    const reqs = await GET('/api/requisitions');
    let active = 'All';
    function render(filter) {
      active = filter;
      const filtered = filter==='All' ? reqs : reqs.filter(r=>{
        if(filter==='Open')    return r.status==='open';
        if(filter==='On hold') return r.status==='on_hold';
        if(filter==='Closed')  return r.status==='closed';
        return true;
      });
      const cnt = {
        Open:    reqs.filter(r=>r.status==='open').length,
        'On hold':reqs.filter(r=>r.status==='on_hold').length,
        Closed:  reqs.filter(r=>r.status==='closed').length,
      };
      const pills = ['All','Open','On hold','Closed'].map(k=>{
        const label = k==='All' ? `All (${reqs.length})` : `${k} (${cnt[k]||0})`;
        return `<button class="filter-pill${active===k?' active':''}" onclick="reqFilter('${esc(k)}')">${esc(label)}</button>`;
      }).join('');
      const rows = filtered.length===0
        ? `<div class="empty-state"><p class="empty-title">No requisitions</p>
             <p class="empty-desc">Create a new requisition to get started.</p></div>`
        : `<div class="table-wrap"><table>
            <thead><tr><th>Role</th><th>Business unit</th><th>Band</th><th>In pipeline</th><th>Roll type</th><th>Status</th></tr></thead>
            <tbody>${filtered.map(r=>`
              <tr class="row-link" onclick="nav('#/requisitions/${esc(r.id)}')">
                <td class="fw-600">${esc(r.title)}</td>
                <td>${esc(r.business_unit)}</td>
                <td><span class="badge badge-grey">${esc(r.band)}</span></td>
                <td>${r.in_pipeline??0}</td>
                <td>${esc(r.roll_type?.replace('_',' '))}</td>
                <td>${statusBadge(r.status)}</td>
              </tr>`).join('')}
            </tbody></table></div>`;
      document.getElementById('sc').innerHTML = `
        <div class="page-header">
          <h1 class="page-title">Requisitions</h1>
          <button class="btn btn-primary" onclick="nav('#/requisitions/new')">${ic.plus} New requisition</button>
        </div>
        <div class="filter-pills">${pills}</div>
        <div class="card card-flush">${rows}</div>`;
    }
    window.reqFilter = render;
    render('All');
  } catch(err) {
    setContent(`<div class="alert-error">${esc(err.message)}</div>`);
  }
}

// ============================================================
// SCREEN: New requisition
// ============================================================
async function mountNewRequisition() {
  shell('#/requisitions','New requisition','<div class="loader">Loading…</div>');
  try {
    const [bands, bus] = await Promise.all([GET('/api/bands'), GET('/api/business-units')]);
    const bandOpts = bands.filter(b=>b.is_active).map(b=>`<option value="${esc(b.id)}">${esc(b.code)} — ${esc(b.description)}</option>`).join('');
    const buOpts   = bus.map(b=>`<option value="${esc(b.id)}">${esc(b.name)} (${esc(b.company)})</option>`).join('');

    // default rounds
    const defaultRounds = [
      {seq:1,name:'AI screening interview',type:'bot_interview',auto:true},
      {seq:2,name:'Level 1 Panel',type:'panel',auto:false},
      {seq:3,name:'Level 2 Panel',type:'panel',auto:false},
      {seq:4,name:'Final Panel',type:'hr',auto:false},
    ];
    window._rounds = defaultRounds.map(r=>({...r}));

    function roundsHtml() {
      return `<div class="rounds-builder" id="rounds-builder">
        ${window._rounds.map((r,i)=>`
          <div class="round-row">
            <span class="round-seq">${r.seq}</span>
            ${r.auto ? `<input class="form-input" value="${esc(r.name)}" readonly style="opacity:.6;flex:1">
                        <span class="round-auto-tag">Auto</span>` :
                       `<input class="form-input" id="rn${i}" value="${esc(r.name)}" placeholder="Round name" style="flex:1"
                          onchange="window._rounds[${i}].name=this.value">
                        <button class="btn btn-ghost btn-icon" onclick="removeRound(${i})" title="Remove">${ic.trash}</button>`}
          </div>`).join('')}
        <div class="round-row" style="justify-content:flex-start">
          <button class="btn btn-secondary btn-sm" onclick="addRound()">${ic.plus} Add panel level</button>
        </div>
      </div>`;
    }

    setContent(`
      <div class="page-header">
        <h1 class="page-title">New requisition</h1>
        <button class="btn btn-secondary" onclick="nav('#/requisitions')">Cancel</button>
      </div>
      <div id="req-err" class="alert-error" style="display:none"></div>
      <div class="card" style="max-width:760px">
        <div class="form-row">
          <div class="form-group">
            <label class="form-label">Job title *</label>
            <input id="rq-title" class="form-input" placeholder="e.g. Backend Engineer">
          </div>
          <div class="form-group">
            <label class="form-label">Business unit *</label>
            <select id="rq-bu" class="form-select"><option value="">Select…</option>${buOpts}</select>
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label class="form-label">Band *</label>
            <select id="rq-band" class="form-select"><option value="">Select…</option>${bandOpts}</select>
          </div>
          <div class="form-group">
            <label class="form-label">Roll type</label>
            <select id="rq-roll" class="form-select">
              <option value="on_roll">On-roll (Amnex payroll)</option>
              <option value="off_roll">Off-roll (third-party)</option>
            </select>
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label class="form-label">Openings</label>
            <input id="rq-openings" type="number" min="1" value="1" class="form-input">
          </div>
          <div class="form-group">
            <label class="form-label">Min experience (years)</label>
            <input id="rq-exp" type="number" min="0" step="0.5" value="3" class="form-input">
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label class="form-label">Budgeted CTC (₹)</label>
            <input id="rq-ctc" type="number" placeholder="e.g. 1800000" class="form-input">
          </div>
          <div class="form-group">
            <label class="form-label">Fiscal year</label>
            <input id="rq-fy" class="form-input" value="FY25-26">
          </div>
        </div>
        <div class="form-group">
          <label class="form-label">Key skills (comma separated)</label>
          <input id="rq-skills" class="form-input" placeholder="Python, PostgreSQL, REST API">
        </div>
        <div class="form-group">
          <label class="form-label">Job description</label>
          <textarea id="rq-jd" class="form-textarea" rows="3" placeholder="Describe the role…"></textarea>
        </div>
        <div class="form-group mt-8">
          <label class="form-label">Panel interview levels</label>
          <p class="text-sm text-muted mt-4" style="margin-bottom:10px">
            The first round (AI screening) is always included. Add panel levels below — each requisition can have a different number.
          </p>
          <div id="rounds-wrap">${roundsHtml()}</div>
        </div>
        <div style="margin-top:20px">
          <button class="btn btn-primary" onclick="submitNewReq()">Create requisition</button>
        </div>
      </div>`);

    window.addRound = function() {
      const seq = window._rounds.length+1;
      window._rounds.push({seq, name:`Level ${seq-1} Panel`, type:'panel', auto:false});
      document.getElementById('rounds-wrap').innerHTML = roundsHtml();
    };
    window.removeRound = function(i) {
      window._rounds.splice(i,1);
      window._rounds.forEach((r,j)=>r.seq=j+1);
      document.getElementById('rounds-wrap').innerHTML = roundsHtml();
    };
  } catch(err) {
    setContent(`<div class="alert-error">${esc(err.message)}</div>`);
  }
}

window.submitNewReq = async function() {
  const errEl = document.getElementById('req-err');
  const get = id => document.getElementById(id)?.value?.trim();
  const title=get('rq-title'), bu=get('rq-bu'), band=get('rq-band');
  if (!title||!bu||!band) {
    errEl.textContent='Title, business unit and band are required.'; errEl.style.display='block'; return;
  }
  const skills = get('rq-skills').split(',').map(s=>s.trim()).filter(Boolean);
  const body = {
    title, bu_id:bu, band_id:band,
    roll_type: get('rq-roll')||'on_roll',
    openings: parseInt(document.getElementById('rq-openings')?.value)||1,
    min_experience: parseFloat(document.getElementById('rq-exp')?.value)||0,
    budgeted_ctc: parseFloat(document.getElementById('rq-ctc')?.value)||null,
    fiscal_year: get('rq-fy')||'FY25-26',
    key_skills: skills,
    job_description: get('rq-jd')||null,
    rounds: window._rounds.map(r=>({sequence:r.seq,name:r.name,round_type:r.type,is_auto:r.auto})),
  };
  try {
    errEl.style.display='none';
    const res = await POST('/api/requisitions', body);
    nav(`#/requisitions/${res.id}`);
  } catch(err) {
    errEl.textContent = err.message; errEl.style.display='block';
  }
};

// ============================================================
// SCREEN: Kanban requisition detail
// ============================================================
async function mountKanban(reqId) {
  shell('#/requisitions','Requisition detail','<div class="loader">Loading…</div>');
  try {
    const [req, kanban] = await Promise.all([
      GET(`/api/requisitions/${reqId}`),
      GET(`/api/requisitions/${reqId}/kanban`),
    ]);

    // Build columns from rounds + fixed stages
    const panelCols = (req.rounds||[]).map(r=>({key:`panel_${r.sequence}`,label:r.name,round:r.sequence,type:r.round_type}));
    const cols = [
      {key:'applied',       label:'Applications',  statuses:['applied','screening']},
      {key:'screen_passed', label:'Screening',      statuses:['screen_passed']},
      ...panelCols.map(c=>({...c, statuses:['interviewing'], roundFilter:c.round})),
      {key:'selected',      label:'Selected',       statuses:['selected']},
      {key:'offer_stage',   label:'Offer stage',    statuses:['offer_stage','offered']},
      {key:'joined',        label:'Joined',         statuses:['joined']},
    ];

    function colCandidates(col) {
      return (kanban||[]).filter(c=>{
        if(!col.statuses.includes(c.status)) return false;
        if(col.roundFilter!=null) return c.current_round===col.roundFilter;
        return true;
      });
    }

    function cardHtml(c) {
      const score = c.combined_score??c.match_score;
      return `<div class="kanban-card" draggable="true"
          data-id="${esc(c.id)}"
          data-status="${esc(c.status)}"
          ondragstart="kDragStart(event)"
          ondragend="kDragEnd(event)">
        <div class="kanban-name">${esc(c.full_name)}</div>
        <div class="kanban-meta">
          <span>${esc(c.gender||'')}</span>
          ${score!=null?`<span class="score-pill">${Math.round(score)}</span>`:''}
        </div>
      </div>`;
    }

    function colHtml(col) {
      const cards = colCandidates(col);
      return `<div class="kanban-col" data-col="${esc(col.key)}"
          data-next-status="${esc(nextStatus(col.key, cols))}"
          ondragover="kDragOver(event)"
          ondragleave="kDragLeave(event)"
          ondrop="kDrop(event,${cols.indexOf(col)})">
        <div class="kanban-col-header">
          <span>${esc(col.label)}</span>
          <span class="kanban-count">${cards.length}</span>
        </div>
        ${cards.map(cardHtml).join('')}
      </div>`;
    }

    function nextStatus(colKey, allCols) {
      const idx = allCols.findIndex(c=>c.key===colKey);
      if(idx<0||idx>=allCols.length-1) return '';
      const next = allCols[idx+1];
      return next.statuses[0]||'';
    }

    const boardHtml = `<div class="kanban-board" id="kanban">${cols.map(c=>colHtml(c)).join('')}</div>`;

    setContent(`
      <div class="page-header">
        <div>
          <h1 class="page-title">${esc(req.title)}</h1>
          <div class="flex-gap-8 mt-4">
            <span class="badge badge-grey">${esc(req.band)}</span>
            <span class="text-muted text-sm">${esc(req.business_unit)}</span>
            ${statusBadge(req.status)}
          </div>
        </div>
        <button class="btn btn-secondary" onclick="nav('#/requisitions')">← Back</button>
      </div>
      <div class="alert-info" id="kanban-msg" style="display:none"></div>
      ${boardHtml}`);

    // Drag & drop handlers
    let dragging = null;
    window.kDragStart = function(e) {
      dragging = e.currentTarget;
      dragging.classList.add('dragging');
      e.dataTransfer.effectAllowed='move';
    };
    window.kDragEnd = function(e) {
      if(dragging) { dragging.classList.remove('dragging'); dragging=null; }
      $$('.kanban-col').forEach(c=>c.classList.remove('drag-over'));
    };
    window.kDragOver = function(e) {
      e.preventDefault();
      e.dataTransfer.dropEffect='move';
      e.currentTarget.classList.add('drag-over');
    };
    window.kDragLeave = function(e) { e.currentTarget.classList.remove('drag-over'); };
    window.kDrop = async function(e, colIdx) {
      e.preventDefault();
      e.currentTarget.classList.remove('drag-over');
      if(!dragging) return;
      const appId = dragging.dataset.id;
      const toStatus = cols[colIdx]?.statuses[0];
      if(!toStatus || dragging.dataset.status===toStatus) return;
      const msgEl = document.getElementById('kanban-msg');
      try {
        await POST(`/api/applications/${appId}/advance`,
          {to_status:toStatus, actor_id:S.user?.user_id||null, note:'Moved via kanban'});
        dragging.dataset.status = toStatus;
        msgEl.textContent=`Moved to "${cols[colIdx].label}"`;
        msgEl.style.display='block';
        setTimeout(()=>{ if(msgEl) msgEl.style.display='none'; },3000);
        // re-render board
        const fresh = await GET(`/api/requisitions/${reqId}/kanban`);
        const newBoard = document.getElementById('kanban');
        if(newBoard) newBoard.outerHTML = `<div class="kanban-board" id="kanban">${cols.map(c=>{
          const cards2=(fresh||[]).filter(c2=>{
            if(!c.statuses.includes(c2.status)) return false;
            if(c.roundFilter!=null) return c2.current_round===c.roundFilter;
            return true;
          });
          return `<div class="kanban-col" data-col="${esc(c.key)}"
              ondragover="kDragOver(event)" ondragleave="kDragLeave(event)"
              ondrop="kDrop(event,${cols.indexOf(c)})">
            <div class="kanban-col-header"><span>${esc(c.label)}</span><span class="kanban-count">${cards2.length}</span></div>
            ${cards2.map(cardHtml).join('')}
          </div>`;
        }).join('')}</div>`;
      } catch(err) {
        msgEl.textContent=err.message; msgEl.style.display='block';
      }
    };
  } catch(err) {
    setContent(`<div class="alert-error">${esc(err.message)}</div>`);
  }
}

// ============================================================
// SCREEN: Candidates
// ============================================================
async function mountCandidates() {
  shell('#/candidates','Candidates','<div class="loader">Loading…</div>');
  try {
    const candidates = await GET('/api/candidates');
    if(!candidates?.length) {
      setContent(`<div class="page-header"><h1 class="page-title">Candidates</h1></div>
        <div class="card"><div class="empty-state">
          <p class="empty-title">No candidates yet</p>
          <p class="empty-desc">Candidates appear here as they apply to requisitions.</p>
        </div></div>`); return;
    }
    const rows = candidates.map(c=>`
      <tr>
        <td class="fw-600">${esc(c.full_name)}</td>
        <td>${esc(c.requisition_title||'—')}</td>
        <td>${c.combined_score!=null?Math.round(c.combined_score):(c.match_score!=null?Math.round(c.match_score):'—')}</td>
        <td>${statusBadge(c.status)}</td>
        <td>${esc(c.gender||'—')}</td>
        <td>${fmtDate(c.applied_at)}</td>
      </tr>`).join('');
    setContent(`
      <div class="page-header"><h1 class="page-title">Candidates</h1></div>
      <div class="card card-flush">
        <div class="table-wrap"><table>
          <thead><tr><th>Name</th><th>Requisition</th><th>Score</th><th>Stage</th><th>Gender</th><th>Applied</th></tr></thead>
          <tbody>${rows}</tbody>
        </table></div>
      </div>`);
  } catch(err) {
    setContent(`<div class="alert-error">${esc(err.message)}</div>`);
  }
}

// ============================================================
// SCREEN: Interviews
// ============================================================
async function mountInterviews() {
  shell('#/interviews','Interviews','<div class="loader">Loading…</div>');
  try {
    const [interviews, gcal] = await Promise.all([
      GET('/api/interviews'),
      S.user?.user_id ? GET(`/api/google/status?recruiter_id=${S.user.user_id}`) : Promise.resolve(null),
    ]);

    const calCard = `
      <div class="card" style="margin-bottom:20px">
        <div class="card-title">Google Calendar</div>
        ${gcal?.linked
          ? `<div class="gcal-status linked" style="font-size:14px;gap:8px">
               <span class="gcal-dot"></span> Connected as ${esc(gcal.google_email)}
               <button class="btn btn-secondary btn-sm" style="margin-left:auto" onclick="gcalDisconnect()">Disconnect</button>
             </div>`
          : `<p class="text-muted text-sm" style="margin-bottom:12px">
               Connect your Google Calendar to schedule interviews with automatic Meet links.
             </p>
             <button class="btn btn-primary btn-sm" onclick="gcalConnect()">Connect Google Calendar</button>`}
      </div>`;

    const listHtml = !interviews?.length
      ? `<div class="card"><div class="empty-state">
           <p class="empty-title">No interviews scheduled</p>
           <p class="empty-desc">Interviews are scheduled from the requisition kanban board.</p>
         </div></div>`
      : interviews.map(i=>`
          <div class="interview-card">
            <div class="interview-time">${fmtDate(i.scheduled_at)}</div>
            <div style="flex:1">
              <div class="fw-600">${esc(i.candidate_name||'Candidate')}</div>
              <div class="text-muted text-sm">${esc(i.requisition_title||'')} · ${esc(i.round_name||'Interview')}</div>
              ${i.meet_link?`<a href="${esc(i.meet_link)}" target="_blank" class="text-sm" style="color:var(--orange)">Join Meet</a>`:''}
            </div>
            ${statusBadge(i.status)}
          </div>`).join('');

    setContent(`
      <div class="page-header"><h1 class="page-title">Interviews</h1></div>
      ${calCard}
      <div class="section-label">Scheduled interviews</div>
      ${listHtml}`);

    window.gcalConnect    = ()=>{ if(S.user?.user_id) window.location.href=`/api/google/connect?recruiter_id=${S.user.user_id}`; };
    window.gcalDisconnect = async ()=>{ await POST(`/api/google/disconnect?recruiter_id=${S.user.user_id}`); mountInterviews(); };
  } catch(err) {
    setContent(`<div class="alert-error">${esc(err.message)}</div>`);
  }
}

// ============================================================
// SCREEN: Reports
// ============================================================
async function mountReports() {
  shell('#/reports','Reports','<div class="loader">Loading…</div>');
  const reportDefs = [
    {key:'gender',   label:'Gender split',        desc:'Diversity at every pipeline stage'},
    {key:'bu',       label:'Business unit summary',desc:'Headcount rollup by BU'},
    {key:'roll',     label:'On/Off-roll split',    desc:'On-roll vs off-roll breakdown'},
    {key:'positions',label:'Positions by FY',      desc:'Openings vs filled by fiscal year'},
    {key:'budget',   label:'Budget vs offered',    desc:'Budgeted CTC vs actual offers'},
    {key:'tat',      label:'Time to fill',         desc:'TAT per requisition'},
    {key:'recruiter-load',label:'Recruiter load',  desc:'Applications per recruiter'},
  ];
  let activeKey = 'gender';

  async function renderReport(key) {
    activeKey = key;
    const tabsHtml = reportDefs.map(r=>
      `<div class="tab${activeKey===r.key?' active':''}" onclick="loadReport('${esc(r.key)}')">${esc(r.label)}</div>`
    ).join('');
    setContent(`
      <div class="page-header"><h1 class="page-title">Reports</h1></div>
      <div class="tabs">${tabsHtml}</div>
      <div id="report-body"><div class="loader">Loading…</div></div>`);
    try {
      const data = await GET(`/api/reports/${key}`);
      const rb = document.getElementById('report-body');
      if(!rb) return;
      if(!data?.length) {
        rb.innerHTML='<div class="card"><div class="empty-state"><p class="empty-title">No data yet</p></div></div>'; return;
      }
      const headers = Object.keys(data[0]);
      rb.innerHTML=`<div class="card card-flush">
        <div class="table-wrap"><table>
          <thead><tr>${headers.map(h=>`<th>${esc(h.replace(/_/g,' '))}</th>`).join('')}</tr></thead>
          <tbody>${data.map(row=>`<tr>${headers.map(h=>`<td>${esc(row[h]??'—')}</td>`).join('')}</tr>`).join('')}</tbody>
        </table></div></div>`;
    } catch(err) {
      const rb2 = document.getElementById('report-body');
      if(rb2) rb2.innerHTML=`<div class="alert-error">${esc(err.message)}</div>`;
    }
  }

  window.loadReport = renderReport;
  renderReport('gender');
}

// expose nav globally so onclick attrs work
window.nav = nav;
