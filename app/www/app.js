/* FPK更新 · 前端（原生 JS，无依赖） */
'use strict';

const $ = (id) => document.getElementById(id);
let state = null;
let pollTimer = null;

/* 内置默认图标（应用没有自带图标 / 图标读不到时用） */
const DEFAULT_ICON = '/default-icon.svg';

/* 添加/换版本 选择器的状态 */
const picker = { options: null, source: '', editing: false };

/* ── 主题 ──
   三级来源：服务器设置（权威） > localStorage 缓存（避免首屏闪白） > 跟随系统。
   JS 会把"生效值"写到 <html data-theme="light|dark">，CSS 两处都认：
   一处给系统跟随（@media，防无 JS 时闪），一处给手动强制（[data-theme]）。 */
const THEME_KEY = 'fpku-theme';
const mqDark = (typeof window !== 'undefined' && window.matchMedia)
  ? window.matchMedia('(prefers-color-scheme: dark)') : null;

function applyTheme(pref) {
  const p = (pref === 'light' || pref === 'dark') ? pref : 'auto';
  const eff = (p === 'auto') ? ((mqDark && mqDark.matches) ? 'dark' : 'light') : p;
  document.documentElement.setAttribute('data-theme', eff);
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (meta) meta.setAttribute('content', eff);
  document.querySelectorAll('#theme-seg button').forEach((b) => {
    b.classList.toggle('on', b.getAttribute('data-v') === p);
  });
  try { localStorage.setItem(THEME_KEY, p); } catch (e) { /* 隐私模式忽略 */ }
  return p;
}

/* 首屏：先用缓存（或跟随系统）套上，别等接口回来才变色 */
let themePref = 'auto';
try { themePref = localStorage.getItem(THEME_KEY) || 'auto'; } catch (e) { themePref = 'auto'; }
applyTheme(themePref);

/* 跟随系统时，系统切换要跟着动 */
if (mqDark && mqDark.addEventListener) {
  mqDark.addEventListener('change', () => {
    if (themePref === 'auto') applyTheme('auto');
  });
}

/* 任务结果在卡片上保留多久（超过就不显示了，避免陈旧报错一直挂在那里） */
const JOB_TTL_MS = 30 * 60 * 1000;

/* ── 请求封装：POST 必须带自定义头（后端据此挡跨站请求） ── */
async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', credentials: 'same-origin', headers: {} };
  if (init.method === 'POST') {
    init.headers['X-Requested-With'] = 'fpk-update';
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body || {});
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
    // 带上出错的接口路径：响应不是 JSON 时（反向代理/服务器直接回错误页）
    // 光有状态码根本不知道是哪一条请求挂的（实测 501 就是这么摸不清头脑）
    const msg = (data && data.error)
      || ('请求失败（HTTP ' + res.status + ' · ' + path + '）');
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ── Toast ── */
function toast(msg, kind = '', ms = 4200) {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('toasts').appendChild(el);
  const fade = Math.max(600, ms - 400);      // 完成通知要留够阅读时间（默认 4.2s / 通知 9s）
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, fade);
  setTimeout(() => el.remove(), ms);
}

function showNotice(msg, kind = 'info') {
  const el = $('notice');
  if (!msg) { el.classList.add('hidden'); return; }
  el.textContent = msg;
  el.className = 'notice ' + kind;
}

/* ── 格式化 ── */
function fmtTime(iso) {
  if (!iso) return '尚未检查';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  const diff = Math.floor((Date.now() - d.getTime()) / 1000);
  let rel;
  if (diff < 0) rel = '刚刚';
  else if (diff < 60) rel = '刚刚';
  else if (diff < 3600) rel = Math.floor(diff / 60) + ' 分钟前';
  else if (diff < 86400) rel = Math.floor(diff / 3600) + ' 小时前';
  else rel = Math.floor(diff / 86400) + ' 天前';
  const p = (n) => String(n).padStart(2, '0');
  return rel + ' · ' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' '
    + p(d.getHours()) + ':' + p(d.getMinutes());
}

function fmtSize(n) {
  if (n === null || n === undefined) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i];
}

function normVer(v) { return String(v || '').replace(/^[vV]/, ''); }

/* 任务是否还值得显示（未结束，或结束不到 TTL） */
function taskFresh(t) {
  if (!t || !t.status) return false;
  if (t.status === 'running' || t.status === 'queued') return true;
  if (!t.finished_at) return false;
  const ts = new Date(t.finished_at).getTime();
  return !isNaN(ts) && (Date.now() - ts) < JOB_TTL_MS;
}

/* 某个应用当前/最近的任务：优先进行中/排队的，其次最近的已完成 */
function taskFor(appname) {
  const list = (state && state.tasks) || [];
  const live = list.filter((t) => t.appname === appname
    && (t.status === 'running' || t.status === 'queued'));
  if (live.length) return live[live.length - 1];
  const done = list.filter((t) => t.appname === appname && taskFresh(t));
  return done.length ? done[done.length - 1] : null;
}

/* 队列里是否有活儿（决定轮询频率与按钮禁用） */
function activeTasks() {
  return ((state && state.tasks) || []).filter(
    (t) => t.status === 'running' || t.status === 'queued');
}

/* 排序：正在动的 → 有新版本的 → 检查失败的 → 正常的；同级按名字 */
/* 卡片排序：两种模式（用户选择，存在服务端设置里）。
   · added：**添加的先后顺序** —— 服务端 apps 数组本来就是清单顺序，原样返回；
   · alpha：按显示名的字母/拼音序 —— localeCompare('zh') 在浏览器里就是拼音序。
   注意别再偷偷塞「正在跑的/有更新的置顶」：用户选了什么就是什么，不然「顺序」
   会莫名其妙地变，找东西反而更费劲。 */
function sortApps(list) {
  const mode = (state && state.settings && state.settings.sort) || 'added';
  const arr = list.slice();
  if (mode === 'alpha') {
    arr.sort((a, b) => String(a.display || a.appname || '')
      .localeCompare(String(b.display || b.appname || ''), 'zh'));
  }
  return arr;
}

/* ── 小组件 ── */
function badge(text, kind) {
  const b = document.createElement('span');
  b.className = 'badge ' + kind;
  b.textContent = text;
  return b;
}

function chip(text, cls) {
  const s = document.createElement('span');
  s.className = 'ver' + (cls ? ' ' + cls : '');
  s.textContent = text;
  return s;
}

function btn(label, cls, onClick, title) {
  const b = document.createElement('button');
  b.className = 'btn ' + cls;
  b.textContent = label;
  if (title) b.title = title;
  b.addEventListener('click', onClick);
  return b;
}

/* ── 卡片 ── */
/* ── 卡片渲染 ──
   设计原则：**一件事只说一遍**。
   之前的毛病是同一个事实出现三次（绿色"已下载"提示 + 100% 进度条 + "✓ 已下载 …fpk"），
   加上"已装 X → 最新 X"两个**相同**的版本号还配个箭头，看着就乱。现在的规则：
     · 版本行：相同时只写一次；不同才出现 → 与「最新」
     · 状态行：同一时刻只渲染一条 —— 正在跑=进度；刚结束=结果条（可关闭）；
       其余=静态状态（只在"本地有包"这类需要你知道的时候出现）
     · 按钮统一在底部一行，等宽
*/

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}

function iconBox(app) {
  const box = el('div', 'c-icon');
  const img = el('img');
  img.alt = app.display || app.appname;
  img.loading = 'lazy';
  img.addEventListener('error', () => {
    if (!img.dataset.fallback) {
      img.dataset.fallback = '1';
      img.src = DEFAULT_ICON;
    }
  });
  img.src = app.has_icon
    ? ('/api/icon/' + encodeURIComponent(app.appname) + '?v=' + encodeURIComponent(app.installed || ''))
    : DEFAULT_ICON;
  box.appendChild(img);
  return box;
}

/* 右上角状态徽章：一个卡片只挂一个 */
function statusBadge(app, task, running) {
  if (running) {
    return badge(actionLabel(task.action) + (task.status === 'queued' ? '排队中' : '中'), 'new');
  }
  if (app.error && !app.latest) return badge('检查失败', 'err');
  // 未安装：不是错误，是「等你装」的状态。徽章用中性色，别混进「有新版本」里。
  if (app.uninstalled) {
    return (app.downloaded && app.downloaded.complete)
      ? badge('安装包已就绪', 'ok') : badge('未安装', 'wait');
  }
  if (app.update_available) return badge('有新版本', 'new');
  // 有更新的版本、但不能自动更新（典型原因：该来源没有 sha256 摘要）。
  // 早先这种情况会掉到下面的「已是最新」——那是**误导**：明明有新版。
  if (app.relation === 'newer') return badge('有新版 · 不能自动更新', 'warn');
  if (app.relation === 'older') return badge('已钉旧版', 'wait');
  if (app.latest) return badge('已是最新', 'ok');
  return badge('未知', 'wait');
}

/* 版本行：相同时只显示一次，不同才画箭头 */
function versionRow(app) {
  const box = el('div', 'c-vers');
  const same = app.latest && normVer(app.latest) === normVer(app.installed);

  const c1 = el('div', 'c-cell');
  // 未安装的卡片：第一格说「本机未安装」，不要写成「已装 —」（读起来像坏了）
  c1.appendChild(el('span', 'c-label', app.uninstalled ? '本机' : '已装'));
  c1.appendChild(el('span', 'c-val', app.uninstalled ? '未安装' : (app.installed || '—')));
  box.appendChild(c1);

  if (app.latest && !same) {
    box.appendChild(el('span', 'c-arrow', '→'));
    const c2 = el('div', 'c-cell');
    c2.appendChild(el('span', 'c-label', '最新'));
    c2.appendChild(el('span', 'c-val new', app.latest));
    box.appendChild(c2);
  }

  const meta = [];
  if (app.size) meta.push(fmtSize(app.size));
  if (app.published_at) meta.push(fmtTime(app.published_at).replace(/（.*/, '') + ' 发布');
  if (meta.length) box.appendChild(el('span', 'c-meta', meta.join(' · ')));
  return box;
}

/* 静态状态行：只在"本地有包"这类需要你知道的时候出现，且只有一条 */
function staticState(app) {
  // 未安装的应用：说清「包在不在本地、点哪个按钮装」——安装由本工具一条龙完成
  if (app.uninstalled) {
    const done = !!(app.downloaded && app.downloaded.complete);
    const lu = app.local_unknown;
    const cls = done ? 'ok' : (lu ? 'warn' : 'muted');
    let text;
    if (done) {
      text = '安装包已下好：' + app.downloaded.version + '（' + fmtSize(app.downloaded.size)
        + '）—— 点「安装」并选个存储空间即可';
    } else if (lu) {
      text = '本地同名文件' + (lu.reason ? '：' + lu.reason : '与这一版不一致')
        + '（' + fmtSize(lu.size) + '）—— 点「下载并安装」会重新取一份覆盖它';
    } else {
      text = '点「下载并安装」：自动下载、校验 sha256，再由应用中心装好并启动';
    }
    const row = el('div', 'c-state ' + cls);
    row.appendChild(el('span', 'c-state-ico', done ? '↓' : (lu ? '!' : '·')));
    row.appendChild(el('span', '', text));
    return row;
  }
  const d = app.downloaded;
  if (!d) return localUnknownRow(app);   // 有同名外来文件时，改为如实说明它是什么
  const differs = normVer(d.version) !== normVer(app.installed);
  let cls = 'ok';
  let text;
  if (!d.complete) {
    cls = 'warn';
    text = '本地安装包不完整（' + fmtSize(d.size) + '），建议重新下载';
  } else if (differs) {
    text = '安装包已在本地：' + d.version + '（' + fmtSize(d.size) + '），可直接安装';
  } else {
    cls = 'muted';
    text = '安装包已在本地（' + fmtSize(d.size) + '）';
  }
  const row = el('div', 'c-state ' + cls);
  row.appendChild(el('span', 'c-state-ico', cls === 'warn' ? '!' : (cls === 'ok' ? '↓' : '·')));
  row.appendChild(el('span', '', text));
  return row;
}

/* 本地同名文件与这一版对不上（按内容核对，不看谁下载的）。
   为什么单独说：下载目录往往同时是用户的归档目录，同名文件很可能属于旧版本 ——
   不声不响地拿它去装，会出现「sha256 与 Release 不一致」这种把正常状态说成被篡改的报错。 */
function localUnknownRow(app) {
  const lu = app.local_unknown;
  if (!lu) return null;
  // 已经有「本地安装包…」那条状态行时不再重复说（同一件事只说一次）
  const row = el('div', 'c-state warn');
  row.appendChild(el('span', 'c-state-ico', '!'));
  // 只在**内容真的对不上**时才提示（认包看的是字节，不是「谁下载的」——
  // 归档目录里的包常常是别的脚本下的，用「谁下的」当判据会把它们全误判成外来、还把「安装」禁掉）
  row.appendChild(el('span', '', '本地同名文件' + (lu.reason ? '：' + lu.reason : '与这一版不一致')
    + '（' + fmtSize(lu.size) + '）—— 点「下载」重新取一份会覆盖它'));
  return row;
}

function formatNotes(text) {
  const box = el('div', 'c-notes-body');
  if (!text) return box;
  const lines = String(text).split('\n');
  lines.forEach((line, idx) => {
    if (idx > 0) box.appendChild(document.createElement('br'));
    let lastIndex = 0;
    const re = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
    let m;
    while ((m = re.exec(line)) !== null) {
      if (m.index > lastIndex) {
        box.appendChild(document.createTextNode(line.slice(lastIndex, m.index)));
      }
      const a = document.createElement('a');
      a.textContent = m[1];
      a.href = m[2];
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      box.appendChild(a);
      lastIndex = re.lastIndex;
    }
    if (lastIndex < line.length) {
      box.appendChild(document.createTextNode(line.slice(lastIndex)));
    }
  });
  return box;
}

function cardMenu(app, task, running) {
  const wrap = el('div', 'c-menu-wrap');
  const dot = el('span', 'card-menu-btn', '···');
  dot.setAttribute('role', 'button');
  dot.setAttribute('tabindex', '0');
  dot.title = '更多操作';
  let drop = null;
  const close = () => { if (drop) { drop.remove(); drop = null; } };
  dot.addEventListener('click', (ev) => {
    ev.stopPropagation();
    if (drop) { close(); return; }
    document.querySelectorAll('.card-menu-dropdown').forEach((d) => d.remove());
    drop = el('div', 'card-menu-dropdown');
    // 1. 单独检查
    const itCheck = el('div', 'card-menu-item', '🔄 检查此应用');
    itCheck.addEventListener('click', async (e) => {
      e.stopPropagation(); close();
      toast('正在检查「' + (app.display || app.appname) + '」最新版本…', '');
      try {
        await api('/api/apps/check-one', { method: 'POST', body: { appname: app.appname } });
        await refresh();
        toast('已更新「' + (app.display || app.appname) + '」版本信息', 'ok');
      } catch (err) { toast(err.message, 'err'); }
    });
    drop.appendChild(itCheck);
    // 2. 仓库页面
    if (app.repo) {
      const itRepo = el('div', 'card-menu-item', '↗ 访问 GitHub 仓库');
      itRepo.addEventListener('click', () => {
        close();
        if (typeof window !== 'undefined' && window.open) {
          window.open('https://github.com/' + app.repo, '_blank');
        }
      });
      drop.appendChild(itRepo);
    }
    // 3. 从监控列表移除
    const itDel = el('div', 'card-menu-item danger', '🗑️ 从列表移除');
    itDel.addEventListener('click', async (e) => {
      e.stopPropagation(); close();
      const ok = await askDialog({
        title: '移除「' + (app.display || app.appname) + '」',
        notes: ['只会把它从被管理应用列表里去掉，不会卸载应用、也不会删任何文件。'],
        okText: '移除',
        danger: true,
      });
      if (!ok) return;
      try {
        await api('/api/apps/remove', { method: 'POST', body: { appname: app.appname } });
        toast('已从监控列表中移除', 'ok');
        await refresh();
      } catch (err) { toast(err.message, 'err'); }
    });
    drop.appendChild(itDel);
    wrap.appendChild(drop);
  });
  if (typeof document !== 'undefined') {
    document.addEventListener('click', close);
  }
  return wrap;
}

function renderCard(app) {
  const task = taskFor(app.appname);
  const running = !!(task && (task.status === 'running' || task.status === 'queued'));
  const finished = !!(task && !running);
  // 只禁用**这个应用自己**的按钮。原来是"有任何一个任务在跑就禁用所有卡片"，
  // 那等于让队列形同虚设 —— 想给三个应用排队下载都做不到。队列本来就是为这个做的。
  const busy = running;

  const card = el('div', 'card');
  if (app.update_available) card.classList.add('has-update');

  /* 头部 */
  const head = el('div', 'c-head');
  head.appendChild(iconBox(app));
  const ident = el('div', 'c-ident');
  const nameRow = el('div', 'c-name');
  nameRow.appendChild(el('strong', '', app.display || app.appname));
  if (app.appname && app.appname !== (app.display || '')) {
    nameRow.appendChild(el('span', 'appname', app.appname));
  }
  ident.appendChild(nameRow);
  ident.appendChild(el('div', 'c-src', app.repo || app.url || ''));
  head.appendChild(ident);
  head.appendChild(statusBadge(app, task, running));
  head.appendChild(cardMenu(app, task, running));
  card.appendChild(head);

  /* 版本 */
  card.appendChild(versionRow(app));

  /* 更新说明（对标应用商店：让人看到"这版改了什么"）。GitHub Release 正文，可展开。 */
  if (app.notes) {
    const d = el('details', 'c-notes');
    d.appendChild(el('summary', '', '更新说明'));
    d.appendChild(formatNotes(app.notes));
    card.appendChild(d);
  }

  /* 状态：进度 / 结果 / 静态，三选一。
     注意：子节点可能是 null（例如成功时不再留结果条）——真浏览器的 appendChild(null)
     会直接抛异常、整张卡片画不出来，所以必须先判断再挂。 */
  const put = (node) => { if (node) card.appendChild(node); };
  if (running) {
    put(progressBlock(task, 'run'));
  } else if (finished) {
    put(resultStrip(task, app));
    // 成功：结果条为空 → 回到与其他「已是最新」一样的普通静态状态
    if (task.status === 'ok') put(staticState(app));
  } else {
    put(staticState(app));
  }

  if (app.error) card.appendChild(el('div', 'c-err', app.error));

  /* 按钮行：只留**一个**动作按钮。
     用户要求：去掉「下载」「下载并更新」，只显示「更新」；点它时
     **有已校验过的本地包就直接装、没有就先下载再装** —— 这个判断在后端做，不让用户选。
     换版本回滚（钉了旧版本、没新版可升）时按钮显示为「安装」，走同一条路。 */
  const actions = el('div', 'c-actions');

  /* 未安装的应用（v1.3 完整安装）：按钮随包状态自动切换，点它弹「存储空间选择」。
     没包 →「下载并安装」（一条龙：下载→校验→应用中心装好）；
     有包 →「安装」（复用已校验的包，不重复下载）。 */
  if (app.uninstalled) {
    const ready = !!(app.downloaded && app.downloaded.complete);
    const b = btn(ready ? '安装' : '下载并安装', 'primary sm',
      () => doFirstInstall(app, ready, b),
      ready ? '用本地已校验的安装包完成安装（可先选存储空间）'
            : '先下载并校验安装包，再由应用中心完成安装（可先选存储空间）');
    b.disabled = busy || !app.latest || (!!app.error && !ready);
    actions.appendChild(b);
    card.appendChild(actions);
    // 静默预加载：若已有校验过的本地包，空闲时预热向导参数与存储空间，点「安装」立即可见弹窗
    if (ready && app.latest) {
      if (typeof requestIdleCallback === 'function') {
        requestIdleCallback(() => { getVolumes(); getWizard(app.appname); });
      } else {
        setTimeout(() => { getVolumes(); getWizard(app.appname); }, 100);
      }
    }
    return card;
  }

  const upToDate = !!app.installed && !!app.latest
    && normVer(app.installed) === normVer(app.latest) && !app.update_available;
  const localReady = !!(app.downloaded && app.downloaded.complete);
  const brokenLocal = !!(app.downloaded && app.downloaded.complete === false);
  const needAction = (!!app.latest && !upToDate) || brokenLocal
    || (localReady && normVer(app.downloaded.version) !== normVer(app.installed))
    || !!app.local_unknown;
  const actLabel = app.update_available ? '更新' : '安装';
  // 已是最新时按钮文案直接写「已是最新」：写「安装」会让用户以为还能点（截图反馈过）
  const bAct = btn(upToDate ? '已是最新' : actLabel, 'primary sm',
    () => doAction(app, 'update', actLabel, bAct),
    !app.latest ? '暂无可用版本'
      : (!needAction ? '已是最新，无需操作'
                     : (localReady ? ('用已下好的 ' + app.downloaded.version + ' 安装（不重复下载）')
                                   : ('先下载 ' + app.latest + ' 再安装，一步完成'))));
  bAct.disabled = !needAction || busy;
  actions.appendChild(bAct);
  card.appendChild(actions);

  return card;
}

function actionLabel(a) {
  return { download: '下载', install: '安装', update: '更新' }[a] || '处理';
}

/* 进度块（只在任务进行中显示）：进度条 + 文字 + 百分比 */
function progressBlock(task, mode) {
  const p = el('div', 'c-progress');

  const bar = el('div', 'bar');
  const fill = el('i');
  if (task && task.percent !== null && task.percent !== undefined) {
    fill.style.width = Math.max(0, Math.min(100, task.percent)) + '%';
  } else {
    bar.classList.add('indet');
  }
  bar.appendChild(fill);
  p.appendChild(bar);

  const line = el('div', 'line');
  line.appendChild(el('span', 'txt', (task && task.message) || '处理中…'));
  if (task && task.percent !== null && task.percent !== undefined) {
    line.appendChild(el('span', 'pct', task.percent + '%'));
  }
  p.appendChild(line);
  return p;
}

/* 结果条（任务刚结束时显示一条，可关闭/可重试；没有进度条，避免与结果重复表达） */
function resultStrip(task, app) {
  const ok = task.status === 'ok';
  // 成功不留在卡片上：任务完成时单独弹一条通知（见 notifyFinishedTasks），
  // 卡片就回到和其他「已是最新」一模一样的普通形态 —— 这是用户明确要的。
  // 失败仍然留在卡片上，因为要能就地「重试」。
  if (ok) return null;
  const row = el('div', 'c-state ' + (ok ? 'ok' : 'bad'));
  row.appendChild(el('span', 'c-state-ico', ok ? '✓' : '✕'));
  row.appendChild(el('span', '', task.message || (ok ? '完成' : '失败')));
  // 失败给个「重试」——手机应用商店都这么做，比让用户自己找按钮回来点强
  if (!ok && app) {
    const re = el('button', 'mini', '重试');
    re.title = '按同样的操作再试一次';
    re.addEventListener('click', () => doAction(app, task.action, actionLabel(task.action)));
    row.appendChild(re);
  }
  const x = el('button', 'dismiss', '×');
  x.title = '关闭这条提示';
  x.addEventListener('click', async () => {
    try { await api('/api/job/dismiss', { method: 'POST' }); } catch (e) { /* 忽略 */ }
    await refresh();
  });
  row.appendChild(x);
  return row;
}
/* ── 渲染 ── */
/* 回填输入框：正在输入的框不动（否则会打断用户输入），其余一律同步服务端的值 */
function syncInput(id, val) {
  const el = $(id);
  if (!el) return;
  const active = document.activeElement;
  if (active && active === el) return;
  el.value = val;
}

let lastChecking = false;      // 用于识别「检查刚结束」，只在那一刻弹结论
let notifiedTasks = null;      // 已经通知过的任务 id（避免每轮轮询重复弹）
let bootVersion = '';          // 页面加载时本应用的版本（用于识别「自己升级了自己」）
let selfUpdated = false;       // 只自动刷新一次，别循环

/* 任务结束时弹一条通知 —— 而不是在卡片上长期留一条结果条。
   用户要的是：「更新完成」单独弹一下，卡片恢复成和其他「已是最新」一样的普通形态。 */
function notifyFinishedTasks(tasks) {
  const now = Date.now();
  const fresh = (tasks || []).filter((t) => {
    if (t.status !== 'ok' && t.status !== 'failed') return false;
    const ts = Date.parse(t.finished_at || '');
    return !isNaN(ts) && (now - ts) < 5 * 60 * 1000;      // 只看最近 5 分钟
  });
  if (notifiedTasks === null) {
    // 首次渲染：把「一分钟以前就结束的」直接标记为已通知，免得一打开页面弹一堆历史
    notifiedTasks = new Set(fresh.filter((t) => {
      const ts = Date.parse(t.finished_at);
      return (now - ts) > 60000;
    }).map((t) => t.id));
  }
  fresh.forEach((t) => {
    if (notifiedTasks.has(t.id)) return;
    notifiedTasks.add(t.id);
    const nm = t.display || t.appname;
    const body = t.message || (actionLabel(t.action) + (t.status === 'ok' ? '完成' : '失败'));
    toast(nm + '：' + body, t.status === 'ok' ? 'ok' : 'err', 9000);
  });
}

let searchQuery = '';

function render() {
  const s = state;
  const ck = s.checking || {};

  /* 顶栏：检查中显示进度（x/y + 正在查哪个），检查完恢复「上次检查 时间」 */
  if (ck.running) {
    const t = '正在检查 ' + (ck.done || 0) + '/' + (ck.total || 0)
      + (ck.current ? '（' + ck.current + '）' : '') + '…';
    $('last-check').textContent = t;
    $('last-check').title = t;
  } else {
    const t = s.last_check ? ('上次检查 ' + fmtTime(s.last_check)) : '尚未检查';
    $('last-check').textContent = t;
    $('last-check').title = s.last_check ? new Date(s.last_check).toLocaleString() : '尚未检查';
  }
  const cbtn = $('check-btn');
  if (cbtn) {
    cbtn.classList.toggle('loading', !!ck.running);
    cbtn.disabled = !!ck.running;
  }
  if (lastChecking && !ck.running) {
    // 检查刚结束：给一句结论（原来这结论是在 doCheck 里 await 之后弹的）
    const apps = s.apps || [];
    const n = apps.filter((a) => a.update_available).length;
    const errs = apps.filter((a) => a.error).length;
    if (n) toast('检查完成：' + n + ' 个应用有新版本');
    else if (errs) toast('检查完成，但有 ' + errs + ' 个应用出错', 'err');
    else toast('检查完成：全部已是最新', 'ok');
  }
  lastChecking = !!ck.running;

  // 任务结束 → 单独弹一条通知（成功与失败都弹；失败另外还留在卡片上可重试）
  notifyFinishedTasks(s.tasks);

  /* 自己升级了自己 → 页面加载的 JS/CSS 已经过期，必须刷新。
     为什么单独处理：自更新时应用中心会重启进程，页面还停在旧文件上；
     而且这是唯一「刷新之后界面才正确」的场景（其他升级都不影响本页面）。 */
  if (!selfUpdated && bootVersion && s.app_version && s.app_version !== bootVersion) {
    selfUpdated = true;
    toast('本应用已更新到 ' + s.app_version + '，正在刷新界面…', 'ok', 3000);
    setTimeout(() => { try { location.reload(); } catch (e) { /* 忽略 */ } }, 1200);
  }
  if (!bootVersion && s.app_version) bootVersion = s.app_version;

  const cards = $('cards');
  const apps = s.apps || [];

  /* 列表工具条：视图过滤（全部 / 可更新）+ 排序方式（无应用时隐藏） */
  const lb = $('listbar');
  if (lb) {
    lb.classList.toggle('hidden', !apps.length);
    const nUp = apps.filter((a) => a.update_available).length;
    const bAll = $('badge-all');
    if (bAll) bAll.textContent = String(apps.length);
    const bUp = $('badge-update');
    if (bUp) bUp.textContent = String(nUp);

    const vMode = (s.settings && s.settings.filter) || 'all';
    const btnAll = $('view-all');
    if (btnAll) btnAll.classList.toggle('on', vMode === 'all');
    const btnUp = $('view-update');
    if (btnUp) btnUp.classList.toggle('on', vMode === 'update');

    const mode = (s.settings && s.settings.sort) || 'added';
    ['added', 'alpha'].forEach((m) => {
      const b = $('sort-' + m);
      if (b) b.classList.toggle('on', m === mode);
    });
  }

  if (!apps.length) {
    cards.textContent = '';
    const empty = document.createElement('div');
    empty.className = 'card empty-guide';
    const b = document.createElement('div');
    b.className = 'body';
    const t = document.createElement('strong');
    t.textContent = '还没有添加被管理的应用';
    const p = document.createElement('div');
    p.className = 'note';
    p.textContent = '支持任何在 Releases 中发布 .fpk 的 GitHub 仓库。输入仓库地址即可自动识别版本并检查更新。';
    const addAction = document.createElement('button');
    addAction.className = 'btn primary sm';
    addAction.textContent = '+ 添加第一个应用';
    addAction.style.marginTop = '12px';
    addAction.addEventListener('click', openAddApp);
    empty.classList.add('enter');
    b.appendChild(t); b.appendChild(p); b.appendChild(addAction); empty.appendChild(b);
    cards.appendChild(empty);
  } else {
    /* 排序（对标应用商店）：正在动的 → 有新版本的 → 正常的，同级按名字。
       这样「需要你看的东西」永远在最上面，不用在一堆卡片里找。

       ★ 差分更新：签名没变的卡片**复用原节点**，不再每轮清空重建。
       为什么必须这么做：原来每轮轮询都 `cards.textContent=''` 再全量重建 ——
       每次重建都新建 <img>，浏览器就得重新取一次图标（6 个/轮，有活儿时 1 秒一轮），
       而且整块 DOM 反复销毁重建本身就是可见的抖动/闪烁。实测这是「页面感觉卡」的
       主要来源之一。签名里带上该应用的任务状态，所以进度变化仍会重绘那一张卡。 */
    const sigOf = (a) => {
      const t = taskFor(a.appname);
      return JSON.stringify([a.display, a.has_icon, a.installed, a.latest,
        a.update_available, a.relation, a.error, a.size, a.published_at, a.notes,
        a.downloaded, a.local_unknown, a.repo, a.url, a.asset_name,
        t ? [t.id, t.status, t.percent, t.message, t.finished_at] : null]);
    };
    const pool = {};
    Array.prototype.slice.call(cards.children).forEach((n) => {
      if (n.__sig) (pool[n.__sig] = pool[n.__sig] || []).push(n);
    });
    const keep = [];
    /* 首屏（第一次拿到数据）：卡片加 .enter，按顺序错开 45ms 淡入 ——
       只播这一次；之后轮询重建的卡片（进度变化会重建）绝不能带 .enter，
       否则每秒重播一次入场动画，界面会持续"闪"。 */
    const firstPaint = !cards.__painted;
    cards.__painted = true;
    let listToShow = sortApps(apps);
    const filterMode = (s.settings && s.settings.filter) || 'all';
    if (filterMode === 'update') {
      listToShow = listToShow.filter((a) => a.update_available);
    }
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      listToShow = listToShow.filter((a) => {
        const d = (a.display || '').toLowerCase();
        const n = (a.appname || '').toLowerCase();
        const r = (a.repo || a.url || '').toLowerCase();
        return d.includes(q) || n.includes(q) || r.includes(q);
      });
    }
    listToShow.forEach((a, i) => {
      const sig = sigOf(a);
      const bag = pool[sig];
      let node = (bag && bag.length) ? bag.shift() : null;
      if (!node) {
        node = renderCard(a);
        node.__sig = sig;
        if (firstPaint) {
          node.classList.add('enter');
          node.style.animationDelay = Math.min(i * 45, 320) + 'ms';
        }
      }
      keep.push(node);
    });
    keep.forEach((n, i) => {
      if (cards.children[i] !== n) {
        if (cards.insertBefore) cards.insertBefore(n, cards.children[i] || null);
        else cards.appendChild(n);
      }
    });
    Array.prototype.slice.call(cards.children).forEach((n) => {
      if (keep.indexOf(n) === -1 && cards.removeChild) cards.removeChild(n);
    });
  }

  /* 本应用自身：列表上方独立一块（列表里全是「别的应用」） */
  renderSelf(s);

  /* 顶部汇总：有几个可更新 + 一键全部更新（对标手机应用商店）。
     注意：这里只数 apps（被管理的应用）—— 自身更新不在里面，
     所以「全部更新」天然不会带上自己。 */
  renderSummary(s);

  /* 顶部：进行中的任务（含排队），后台跑，关掉页面也不影响 */
  const live = activeTasks();
  if (live.length) {
    const cur = live.find((t) => t.status === 'running') || live[0];
    const extra = live.length > 1 ? ('（队列还有 ' + (live.length - 1) + ' 个）') : '';
    showNotice('后台任务：' + (cur.display || cur.appname) + ' · '
      + actionLabel(cur.action) + ' · ' + (cur.message || '处理中…') + ' ' + extra
      + '　—— 可以关掉本页面，任务会在后台继续。', 'info');
  } else if (s.checking && s.checking.running) {
    showNotice('正在检查更新：' + (s.checking.done || 0) + '/' + (s.checking.total || 0)
      + '（各应用并行查，查完一个卡片就更新一个，不用等整轮）', 'info');
  } else if (!s.has_token) {
    showNotice('还没设置 GitHub 访问令牌：匿名额度只有 60 次/小时且按出口 IP 共享，'
      + '「检查更新」容易被限流。点右上角「设置」填一个令牌就稳了。', 'info');
  } else {
    showNotice('');
  }

  /* 设置面板的内容只在弹窗打开时同步（首次打开由 openSettings() 补一次），
     空闲轮询不再每次回填一堆输入框。顶栏版本标签仍每轮更新。 */
  $('app-ver-chip').textContent = 'v' + (s.app_version || '—');
  if ($('settings-modal') && !$('settings-modal').classList.contains('hidden')) {
    syncSettingsPanels(s);
  }
}

/* 设置面板的同步（输入框回填 / 只读信息 / 列表渲染）。
   从 render() 里抽出来：设置弹窗关着时完全不用跑（否则每轮轮询都白做一遍）。
   打开设置时 openSettings() 会调它一次 —— 所以不会看到旧值。 */
function syncSettingsPanels(s) {
  /* 设置里的只读信息 */
  $('wl-path').textContent = s.whitelist_file || '—';
  // 本应用的版本：顶栏一个常驻小标签（一眼可见）+ 设置「运行信息」一行（排障时看）
  const av = s.app_version || '—';
  $('app-ver-chip').textContent = 'v' + av;
  $('app-ver-kv').textContent = av;
  $('auto-note').textContent = '每 ' + (s.check_interval_min || 60) + ' 分钟一次（只查版本，不会自动升级）';
  renderAppsList(s.whitelist || []);
  renderTaskList(s.tasks || []);
  $('token-state').textContent = s.has_token ? ('当前：已设置 ' + (s.token_hint || '')) : '当前：未设置（走匿名额度）';
  const qInfo = s.gh_rate_limit || {};
  const qVal = $('quota-val');
  const qFill = $('quota-fill');
  const qHint = $('quota-hint');
  const qBar = qFill ? qFill.parentElement : null;
  if (qInfo && qInfo.limit) {
    const rem = qInfo.remaining !== undefined ? qInfo.remaining : 0;
    const lim = qInfo.limit || 60;
    const pct = Math.max(0, Math.min(100, Math.round((rem / lim) * 100)));
    if (qVal) qVal.textContent = rem + ' / ' + lim + (s.has_token ? '（令牌）' : '（匿名）');
    if (qFill) qFill.style.width = pct + '%';
    if (qBar) {
      qBar.classList.toggle('warn', pct <= 25 && pct > 10);
      qBar.classList.toggle('danger', pct <= 10);
    }
    if (qHint) {
      const rstTime = qInfo.reset ? new Date(qInfo.reset * 1000).toLocaleTimeString() : '—';
      qHint.textContent = '重置时间约：' + rstTime + (rem <= 5 ? '（⚠ 配额即将耗尽）' : '');
    }
  } else {
    if (qVal) qVal.textContent = s.has_token ? '5000 次/小时' : '60 次/小时';
    if (qFill) qFill.style.width = '100%';
    if (qHint) qHint.textContent = s.has_token ? '已配置访问令牌，额度充裕' : '未配置访问令牌，使用共享匿名配额';
  }
  const st = s.settings || {};
  const cur = st.download_dir || '—';
  $('dl-state').textContent = '当前：' + cur
    + (cur === s.default_download_dir ? '（默认）' : '');
  syncInput('dl-input', cur);
  syncInput('conc-input', String(st.concurrency || 4));
  $('auto-dl-input').checked = !!st.auto_download;
  $('auto-clean-input').checked = !!st.auto_clean;
  $('keep-current-input').checked = st.clean_keep_current !== false;
  const pk = s.packages || {};
  const fg = pk.foreign_count || 0;
  $('pkg-state').textContent = (pk.count
    ? ('可清理：' + pk.count + ' 个 · ' + fmtSize(pk.size))
    : '可清理：无（本应用下载的包，装完会自动清掉）')
    + (pk.keep_count ? ('　·　保留当前版本 ' + pk.keep_count + ' 个（' + fmtSize(pk.keep_size) + '）') : '')
    + (fg ? ('　·　另有 ' + fg + ' 个不是本应用下载的（' + fmtSize(pk.foreign_size)
      + '），不会动') : '')
    + ((pk.parts_count) ? ('　·　下载残留 ' + pk.parts_count + ' 个（'
      + fmtSize(pk.parts_size) + '）') : '');
  // 主题以服务端设置为准（首屏用的是本地缓存）
  if (st.theme && st.theme !== themePref) {
    themePref = applyTheme(st.theme);
  } else {
    document.querySelectorAll('#theme-seg button').forEach((b) => {
      b.classList.toggle('on', b.getAttribute('data-v') === themePref);
    });
  }
  fillMirrorSelect(st.mirror || '', s.mirror_presets || []);
  // 自身更新用哪条来源（留空 = 内置默认）
  syncInput('self-repo-input', st.self_repo || s.self_default_repo || '');
  $('self-repo-kv').textContent = (s.self_repo || s.self_default_repo || '—')
    + (s.self_repo ? '' : '（内置默认）');
}

/* ── 本应用自身（**单独一块**，不混进被管理的列表） ──
   为什么必须单独：更新它是「自己更新自己」—— 应用中心会重启本进程，页面上的
   js/css 立刻过期（见 render() 里的自动刷新）；「全部更新」绝不能带上它，否则
   队列里排在后面的应用会被连累着中断；它也没有「本机已安装」那道门（自己当然装了）。
   服务端把它的检测结果单独放在 state.self 里，这里只负责画。 */
function renderSelf(s) {
  const box = $('selfbar');
  if (!box) return;
  const it = s.self;
  if (!it) { box.classList.add('hidden'); return; }   // 还没检测过：这一块先不出现
  box.classList.remove('hidden');
  box.classList.toggle('has-update', !!it.update_available);

  const task = taskFor(it.appname);
  const running = !!(task && (task.status === 'running' || task.status === 'queued'));
  const finished = !!(task && !running);
  const now = s.app_version || it.installed || '';
  const latest = it.latest || '';
  const newer = !!it.update_available;

  $('sb-name').textContent = it.display || '本应用';
  $('sb-ver').textContent = 'v' + (now || '—');

  /* 状态文字：一次只说一件事（进行中 > 出错 > 有新版 > 已是最新） */
  const st = $('sb-state');
  if (running) {
    st.className = 'sb-state run';
    st.textContent = task.message || '处理中…';
    st.title = task.message || '处理中…';
  } else if (it.error) {
    st.className = 'sb-state bad';
    st.textContent = it.error;
    st.title = it.error;
  } else if (newer) {
    st.className = 'sb-state new';
    st.textContent = '有新版 ' + latest;
    st.title = '当前 ' + now + ' → 最新 ' + latest;
  } else {
    st.className = 'sb-state';
    st.textContent = latest ? '已是最新' : '还没查过';
    st.title = latest ? ('已安装 ' + now + '，已是最新版本') : '尚未检查';
  }

  /* 重新检测（只看它自己、绕过缓存）：检查中由顶栏统一显示进度，这里跟着禁用 */
  const ckRunning = !!(s.checking && s.checking.running);
  const bck = $('sb-check');
  if (bck) {
    bck.disabled = running || ckRunning;
    bck.classList.toggle('loading', ckRunning);
  }

  /* 动作按钮：只有真的能更新时才出现 —— 摆在那儿却不能点，比不出现更让人困惑 */
  const b = $('sb-update');
  if (b) {
    b.hidden = !newer;
    b.classList.toggle('hidden', !newer);
    b.disabled = running || !newer;
    b.title = newer ? ('更新到 ' + latest + '（会重启本应用，装完页面自动刷新）') : '';
  }

  /* 附加区：进度 / 失败结果条（可重试）/ 更新说明，一次一件 */
  const ex = $('sb-extra');
  if (ex) {
    ex.textContent = '';
    if (running) {
      ex.appendChild(progressBlock(task, 'run'));
    } else if (finished && task.status !== 'ok') {
      ex.appendChild(resultStrip(task, it));
    } else if (newer && it.notes) {
      const d = el('details', 'c-notes');
      d.appendChild(el('summary', '', '更新说明'));
      d.appendChild(el('div', 'c-notes-body', it.notes));
      ex.appendChild(d);
    }
  }
}

async function doSelfUpdate() {
  const it = (state && state.self) || {};
  const conf = CONFIRM.update(it);
  conf.title = '更新「本应用」（FPK更新）到 ' + (it.latest || '新版');
  conf.notes = conf.notes.concat([
    '这是「自己更新自己」：装完飞牛应用中心会重启本应用，页面会自动刷新一次，中途短暂打不开是正常的。',
  ]);
  if (!(await askDialog(conf))) return;
  try {
    const r = await api('/api/self/update', { method: 'POST', body: {} });
    toast(r.message || '已加入后台队列，可以关掉页面');
    await refresh();
    startPoll();
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* ── 顶部汇总条 + 全部更新 ── */
function renderSummary(s) {
  const box = $('summary');
  const apps = s.apps || [];
  const n = apps.filter((a) => a.update_available).length;
  const errs = apps.filter((a) => a.error && !a.latest).length;
  if (!n && !errs) { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  const bits = [];
  if (n) bits.push(n + ' 个应用有新版本');
  if (errs) bits.push(errs + ' 个检查失败');
  $('summary-txt').textContent = bits.join(' · ');
  $('all-download').classList.toggle('hidden', !n);
}

async function doAll(action, triggerBtn) {
  const apps = (state.apps || []).filter((a) => a.update_available);
  const names = apps.map((a) => a.display || a.appname).join('、');
  const what = action === 'update' ? '更新' : '下载';
  if (!apps.length) {
    // 顶栏「一键更新」常驻可见，点它时「没有可更新的」是正常状态而不是错误 ——
    // 用提示语气（不是红字报错），并告诉用户另有一个刷新动作可用。
    toast(action === 'update'
      ? '所有应用均已是最新，无需更新（想立刻复查可点「检查更新」）'
      : '没有需要下载的应用（均为最新）', '');
    return;
  }
  if (triggerBtn) busyOn(triggerBtn, '正在准备…');
  let okAll = false;
  try {
    okAll = await askDialog({
      title: '对 ' + apps.length + ' 个应用执行「全部' + what + '」',
      rows: apps.map((a) => [[a.display || a.appname, a.update_available ? (a.installed + ' → ' + a.latest) : '']]),
      notes: [action === 'update'
        ? '会依次下载并升级，每装一个该应用会短暂重启。队列一次跑一个。'
        : '只下载安装包到本地，不改动任何应用。'],
      okText: '全部' + what,
    });
  } finally {
    if (triggerBtn) busyOff(triggerBtn);
  }
  if (!okAll) return;
  try {
    const r = await api('/api/all', { method: 'POST', body: { action } });
    const okN = (r.queued || []).length;
    const skipN = (r.skipped || []).length;
    toast('已排队 ' + okN + ' 个' + (skipN ? ('，跳过 ' + skipN + ' 个') : ''), 'ok');
    await refresh();
    startPoll();
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* ── 安装包清理（一键） ── */
async function doClean(triggerBtn) {
  if (triggerBtn) busyOn(triggerBtn, '正在分析待清理文件…');
  let r;
  try {
    // 先 dry 拿清单（只复盘、不删），让确认框里能看到到底会删什么
    r = await api('/api/clean', { method: 'POST', body: { dry: true } });
  } catch (e) {
    toast(e.message, 'err');
    return;
  } finally {
    if (triggerBtn) busyOff(triggerBtn);
  }
  const items = r.items || [];
  const fgn = r.foreign_count || 0;
  const parts = r.parts_count || 0;
  const selfN = r.self_count || 0;
  if (!items.length && !parts && !selfN) {
    toast('没有可清理的安装包' + (fgn ? '（另有 ' + fgn + ' 个不是本应用下载的，不会动）' : ''), '');
    await refresh();
    return;
  }
  const total = items.length + selfN;
  const where = items.length
    ? items[0].path.replace(/\/[^/]*$/, '')
    : ('下载目录');
  const okClean = await askDialog({
    title: total
      ? ('清理 ' + total + ' 个安装包')
      : ('清理 ' + parts + ' 个下载残留'),
    rows: [items.map((i) => [i.name, fmtSize(i.size)])
      .concat((r.parts || []).slice(0, 8).map((n) => [n, '下载残留']))
      .concat((r.parts || []).length > 8
        ? [['… 另有 ' + ((r.parts || []).length - 8) + ' 个下载残留', '']] : [])],
    notes: [
      // 本应用自己的安装器（旧版 fpk-update-x.y.z.fpk）单独说一句：它们也在清理范围里
      (selfN ? ('其中 ' + selfN + ' 个是本应用自己的安装器（'
        + (r.self || []).join('、') + '）—— 会一并清掉，需要时能从本应用 Release 重新下载。') : ''),
      (r.keep_count ? ('会保留 ' + r.keep_count + ' 个当前版本的包（回滚/修复用，'
        + fmtSize(r.keep_size) + '）。要连它们一起清，可在设置里关掉「清理时保留当前版本」。') : ''),
      '位置：' + where,
      fgn ? ('同目录里另有 ' + fgn + ' 个不是本应用下载的安装包（'
        + fmtSize(r.foreign_size) + '），不会动。') : '',
      '只删本应用自己下载过的包；已装好的应用不受影响。',
      '删掉的包以后要用可以再从 GitHub Release 下载。',
    ].filter(Boolean),
    okText: '清理',
    danger: true,
  });
  if (!okClean) return;
  try {
    const d = await api('/api/clean', { method: 'POST', body: {} });
    const n = (d.deleted || []).length;
    const np = (d.parts_deleted || []).length;
    const ns = (d.self_deleted || []).length;
    const bad = (d.failed || []).length;
    let msg = '已清理 ' + (n + np + ns) + ' 项，释放 '
      + fmtSize((d.freed || 0) + (d.parts_freed || 0) + (d.self_freed || 0));
    if (bad) msg += '；' + bad + ' 个没删掉（' + d.failed[0].reason + '）';
    if ((d.kept || []).length) msg += '；保留当前版本 ' + d.kept.length + ' 个';
    toast(msg, bad ? '' : 'ok');
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
}

/* ── 任务列表（设置里） ── */
function renderTaskList(tasks) {
  const box = $('task-list');
  if (!box) return;
  box.textContent = '';
  const shown = tasks.filter(taskFresh).slice().reverse();
  if (!shown.length) {
    const e = document.createElement('div');
    e.className = 'set-hint';
    e.textContent = '（还没有任务）';
    box.appendChild(e);
    return;
  }
  shown.forEach((t) => {
    const row = document.createElement('div');
    row.className = 'taskrow';
    const head = document.createElement('div');
    head.className = 'taskhead';
    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = (t.display || t.appname) + ' · ' + actionLabel(t.action);
    head.appendChild(nm);
    const stt = document.createElement('span');
    stt.className = 'badge ' + (t.status === 'ok' ? 'ok'
      : (t.status === 'failed' ? 'err' : 'new'));
    stt.textContent = { queued: '排队中', running: '进行中', ok: '完成', failed: '失败' }[t.status] || t.status;
    head.appendChild(stt);
    row.appendChild(head);
    const msg = document.createElement('div');
    msg.className = 'taskmsg';
    msg.textContent = t.message || '';
    row.appendChild(msg);
    if (t.status === 'running' || t.status === 'queued') {
      row.appendChild(progressBlock(t, 'run'));
    }
    box.appendChild(row);
  });
}

/* ── 被管理的应用列表 ── */
function renderAppsList(list) {
  const box = $('apps-list');
  box.textContent = '';
  if (!list.length) {
    const e = document.createElement('div');
    e.className = 'set-hint';
    e.textContent = '（列表为空 —— 没有应用会被显示或操作）';
    box.appendChild(e);
    return;
  }
  list.forEach((it) => {
    const row = document.createElement('div');
    row.className = 'approw';

    // 左侧元数据主区：标题 + appname + 来源 + 标签
    const main = document.createElement('div');
    main.className = 'approw-main';

    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = it.display || it.appname || '(未识别)';
    main.appendChild(nm);

    if (it.appname) {
      const an = document.createElement('span');
      an.className = 'an';
      an.textContent = it.appname;
      main.appendChild(an);
    }

    const src = document.createElement('span');
    src.className = 'src';
    src.textContent = it.repo || it.url || '';
    src.title = src.textContent;
    main.appendChild(src);

    if (it.tag) main.appendChild(chipSmall('tag ' + it.tag));
    if (it.asset) main.appendChild(chipSmall('包 ' + it.asset));
    if (it.allow_prerelease) main.appendChild(chipSmall('含预发布'));

    row.appendChild(main);

    // 右侧操作按钮组：独立容器且 flex: none，绝对不参与挤压和溢出
    const ops = document.createElement('div');
    ops.className = 'approw-ops';

    // 换版本：把来源填回输入框并列出它的所有版本/安装包，供手动改钉
    const repin = document.createElement('button');
    repin.className = 'rm';
    repin.textContent = '换版本';
    repin.title = '改这个来源使用的版本 / 安装包';
    repin.addEventListener('click', async () => {
      const src = it.repo || it.url;
      $('app-add-input').value = src;
      picker.editing = true;
      try {
        await loadOptions(src);
        toast('选好新版本后点「保存」', 'ok');
      } catch (e) {
        toast(e.message, 'err');
      }
    });
    ops.appendChild(repin);

    const rm = document.createElement('button');
    rm.className = 'rm danger-hover';
    rm.textContent = '移除';
    rm.addEventListener('click', async () => {
      const who = it.display || it.appname || it.repo || it.url;
      if (!(await askDialog({
        title: '不再管理「' + who + '」',
        notes: ['只会把它从列表里去掉，不会卸载应用、也不会删任何文件。'],
        okText: '移除',
        danger: true,
      }))) return;
      try {
        await api('/api/apps/remove', { method: 'POST', body: { source: it.repo || it.url, appname: it.appname } });
        toast('已移除 ' + who, 'ok');
        await refresh();
        doCheck($('check-btn'));
      } catch (e) { toast(e.message, 'err'); }
    });
    ops.appendChild(rm);

    row.appendChild(ops);
    box.appendChild(row);
  });
}

function chipSmall(text) {
  const s = document.createElement('span');
  s.className = 'tagchip';
  s.textContent = text;
  return s;
}

/* ── 动作 ── */
/* 让「会等一会儿」的按钮有明确反馈：灰掉 + 转圈 + 顶栏写清在做什么。
   实测这几个走 GitHub 的接口要 0.7~1.4 秒，原来只把按钮灰掉、没有任何文字与动效 ——
   用户看到的就是「点了没反应，卡住了」。 */
function busyOn(btn, what) {
  if (btn) { btn.disabled = true; btn.classList.add('loading', 'spin'); }
  const lbl = $('last-check');
  if (lbl && what) lbl.textContent = what;
}

function busyOff(btn) {
  if (btn) { btn.classList.remove('loading', 'spin'); btn.disabled = false; }
  const lbl = $('last-check');
  if (lbl) {
    lbl.textContent = (state && state.last_check)
      ? ('上次检查 ' + fmtTime(state.last_check)) : '尚未检查';
  }
}

let lastStateSig = '';
async function refresh() {
  const s = await api('/api/state');
  const sig = JSON.stringify(s);
  state = s;
  // 状态一字未变 → 跳过整轮渲染。这是「界面卡卡的」的主要来源之一：
  // 空闲时每 10~15 秒都会把整个列表重画一遍，其实一个字节都没变。
  if (sig === lastStateSig) return true;
  lastStateSig = sig;
  render();
  return true;
}

async function doCheck(btn) {
  // ⚠️ 不要用 innerHTML 替换按钮内容 —— 手机端 CSS 把按钮里的 span 隐藏了
  // （#check-btn span{display:none}），一替换就会出现"图标没了、文字也被隐藏"的空按钮。
  // 正确做法：保留图标与文字，只让它转圈 + 禁用，并把状态写到顶栏的时间文字上。
  //
  // 关键改动：**不再 await 整轮检查**。串行检查 6 个应用实测 6.1 秒，原来前端一直等到
  // 全部跑完 —— 用户看到的就是「点一下卡六七秒、屏幕毫无变化」。现在服务端立刻返回、
  // 检查在后台并行跑，进度与结果都由轮询（检查中 1 秒一次）补上。
  btn.disabled = true;
  btn.classList.add('loading');
  try {
    const r = await api('/api/check', { method: 'POST', body: {} });
    if (r.apps) {                       // 节流命中：直接采用服务端返回的上次结果
      state.apps = r.apps;
      state.last_check = r.last_check;
    }
    if (r.checking) state.checking = r.checking;
    if (r.throttled) toast('刚检查过，直接给你上次结果', '');
    else if (r.started) toast('已开始检查，进度会实时刷新', '');
    await refresh();
  } catch (e) {
    toast(e.message, 'err');
  }
  // 按钮的禁用/转圈统一由 render() 按 state.checking.running 决定 ——
  // 两处都管就会打架（这里开了、render 关了，或者反过来，按钮状态会闪）
}


/* 动作 → 接口（显式写出来，便于静态校验；也避免拼路径出错） */
const ENDPOINT = {
  download: '/api/download',
  install: '/api/install',
  update: '/api/update',
};

/* ── 弹窗开关（带入场/退场动效）──
   为什么要统一入口：原来是 classList.remove/add('hidden') 直接切 —— 弹窗「啪」
   地出现、瞬间消失，观感很突兀（用户反馈）。这里卸载时先加 .closing 播退场动画，
   150ms 后再真正隐藏；重开同一弹窗时会取消未完成的关闭定时器，避免「刚打开又被
   上一次的定时器关掉」。 */
const _closeTimers = new WeakMap();
function openModal(el) {
  if (!el) return;
  const tm = _closeTimers.get(el);
  if (tm) { clearTimeout(tm); _closeTimers.delete(el); }
  el.classList.remove('closing');
  el.classList.remove('hidden');
}
function closeModal(el) {
  if (!el || el.classList.contains('hidden') || el.classList.contains('closing')) return;
  el.classList.add('closing');
  const tm = setTimeout(() => {
    el.classList.add('hidden');
    el.classList.remove('closing');
    _closeTimers.delete(el);
  }, 120);
  _closeTimers.set(el, tm);
}

/* 应用内确认弹窗 —— 替代浏览器原生 confirm。
   为什么自己做：原生弹窗是浏览器样式（方角、系统字体、不跟随应用深浅色），
   嵌在飞牛应用小窗里跟应用不是一套东西。
   返回 Promise<boolean>：确定 true、取消/Esc/点遮罩 false。
   o.selects：可选的下拉项。确认时会把 {id: 选中值} 交给 o.onOk（先于 resolve）。 */
function askDialog(opt) {
  const o = opt || {};
  return new Promise((resolve) => {
    const modal = $('confirm-modal');
    $('cf-title').textContent = o.title || '确认';
    const body = $('cf-body');
    body.textContent = '';
    (o.lines || []).forEach((t) => {
      const d = document.createElement('div');
      if (t === '') { d.className = 'cf-gap'; body.appendChild(d); return; }
      d.textContent = String(t);       // 一律纯文本：不用 innerHTML，避免注入
      body.appendChild(d);
    });
    (o.rows || []).forEach((r) => {
      const li = document.createElement('div');
      li.className = 'cf-list';
      r.forEach((pair) => {
        const line = document.createElement('div');
        line.className = 'cf-li cf-mono';
        const a = document.createElement('span'); a.textContent = String(pair[0]);
        const b = document.createElement('span'); b.textContent = String(pair[1]);
        line.appendChild(a); line.appendChild(b);
        li.appendChild(line);
      });
      body.appendChild(li);
    });
    /* 表单字段（安装参数 + 存储空间都用它）：text / password / select / radio /
       checkbox / switch / tips。规则校验（required/pattern/min/max）在这里就地做，
       不通过就不关窗。用原生控件：手机端会调起系统键盘/选择器，比自绘好点。 */
    const fieldEls = [];
    (o.fields || []).forEach((f) => {
      const type = f.type || 'text';
      if (type === 'tips') {
        const d = document.createElement('div');
        d.className = 'cf-dim';
        d.textContent = String(f.helpText || '');
        body.appendChild(d);
        return;
      }
      const wrap = document.createElement('div');
      wrap.className = 'cf-field';
      const lab = document.createElement('label');
      lab.className = 'cf-field-label';
      lab.textContent = String(f.label || f.id || '');
      let getVal;
      if (type === 'select') {
        const sel = document.createElement('select');
        (f.options || []).forEach((op) => {
          const oe = document.createElement('option');
          oe.value = String(op.value);
          oe.textContent = String(op.label);
          sel.appendChild(oe);
        });
        if (f.value !== undefined && f.value !== null) sel.value = String(f.value);
        wrap.appendChild(lab);
        wrap.appendChild(sel);
        getVal = () => sel.value;
      } else if (type === 'radio') {
        const box = document.createElement('div');
        box.className = 'cf-radio-box';
        const radios = [];
        (f.options || []).forEach((op, i) => {
          const rl = document.createElement('label');
          rl.className = 'cf-radio';
          const r = document.createElement('input');
          r.type = 'radio';
          r.name = 'cfr-' + f.id;
          r.value = String(op.value);
          if ((f.value !== undefined && String(f.value) === String(op.value))
              || (f.value === undefined && i === 0)) r.checked = true;
          rl.appendChild(r);
          rl.appendChild(document.createTextNode(' ' + String(op.label)));
          box.appendChild(rl);
          radios.push(r);
        });
        wrap.appendChild(lab);
        wrap.appendChild(box);
        getVal = () => {
          const hit = radios.find((r) => r.checked);
          return hit ? hit.value : '';
        };
      } else if (type === 'checkbox' || type === 'switch') {
        // 勾选类：label 就在勾选框旁边，不再单独占一行
        const rl = document.createElement('label');
        rl.className = 'cf-check';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = String(f.value || f.initValue || '') === 'true';
        rl.appendChild(cb);
        rl.appendChild(document.createTextNode(' ' + String(f.label || f.id || '')));
        wrap.appendChild(rl);
        getVal = () => (cb.checked ? 'true' : 'false');
      } else {
        const inp = document.createElement('input');
        inp.type = (type === 'password') ? 'password' : 'text';
        if (f.value !== undefined && f.value !== null) inp.value = String(f.value);
        else if (f.initValue !== undefined) inp.value = String(f.initValue);
        wrap.appendChild(lab);
        wrap.appendChild(inp);
        getVal = () => inp.value;
      }
      if (f.helpText) {
        const h = document.createElement('div');
        h.className = 'cf-field-help';
        h.textContent = String(f.helpText);
        wrap.appendChild(h);
      }
      const err = document.createElement('div');
      err.className = 'cf-field-err hidden';
      wrap.appendChild(err);
      body.appendChild(wrap);
      fieldEls.push({ id: f.id, rules: f.rules || [], getVal, err });
    });
    (o.notes || []).forEach((t) => {
      const d = document.createElement('div');
      d.className = 'cf-dim';
      d.textContent = String(t);
      body.appendChild(d);
    });
    const ok = $('cf-ok');
    ok.textContent = o.okText || '确定';
    ok.classList.toggle('danger', !!o.danger);
    ok.classList.toggle('primary', !o.danger);
    $('cf-cancel').textContent = o.cancelText || '取消';
    openModal(modal);
    if (ok.focus) ok.focus();

    /* 按字段自带的 rules 做校验：required / pattern / min/max。
       min/max 的含义按值自适应：全数字 → 数值范围（端口那类）；否则按字符长度
       （密码那类）—— 两边的真实用例分别是 1-65535 与 4-64。 */
    const checkField = (felt) => {
      const v = felt.getVal();
      for (const r of felt.rules) {
        const msg = r.message || '输入不合法';
        if (r.required && !String(v).trim()) { felt.err.textContent = msg; return false; }
        if (r.pattern) {
          let re = null;
          try { re = new RegExp(r.pattern); } catch (e) { re = null; }
          if (re && !re.test(String(v))) { felt.err.textContent = msg; return false; }
        }
        if (r.min !== undefined && r.min !== null && String(v) !== '') {
          const numeric = /^\d+$/.test(String(v));
          const nv = numeric ? parseInt(v, 10) : String(v).length;
          if (nv < Number(r.min)) { felt.err.textContent = msg; return false; }
        }
        if (r.max !== undefined && r.max !== null && String(v) !== '') {
          const numeric = /^\d+$/.test(String(v));
          const nv = numeric ? parseInt(v, 10) : String(v).length;
          if (nv > Number(r.max)) { felt.err.textContent = msg; return false; }
        }
      }
      return true;
    };
    const readValues = () => {
      const v = {};
      fieldEls.forEach((felt) => { v[felt.id] = felt.getVal(); });
      return v;
    };
    const onOk = () => {
      let okAll = true;
      fieldEls.forEach((felt) => {
        const good = checkField(felt);
        felt.err.classList.toggle('hidden', good);
        if (!good) okAll = false;
      });
      if (!okAll) return;              // 校验没过不关窗，让用户改
      ok.disabled = true;
      ok.classList.add('spin');
      // 选择值交给调用方（只有确认才给；取消时什么都不给）
      if (o.onOk) { try { o.onOk(readValues()); } catch (e) { /* 调用方自己兜 */ } }
      done(true);
    };
    const onNo = () => done(false);
    const onBg = (ev) => { if (ev.target === modal) done(false); };
    const onKey = (ev) => {
      if (ev.key === 'Escape') {
        if (ev.stopPropagation) ev.stopPropagation();
        done(false);
      } else if (ev.key === 'Enter' && document.activeElement !== $('cf-cancel')) {
        if (ev.stopPropagation) ev.stopPropagation();
        done(true);
      }
    };
    function done(v) {
      closeModal(modal);
      ok.classList.remove('spin');
      ok.disabled = false;
      ok.removeEventListener('click', onOk);
      $('cf-cancel').removeEventListener('click', onNo);
      modal.removeEventListener('click', onBg);
      document.removeEventListener('keydown', onKey);
      resolve(v);
    }
    ok.addEventListener('click', onOk);
    $('cf-cancel').addEventListener('click', onNo);
    modal.addEventListener('click', onBg);
    document.addEventListener('keydown', onKey);
  });
}

/* 三个动作的确认内容。改回对象形式，好让弹窗按「标题 / 正文 / 按钮」排布，
   而不是把一整段文字塞进原生 confirm 的纯文本里。 */
const CONFIRM = {
  download: (a) => ({
    title: '下载「' + (a.display || a.appname) + '」的安装包',
    rows: [[['版本', a.latest + '（' + fmtSize(a.size) + '）']]],
    notes: ['只下载到本地并校验 sha256，不会改动应用。'],
    okText: '下载',
  }),
  // downloaded 可能已被清理/目录换过而缺失：这里必须兜底，否则「重试」一点就抛
  // TypeError（点击处理器里没有 try/catch）→ 按钮毫无反应、也没有任何提示。
  install: (a) => ({
    title: '用已下载的安装包安装「' + (a.display || a.appname) + '」',
    rows: [[['版本', a.installed + ' → '
      + ((a.downloaded && a.downloaded.version) || a.latest || '?')]]],
    notes: ['安装过程中该应用会短暂重启，数据不会丢失。'],
    okText: '安装',
  }),
  update: (a) => ({
    title: (a.update_available ? '更新' : '安装') + '「' + (a.display || a.appname) + '」',
    rows: [[['版本', a.installed + ' → ' + a.latest]]],
    notes: [
      (a.downloaded && a.downloaded.complete)
        ? ('本地已经有校验过的 ' + a.downloaded.version + ' 安装包，直接安装，不重复下载。')
        : ('本地没有（或需要重取）安装包，会先下载 ' + a.latest + ' 并校验 sha256，然后安装。'),
      '过程中该应用会短暂重启，数据不会丢失。',
    ],
    okText: a.update_available ? '更新' : '安装',
  }),
};

async function doAction(app, action, label, triggerBtn) {
  if (triggerBtn) busyOn(triggerBtn, '正在准备…');
  let ok = false;
  try {
    ok = await askDialog(CONFIRM[action](app));
  } finally {
    if (triggerBtn) busyOff(triggerBtn);
  }
  if (!ok) return;
  try {
    const r = await api(ENDPOINT[action], { method: 'POST', body: { appname: app.appname } });
    toast('已加入后台队列（' + label + '），可以关掉页面');
    await refresh();
    startPoll();
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* 未安装应用的**完整安装**：选存储空间 + 填安装参数（端口/密钥等向导字段）→
   入队「安装」（后端：下载→校验→应用中心装好并拉起）。
   参数表从 /api/wizard 取：本地有包读本地包，没有就远端头窗/仓库源码/小包预览。 */
let _volumesCache = null;
let _volumesPromise = null;
async function getVolumes(force) {
  if (!force && _volumesCache) return _volumesCache;
  if (_volumesPromise) return _volumesPromise;
  _volumesPromise = api('/api/volumes').then((r) => {
    _volumesCache = { volumes: r.volumes || [], default: r.default };
    _volumesPromise = null;
    return _volumesCache;
  }).catch((e) => {
    _volumesPromise = null;
    throw e;
  });
  return _volumesPromise;
}

let _wizardCache = {};
async function getWizard(appname, force) {
  if (!force && _wizardCache[appname]) return _wizardCache[appname];
  const wz = await api('/api/wizard?appname=' + encodeURIComponent(appname));
  if (wz && !wz.defer) _wizardCache[appname] = wz;
  return wz;
}

let firstInstallBusy = false;      // 防双击：取参数表期间按钮置灰+转圈
async function doFirstInstall(app, pkgReady, triggerBtn) {
  if (firstInstallBusy) return;
  firstInstallBusy = true;
  if (triggerBtn) busyOn(triggerBtn, '正在准备安装…');
  try {
    return await _doFirstInstallJob(app, pkgReady);
  } finally {
    firstInstallBusy = false;
    if (triggerBtn) busyOff(triggerBtn);
  }
}

async function _doFirstInstallJob(app, pkgReady) {
  let vols = [];
  let defVol = null;
  let wz = null;
  try {
    const [volRes, wzRes] = await Promise.all([
      getVolumes(),
      getWizard(app.appname),
    ]);
    vols = (volRes && volRes.volumes) || [];
    defVol = volRes && volRes.default;
    wz = wzRes;
  } catch (e) {
    toast('准备安装向导失败：' + e.message, 'err');
    return;
  }
  if (!vols.length) {
    toast('系统里没有可用于安装的存储空间', 'err');
    return;
  }
  // 包太大且读不到参数表 → 先下载；下好后卡片按钮变「安装」，那时本地包在手，
  // 参数表一定读得到（本地包是权威来源），再填表和安装。
  if (wz && wz.defer) {
    const okDl = await askDialog({
      title: '先下载「' + (app.display || app.appname) + '」的安装包',
      rows: [[['版本', app.latest || '?'], ['大小', app.size ? fmtSize(app.size) : '—']]],
      notes: ['这个安装包比较大，先把它下载并校验。下好后回到卡片点「安装」，'
        + '如果该应用需要填参数（端口、密钥等），届时会显示表单。'],
      okText: '先下载',
    });
    if (!okDl) return;
    try {
      await api('/api/download', { method: 'POST', body: { appname: app.appname } });
      toast('已加入后台队列（下载），可以关掉页面');
      await refresh();
      startPoll();
    } catch (e) {
      toast(e.message, 'err');
    }
    return;
  }
  // 组装表单：向导字段（原顺序）+ 存储空间
  const fields = (wz && wz.fields ? wz.fields : []).map((x) => {
    if (x.type === 'tips') return { type: 'tips', helpText: x.helpText };
    return {
      type: x.type || 'text', id: x.field, label: x.label || x.field,
      value: x.initValue !== undefined ? x.initValue : '',
      helpText: x.helpText, options: x.options, rules: x.rules,
    };
  });
  fields.push({
    type: 'select', id: '__volume', label: '安装到',
    value: String(defVol != null ? defVol : vols[0].index),
    options: vols.map((v) => ({
      value: String(v.index),
      label: '存储空间 ' + v.index + '（剩余 ' + fmtSize(v.free) + '）'
        + (v.index === defVol ? ' · 默认' : ''),
    })),
  });
  const localReady = pkgReady || !!(wz && wz.local_ready);
  let chosen = null;
  let params = {};
  const okGo = await askDialog({
    title: (localReady ? '安装「' : '下载并安装「') + (app.display || app.appname) + '」',
    rows: [[['版本', (app.latest || '?') + (app.size ? '（' + fmtSize(app.size) + '）' : '')]]],
    fields,
    notes: [
      localReady ? '本地已有校验过的安装包，直接安装，不重复下载。'
                 : '会先下载安装包并校验 sha256，然后执行安装。',
      '由飞牛应用中心完成安装（约需片刻），装好后应用会自动启动；'
        + '数据保存在所选存储空间上。',
    ],
    okText: '安装',
    onOk: (vals) => {
      chosen = parseInt(vals.__volume, 10);
      params = {};
      Object.keys(vals).forEach((k) => {
        if (k !== '__volume') params[k] = vals[k];
      });
    },
  });
  if (!okGo) return;
  if (!chosen || isNaN(chosen)) { toast('没有选存储空间，已取消', ''); return; }
  try {
    await api('/api/install', {
      method: 'POST',
      body: { appname: app.appname, volume: chosen, params },
    });
    toast('已加入后台队列（安装到存储空间 ' + chosen + '），可以关掉页面');
    await refresh();
    startPoll();
  } catch (e) {
    toast(e.message, 'err');
  }
}

let idleCycles = 0;

/* ── 轮询：有任务时快轮询，空闲时慢轮询 ── */
function startPoll() {
  stopPoll();
  idleCycles = 0;
  const tick = async () => {
    if (typeof document !== 'undefined' && document.hidden) return;   // 后台不轮询
    try {
      await refresh();
      const live = activeTasks();
      const ck = (state && state.checking) || {};
      const justDone = ((state && state.tasks) || []).some((t) => {
        if (!t.finished_at) return false;
        const ts = Date.parse(t.finished_at);
        return !isNaN(ts) && (Date.now() - ts) < 20000;
      });
      // 动态自适应退避：
      // - 活跃期（有任务、检测中、刚结束）：1.5s；
      // - 刚进入空闲（前 2 分钟）：15s；
      // - 长期纯空闲（>2 分钟无操作）：拉长至 30s，彻底降温 CPU 与系统唤醒。
      let want = 1500;
      if (live.length || ck.running || justDone) {
        idleCycles = 0;
        want = 1500;
      } else {
        idleCycles++;
        want = idleCycles > 8 ? 30000 : 15000;
      }
      if (pollTimer && pollTimer.ms !== want) {
        clearInterval(pollTimer.id);
        pollTimer = { ms: want, id: setInterval(tick, want) };
      }
    } catch (e) { /* 网络抖动忽略，下一轮再试 */ }
  };
  pollTimer = { ms: 1500, id: setInterval(tick, 1500) };
}

function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer.id); pollTimer = null; }
}

/* 页面从后台回到前台：立刻补一次刷新（锁屏回来不该看到 15 秒前的数据） */
if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refresh().catch(() => {});
  });
}

/* ── 设置 ── */
function openSettings() {
  try { if (state) syncSettingsPanels(state); } catch (e) { /* 同步失败不影响打开 */ }
  openModal($('settings-modal'));
}
function closeSettings() { closeModal($('settings-modal')); }

function openAddApp() {
  openSettings();
  switchTab('apps');
  setTimeout(() => {
    const input = $('app-add-input');
    if (input) {
      input.focus();
      if (input.scrollIntoView) input.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, 60);
}

async function showLog() {
  const box = $('log-box');
  if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
  try {
    const r = await api('/api/log?lines=200');
    box.textContent = (r.lines || []).join('\n') || '（暂无日志）';
    box.classList.remove('hidden');
    box.scrollTop = box.scrollHeight;
  } catch (e) { toast(e.message, 'err'); }
}

/* ── 事件 ── */
$('add-btn').addEventListener('click', openAddApp);
$('check-btn').addEventListener('click', (ev) => doCheck(ev.currentTarget));
/* 排序切换：先在本地生效（界面立刻重排），再写服务端设置；失败回滚并提示。 */
function setSortMode(mode) {
  if (!state) return;
  const cur = (state.settings && state.settings.sort) || 'added';
  if (mode === cur) return;
  state.settings = state.settings || {};
  state.settings.sort = mode;
  render();
  api('/api/settings', { method: 'POST', body: { sort: mode } }).then((r) => {
    state.settings.sort = (r.settings && r.settings.sort) || mode;
    render();
    toast(mode === 'alpha' ? '已切换：按字母排序' : '已切换：按添加顺序', 'ok');
  }).catch((e) => {
    state.settings.sort = cur;
    render();
    toast('排序设置没保存成功：' + e.message, 'err');
  });
}

/* 视图过滤切换（全部 vs 可更新）：钉住持久化，不手动切换绝不变 */
function setFilterMode(mode) {
  if (!state) return;
  const cur = (state.settings && state.settings.filter) || 'all';
  if (mode === cur) return;
  state.settings = state.settings || {};
  state.settings.filter = mode;
  render();
  api('/api/settings', { method: 'POST', body: { filter: mode } }).then((r) => {
    state.settings.filter = (r.settings && r.settings.filter) || mode;
    render();
    toast(mode === 'update' ? '已切换：仅显示可更新应用' : '已切换：显示全部应用', 'ok');
  }).catch((e) => {
    state.settings.filter = cur;
    render();
    toast('视图设置没保存成功：' + e.message, 'err');
  });
}
$('view-all').addEventListener('click', () => setFilterMode('all'));
$('view-update').addEventListener('click', () => setFilterMode('update'));
$('sort-added').addEventListener('click', () => setSortMode('added'));
$('sort-alpha').addEventListener('click', () => setSortMode('alpha'));
$('settings-btn').addEventListener('click', openSettings);
$('settings-close').addEventListener('click', closeSettings);
$('settings-modal').addEventListener('click', (ev) => {
  if (ev.target === $('settings-modal')) closeSettings();
});
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') {
    // 确认弹窗开着时，Escape 只取消上层确认弹窗，不关闭下层设置面板
    const cf = $('confirm-modal');
    if (cf && !cf.classList.contains('hidden')) return;
    closeSettings();
  }
});
$('log-btn').addEventListener('click', showLog);

$('token-save').addEventListener('click', async () => {
  const val = $('token-input').value.trim();
  if (val && !(await askDialog({
    title: '保存这个 GitHub 访问令牌',
    notes: ['它只写入本机数据目录（权限 600），不会随应用包分发，页面上也只回显末四位。'],
    okText: '保存',
  }))) return;
  try {
    const r = await api('/api/token', { method: 'POST', body: { token: val } });
    $('token-input').value = '';
    toast(r.has_token ? '令牌已保存，正在重新检查…' : '令牌已清除', 'ok');
    await refresh();
    if (r.has_token) doCheck($('check-btn'));
  } catch (e) { toast(e.message, 'err'); }
});

$('dl-save').addEventListener('click', async () => {
  const val = $('dl-input').value.trim();
  try {
    const r = await api('/api/settings', { method: 'POST', body: { download_dir: val } });
    toast('下载目录已改为 ' + r.settings.download_dir, 'ok');
    await refresh();
    doCheck($('check-btn'));
  } catch (e) { toast(e.message, 'err'); }
});

$('dl-reset').addEventListener('click', async () => {
  try {
    const r = await api('/api/settings', { method: 'POST', body: { download_dir: state.default_download_dir } });
    $('dl-input').value = r.settings.download_dir;
    toast('已恢复默认下载目录', 'ok');
    await refresh();
    doCheck($('check-btn'));
  } catch (e) { toast(e.message, 'err'); }
});

/* ── 下载加速设置 ── */
$('mirror-select').addEventListener('change', syncMirrorInput);

$('speed-save').addEventListener('click', async () => {
  const body = {
    concurrency: parseInt($('conc-input').value, 10) || 4,
    mirror: currentMirror(),
  };
  try {
    const r = await api('/api/settings', { method: 'POST', body });
    $('conc-input').value = r.settings.concurrency;
    toast('已保存：' + r.settings.concurrency + ' 段并发 · '
      + (r.settings.mirror ? r.settings.mirror : '直连'), 'ok');
    await refresh();
  } catch (e) { toast(e.message, 'err'); }
});

/* ── 分页/汇总/列表 ── */
$('all-download').addEventListener('click', (ev) => doAll('download', ev.currentTarget));
/* 顶栏「一键更新」：与汇总条的「全部更新」同一条链路（含应用内确认弹窗）。 */
$('update-all-btn').addEventListener('click', (ev) => doAll('update', ev.currentTarget));

/* ── 安装包清理 ── */
$('clean-btn').addEventListener('click', (ev) => doClean(ev.currentTarget));
$('clean-now-btn').addEventListener('click', (ev) => doClean(ev.currentTarget));
$('clean-refresh').addEventListener('click', async () => {
  await refresh();
  toast('已刷新', '');
});
$('keep-current-input').addEventListener('change', async (ev) => {
  const v = ev.currentTarget.checked;
  try {
    const r = await api('/api/settings', { method: 'POST', body: { clean_keep_current: v } });
    toast(r.settings.clean_keep_current
      ? '清理时会保留当前版本的包（回滚/修复用）'
      : '清理时会连当前版本的包一起清掉', 'ok');
    await refresh();
  } catch (e) {
    ev.currentTarget.checked = !v;
    toast(e.message, 'err');
  }
});

$('auto-clean-input').addEventListener('change', async (ev) => {
  const v = ev.currentTarget.checked;
  try {
    const r = await api('/api/settings', { method: 'POST', body: { auto_clean: v } });
    toast(r.settings.auto_clean ? '已开启：安装成功后自动清理安装包' : '已关闭自动清理', 'ok');
    await refresh();
  } catch (e) {
    ev.currentTarget.checked = !v;      // 服务端拒绝就还原开关，别显示成已生效
    toast(e.message, 'err');
  }
});

/* ── 设置分页 ── */
function switchTab(name) {
  document.querySelectorAll('#settings-tabs .tab').forEach((b) => {
    b.classList.toggle('on', b.getAttribute('data-tab') === name);
  });
  document.querySelectorAll('.panes .pane').forEach((p) => {
    p.classList.toggle('on', p.getAttribute('data-pane') === name);
  });
  const card = document.querySelector('#settings-modal .modal-card');
  if (card) card.scrollTop = 0;
}

document.querySelectorAll('#settings-tabs .tab').forEach((b) => {
  b.addEventListener('click', () => switchTab(b.getAttribute('data-tab')));
});

/* ── 主题切换 ── */
document.querySelectorAll('#theme-seg button').forEach((b) => {
  b.addEventListener('click', async () => {
    const v = b.getAttribute('data-v');
    themePref = applyTheme(v);            // 先本地生效，界面立刻变
    try {
      const r = await api('/api/settings', { method: 'POST', body: { theme: v } });
      themePref = applyTheme(r.settings.theme);
    } catch (e) {
      toast(e.message, 'err');
    }
  });
});

/* ── 自动下载开关（改完立即保存） ── */
$('auto-dl-input').addEventListener('change', async (ev) => {
  const on = ev.currentTarget.checked;
  try {
    const r = await api('/api/settings', { method: 'POST', body: { auto_download: on } });
    toast(on ? '已开启：有新版本会自动下载（不会自动安装）' : '已关闭自动下载', 'ok');
    await refresh();
  } catch (e) {
    ev.currentTarget.checked = !on;
    toast(e.message, 'err');
  }
});

/* ── 任务列表 ── */
$('task-clear').addEventListener('click', async () => {
  try {
    await api('/api/tasks/clear', { method: 'POST' });
    await refresh();
    toast('已清理已完成的任务', 'ok');
  } catch (e) { toast(e.message, 'err'); }
});

/* ── 加速镜像选择器 ── */
function fillMirrorSelect(cur, presets) {
  const sel = $('mirror-select');
  if (!sel) return;
  if (sel.dataset.filled !== '1') {
    sel.textContent = '';
    presets.forEach((p) => {
      const o = document.createElement('option');
      o.value = p.prefix;
      o.textContent = p.label;
      sel.appendChild(o);
    });
    const custom = document.createElement('option');
    custom.value = '__custom__';
    custom.textContent = '自定义…';
    sel.appendChild(custom);
    sel.dataset.filled = '1';
  }
  // 每次都按服务端的值回填选中项 —— 否则在别处改过（或首次填自定义）会显示成旧的
  const known = presets.some((p) => p.prefix === cur);
  sel.value = known ? cur : '__custom__';
  if (!known && cur) syncInput('mirror-input', cur);
  syncMirrorInput();
}

function syncMirrorInput() {
  const sel = $('mirror-select');
  const box = $('mirror-custom');
  if (sel && box) box.classList.toggle('hidden', sel.value !== '__custom__');
}

function currentMirror() {
  const sel = $('mirror-select');
  if (!sel) return '';
  return sel.value === '__custom__' ? $('mirror-input').value.trim() : sel.value;
}

/* ── 添加 / 换版本：手动选版本与安装包 ── */

function assetLabel(a) {
  const parts = [a.name];
  if (a.size) parts.push(fmtSize(a.size));
  if (!a.sha256) parts.push('无 sha256');
  if (a.recommended) parts.push('★ 推荐');
  return parts.join(' · ');
}

function verLabel(r) {
  const bits = [r.tag || '(无 tag)'];
  if (r.published_at) bits.push(fmtTime(r.published_at).replace(/（.*/, ''));
  if (r.prerelease) bits.push('预发布');
  if (r.rolling) bits.push('滚动版');
  return bits.join(' · ');
}

function fillAssetSelect() {
  const vi = parseInt($('ver-select').value, 10) || 0;
  const rel = picker.options.releases[vi];
  const sel = $('asset-select');
  sel.textContent = '';
  (rel.assets || []).forEach((a, i) => {
    const o = document.createElement('option');
    o.value = String(i);
    o.textContent = assetLabel(a);
    sel.appendChild(o);
  });
  const rec = (rel.assets || []).findIndex((a) => a.recommended);
  sel.value = String(rec >= 0 ? rec : 0);
}

function showPicker() {
  const box = $('app-picker');
  const sel = $('ver-select');
  sel.textContent = '';
  picker.options.releases.forEach((r, i) => {
    const o = document.createElement('option');
    o.value = String(i);
    o.textContent = verLabel(r);
    sel.appendChild(o);
  });
  // 默认选「第一个不是预发布/滚动版的」，没有就选第一个
  let def = picker.options.releases.findIndex((r) => !r.prerelease && !r.rolling);
  if (def < 0) def = 0;
  sel.value = String(def);
  fillAssetSelect();
  box.classList.remove('hidden');
  $('app-preview').classList.add('hidden');
  $('app-confirm-btn').textContent = picker.editing ? '保存' : '添加';
}

async function loadOptions(source) {
  const opts = await api('/api/apps/options', { method: 'POST', body: { source } });
  picker.options = opts;
  picker.source = opts.source || source;
  showPicker();
  return opts;
}

function currentPick() {
  const vi = parseInt($('ver-select').value, 10) || 0;
  const rel = picker.options.releases[vi];
  const ai = parseInt($('asset-select').value, 10) || 0;
  const asset = (rel.assets || [])[ai];
  return { tag: rel.tag || '', asset: asset ? asset.name : '', release: rel, assetRow: asset };
}

$('app-add-btn').addEventListener('click', async () => {
  const raw = $('app-add-input').value.trim();
  if (!raw) { toast('请先填仓库地址或 fpk 直链', 'err'); return; }
  const btn = $('app-add-btn');
  busyOn(btn, '正在读取仓库…');
  picker.editing = false;
  try {
    await loadOptions(raw);
    toast('选好版本和安装包后点「添加」', 'ok');
  } catch (e) {
    $('app-picker').classList.add('hidden');
    toast(e.message, 'err');
  } finally {
    busyOff(btn);
  }
});

$('ver-select').addEventListener('change', fillAssetSelect);

$('app-preview-btn').addEventListener('click', async () => {
  const p = currentPick();
  const box = $('app-preview');
  box.classList.remove('hidden');
  box.textContent = '正在识别…';
  const pbtn = $('app-preview-btn');
  busyOn(pbtn, '正在识别安装包…');
  try {
    const r = await api('/api/apps/preview', {
      method: 'POST',
      body: { source: picker.source, tag: p.tag, asset: p.asset },
    });
    box.textContent = [
      '识别结果：',
      '  应用名    ' + r.appname,
      '  显示名    ' + (r.display_name || r.appname),
      '  版本      ' + r.version + (r.tag ? '（Release tag ' + r.tag + '）' : '')
        + (r.version_source === 'tag' ? '  ⚠ 版本取自 tag（包内 manifest 读不到）' : ''),
      '  安装包    ' + (r.asset_name || '—') + (r.size ? '（' + fmtSize(r.size) + '）' : ''),
      '  平台      ' + (r.platform || '未声明'),
      '  本机已装  ' + (r.installed ? r.installed_version : '未安装'),
      '  sha256    ' + (r.has_sha256 ? '有' : '没有（出于安全不会下载/安装）'),
      '',
      r.installed
        ? (r.update_available ? ('可以更新：' + r.installed_version + ' → ' + r.version)
                              : ('当前已是最新（' + r.installed_version + '）'))
        : ('本机未安装：添加后可在卡片里直接安装（自动下载、校验并由应用中心装好）'),
    ].join('\n');
  } catch (e) {
    box.textContent = '✕ ' + e.message;
  } finally {
    busyOff(pbtn);
  }
});

$('app-confirm-btn').addEventListener('click', async () => {
  const p = currentPick();
  const btn = $('app-confirm-btn');
  busyOn(btn, picker.editing ? '正在保存版本…' : '正在添加应用…');
  try {
    if (picker.editing) {
      await api('/api/apps/pin', {
        method: 'POST',
        body: { source: picker.source, tag: p.tag, asset: p.asset },
      });
      toast('已保存', 'ok');
    } else {
      const r = await api('/api/apps/add', {
        method: 'POST',
        body: { source: picker.source, tag: p.tag, asset: p.asset },
      });
      toast('已加入 ' + (r.appname || '')
        + (r.installed === false ? '（本机未安装：可在卡片里直接安装）' : ''), 'ok', 6500);
      $('app-add-input').value = '';
    }
    $('app-picker').classList.add('hidden');
    picker.editing = false;
    await refresh();
    doCheck($('check-btn'));
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    busyOff(btn);
  }
});

$('app-picker-cancel').addEventListener('click', () => {
  $('app-picker').classList.add('hidden');
  picker.editing = false;
});

$('app-add-input').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') $('app-add-btn').click();
});

$('apps-reset').addEventListener('click', async () => {
  if (!(await askDialog({
    title: '清空被管理应用列表',
    notes: ['会清空当前被管理应用列表（恢复为初始空列表），不会卸载已安装应用、也不会删文件。'],
    okText: '确认清空',
    danger: true,
  }))) return;
  try {
    await api('/api/apps/reset', { method: 'POST' });
    toast('已清空列表', 'ok');
    await refresh();
    doCheck($('check-btn'));
  } catch (e) { toast(e.message, 'err'); }
});

const searchEl = $('search-input');
if (searchEl) {
  searchEl.addEventListener('input', (ev) => {
    searchQuery = (ev.target.value || '').trim();
    render();
  });
}

const expBtn = $('apps-export');
if (expBtn) {
  expBtn.addEventListener('click', () => {
    const list = (state && state.whitelist) || [];
    const dataStr = 'data:text/json;charset=utf-8,' + encodeURIComponent(JSON.stringify({
      version: '1.0',
      exported_at: new Date().toISOString(),
      apps: list,
    }, null, 2));
    const a = document.createElement('a');
    a.setAttribute('href', dataStr);
    a.setAttribute('download', 'fpk-update-apps-backup.json');
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast('已导出应用清单备份文件', 'ok');
  });
}

const impBtn = $('apps-import-btn');
const impFile = $('apps-import-file');
if (impBtn && impFile) {
  impBtn.addEventListener('click', () => impFile.click());
  impFile.addEventListener('change', (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async (e) => {
      try {
        const text = e.target.result;
        const parsed = JSON.parse(text);
        const incoming = Array.isArray(parsed) ? parsed : (parsed && parsed.apps);
        if (!Array.isArray(incoming)) {
          throw new Error('导入的文件格式不正确（需包含应用数组）');
        }
        const r = await api('/api/apps/import', { method: 'POST', body: { apps: incoming, mode: 'merge' } });
        toast('导入完成：成功合并 ' + r.added + ' 个新应用（当前共 ' + r.total + ' 个）', 'ok');
        await refresh();
      } catch (err) {
        toast('导入失败：' + err.message, 'err');
      } finally {
        impFile.value = '';
      }
    };
    reader.readAsText(file);
  });
}

/* ── 本应用自身 ── */
$('sb-update').addEventListener('click', doSelfUpdate);

$('sb-check').addEventListener('click', async () => {
  // 只看自己：服务端会绕过 Release 列表的 15 秒缓存，刚发一版立刻就能看到
  try {
    await api('/api/self/check', { method: 'POST', body: {} });
    await refresh();
    startPoll();
  } catch (e) { toast(e.message, 'err'); }
});

$('self-repo-save').addEventListener('click', async () => {
  try {
    const r = await api('/api/settings', {
      method: 'POST', body: { self_repo: $('self-repo-input').value.trim() },
    });
    toast('自身更新来源已改为 ' + (r.settings.self_repo || '内置默认'), 'ok');
    await refresh();
    try { await api('/api/self/check', { method: 'POST', body: {} }); await refresh(); startPoll(); }
    catch (e) { /* 立刻重查失败不影响设置已保存 */ }
  } catch (e) { toast(e.message, 'err'); }
});

/* ── 启动 ── */
(async function boot() {
  try {
    await refresh();
    startPoll();
    const last = state.last_check ? new Date(state.last_check).getTime() : 0;
    if (!last || Date.now() - last > 5 * 60 * 1000) doCheck($('check-btn'));
  } catch (e) {
    toast('无法连接服务：' + e.message, 'err');
  }
})();
