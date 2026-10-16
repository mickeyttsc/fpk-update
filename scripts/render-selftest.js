/* 渲染自测：用最小 DOM 桩真跑 app.js 的 renderCard，检查结构 + 有无运行时错误。
   为什么值得做：浏览器工具在本机不可用，而"页面长得对不对"不能靠猜 ——
   至少要把 DOM 树打出来，确认（a）没抛异常（b）同一件事只出现一次。 */
const fs = require('fs');
const vm = require('vm');
const path = process.argv[2] || 'app/www/app.js';

function makeEl(tag) {
  const e = {
    tagName: (tag || 'div').toUpperCase(),
    className: '', textContent: '', value: '', checked: false,
    dataset: {}, style: {}, children: [], title: '', alt: '', src: '', loading: '',
    _attrs: {},
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      toggle(c, on) { if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); } else if (on) this._s.add(c); else this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {},
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return this._attrs[k] === undefined ? null : this._attrs[k]; },
  };
  return e;
}

const _els = {};
/* 只认 index.html 里真实存在的 id —— 往「HTML 里没有的元素」上写会返回 null 并立刻炸出来，
   这是最省事也最有效的一类回归守卫（拼错 id、改了 HTML 忘了改 JS）。 */
const htmlPath = require('path').join(require('path').dirname(path), 'index.html');
let htmlIds = [];
try {
  const html = fs.readFileSync(htmlPath, 'utf8');
  htmlIds = (html.match(/id="[A-Za-z0-9_-]+"/g) || []).map((m) => m.slice(4, -1));
} catch (e) {
  console.log('  （读不到 index.html，跳过 id 校验：' + htmlPath + '）');
}
const idSet = new Set(htmlIds);
const document = {
  createElement: makeEl,
  getElementById: (id) => {
    if (htmlIds.length && !idSet.has(id)) return null;
    return _els[id] || (_els[id] = makeEl('div'));
  },
  addEventListener() {},
  querySelector: () => makeEl('div'),
  querySelectorAll: () => [],
  documentElement: makeEl('html'),
};

const sandbox = {
  document,
  console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  fetch: () => new Promise(() => {}),   // boot() 会挂着，不影响我们直接调 renderCard
  location: { pathname: '/' },
  navigator: {},
  localStorage: {
    _m: {},
    getItem(k) { return this._m[k] === undefined ? null : this._m[k]; },
    setItem(k, v) { this._m[k] = String(v); },
    removeItem(k) { delete this._m[k]; },
  },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.window.matchMedia = () => ({
  matches: false,
  addEventListener() {},
  removeEventListener() {},
});

const src = fs.readFileSync(path, 'utf8')
  + '\n;globalThis.__renderCard = renderCard;'
  + '\n;globalThis.__setState = (s) => { state = s; };'
  + '\n;globalThis.__versionRow = versionRow;'
  + '\n;globalThis.__staticState = staticState;'
  + '\n;globalThis.__applyTheme = applyTheme;'
  + '\n;globalThis.__switchTab = switchTab;'
  + '\n;globalThis.__sortApps = sortApps;'
  + '\n;globalThis.__getTheme = () => themePref;'
  + '\n;globalThis.__render = render;'
  + '\n;globalThis.__els = (id) => document.getElementById(id);';

vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: path });

/* 主题自测：设置值 → 写进 <html data-theme> 的生效值应当正确 */
function themeCheck() {
  let bad = 0;
  const cases = [
    ['light', 'light'], ['dark', 'dark'], ['auto', 'light'],   // 桩里系统偏好是浅色
    ['乱填', 'light'],                                          // 非法值应退回跟随系统
  ];
  for (const [input, want] of cases) {
    sandbox.__applyTheme(input);
    const got = sandbox.document.documentElement.getAttribute
      ? sandbox.document.documentElement.getAttribute('data-theme') : null;
    const ok = got === want;
    if (!ok) bad++;
    console.log(`  ${ok ? '✔' : '✘'} 主题 ${JSON.stringify(input)} -> data-theme=${got}（期望 ${want}）`);
  }
  return bad;
}

function dump(el, indent = 0) {
  const pad = '  '.repeat(indent);
  const cls = el.className ? '.' + String(el.className).split(/\s+/).join('.') : '';
  let line = `${pad}${el.tagName.toLowerCase()}${cls}`;
  if (el.textContent) line += `  "${el.textContent}"`;
  const out = [line];
  (el.children || []).forEach((c) => out.push(dump(c, indent + 1)));
  return out.join('\n');
}

function countText(node, needle) {
  let n = 0;
  const walk = (e) => {
    if (String(e.textContent || '').includes(needle)) n++;
    (e.children || []).forEach(walk);
  };
  walk(node);
  return n;
}

const cases = [
  ['已是最新 + 本地有包', {
    appname: 'FanControlServer', display: 'Fan Control Server', has_icon: true,
    repo: 'guan-ry/FanControlServerApp', installed: '1.3.7.1', latest: '1.3.7.1',
    update_available: false, relation: 'same', size: 6354481,
    published_at: '2026-10-15T12:00:00Z', error: null,
    downloaded: { version: '1.3.7.1', size: 6354481, complete: true },
  }],
  ['有新版本', {
    appname: 'cliproxyapi', display: 'CLIProxyAPI', has_icon: true,
    repo: 'mickeyttsc/cliproxyapi-fpk', installed: '8.0.31-1', latest: '8.0.32-1',
    update_available: true, relation: 'newer', size: 24427225,
    published_at: '2026-10-16T04:05:00Z', error: null,
    downloaded: null,
  }],
  ['有新版本 + 已下好新版', {
    appname: 'cliproxyapi', display: 'CLIProxyAPI', has_icon: true,
    repo: 'mickeyttsc/cliproxyapi-fpk', installed: '8.0.31-1', latest: '8.0.32-1',
    update_available: true, relation: 'newer', size: 24427225,
    published_at: '2026-10-16T04:05:00Z', error: null,
    downloaded: { version: '8.0.32-1', size: 24427225, complete: true },
  }],
  ['本地包不完整', {
    appname: 'hermes-studio', display: 'Ekko Studio', has_icon: false,
    repo: 'mickeyttsc/ekko-studio-fpk', installed: '0.7.36-1', latest: '0.7.36-1',
    update_available: false, relation: 'same', size: 357248895,
    published_at: '2026-10-15T14:49:44Z', error: null,
    downloaded: { version: '0.7.36-1', size: 357240506, complete: false },
  }],
  // 自审修的：有新版、但没有 sha256 ⇒ 不能自动更新。以前会误显示「已是最新」。
  ['有新版但不可自动更新', {
    appname: 'sometool', display: 'Some Tool', has_icon: false,
    repo: '', url: 'https://example.com/sometool-1.2.0.fpk',
    installed: '1.1.0', latest: '1.2.0',
    update_available: false, relation: 'newer', size: 1024,
    published_at: null,
    error: '该来源没有 sha256 摘要，出于安全不会下载/安装',
    downloaded: null,
  }],
];

let bad = 0;

console.log('────────────────────────────────────────');
console.log('▸ 主题生效值');
bad += themeCheck();

console.log('────────────────────────────────────────');
console.log('▸ 分页切换');
try {
  sandbox.__switchTab('dl');
  console.log('  ✔ switchTab("dl") 未抛异常');
} catch (e) {
  console.log('  ✘ switchTab 抛异常 -> ' + e.message);
  bad++;
}

console.log('────────────────────────────────────────');
console.log('▸ 排序（对标应用商店：正在动 → 有新版本 → 正常）');
try {
  sandbox.__setState({ tasks: [{ id: 'x', appname: 'zzz', action: 'download', status: 'running' }] });
  const input = [
    { appname: 'aaa', display: 'AAA', update_available: false, latest: '1.0', error: null },
    { appname: 'mmm', display: 'MMM', update_available: true, latest: '2.0', error: null },
    { appname: 'zzz', display: 'ZZZ', update_available: false, latest: '1.0', error: null },
    { appname: 'bbb', display: 'BBB', update_available: true, latest: '2.0', error: null },
  ];
  const got = sandbox.__sortApps(input).map((a) => a.appname);
  const want = ['zzz', 'bbb', 'mmm', 'aaa'];   // zzz 在跑；bbb/mmm 有更新（按名）；aaa 正常
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log('  ' + (ok ? '✔' : '✘') + ' 得到 ' + JSON.stringify(got) + (ok ? '' : '，期望 ' + JSON.stringify(want)));
  if (!ok) bad++;
} catch (e) {
  console.log('  ✘ sortApps 抛异常 -> ' + e.message);
  bad++;
}

for (const [title, app] of cases) {
  sandbox.__setState({ tasks: [] });
  let card;
  try {
    card = sandbox.__renderCard(app);
  } catch (e) {
    console.log(`✘ ${title}：渲染抛异常 -> ${e.message}`);
    bad++;
    continue;
  }
  console.log('────────────────────────────────────────');
  console.log('▸ ' + title);
  console.log(dump(card, 1));
  // 「一件事只说一遍」检查：版本号在"已装/最新"里最多各出现一次；
  // 且"已下载"这个事实不应同时出现在结果条与静态状态里
  const vCount = countText(card, app.installed);
  console.log(`  · 已装版本 "${app.installed}" 出现 ${vCount} 次（相同版本时应为 1）`);
  if (app.installed === app.latest && vCount > 1) { console.log('  ✘ 相同版本号重复显示'); bad++; }

  // 徽章正确性：有新版但不可自动更新时，绝不能显示"已是最新"
  const badgeText = (card.children.find((c) => c.className === 'c-head') || { children: [] })
    .children.map((c) => c.textContent).join(' ');
  if (app.relation === 'newer' && !app.update_available) {
    const ok = /不能自动更新/.test(badgeText) && !/已是最新/.test(badgeText);
    console.log(`  · 徽章="${badgeText}" ${ok ? '✔ 正确（不再误报已是最新）' : '✘ 误报'}`);
    if (!ok) bad++;
  }
  if (app.relation === 'same' && app.latest) {
    const ok = /已是最新/.test(badgeText);
    if (!ok) { console.log(`  ✘ 已是最新却显示="${badgeText}"`); bad++; }
  }
}

/* 更新说明：有 notes 时必须渲染出可展开块，且正文在里面 */
console.log('────────────────────────────────────────');
console.log('▸ 更新说明（changelog）');
try {
  sandbox.__setState({ tasks: [] });
  const card = sandbox.__renderCard({
    appname: 'cliproxyapi', display: 'CLIProxyAPI', has_icon: true,
    repo: 'mickeyttsc/cliproxyapi-fpk', installed: '8.0.31-1', latest: '8.0.32-1',
    update_available: true, relation: 'newer', size: 24427225, published_at: null,
    error: null, downloaded: null, notes: '跟随上游 v8.0.32；修了若干问题。',
  });
  const box = card.children.find((c) => String(c.className).includes('c-notes'));
  const hasText = countText(box || card, '跟随上游 v8.0.32') > 0;
  const ok = !!box && hasText;
  console.log('  ' + (ok ? '✔' : '✘') + ' 展开块=' + (box ? box.tagName : '无') + '，含正文=' + hasText);
  if (!ok) bad++;
} catch (e) {
  console.log('  ✘ 抛异常 -> ' + e.message);
  bad++;
}

/* 失败结果条：必须有「重试」按钮 */
console.log('────────────────────────────────────────');
console.log('▸ 失败可重试');
try {
  sandbox.__setState({ tasks: [{ id: 't9', appname: 'wildwork', action: 'update',
    status: 'failed', message: '下载失败：连接超时', finished_at: new Date().toISOString() }] });
  const card = sandbox.__renderCard({
    appname: 'wildwork', display: 'Wild Work', has_icon: true,
    repo: 'mickeyttsc/wild-work-fpk', installed: '2.8.0-1', latest: '2.8.1-1',
    update_available: true, relation: 'newer', size: 8367502, published_at: null,
    error: null, downloaded: null, notes: '',
  });
  let hasRetry = false;
  const walk = (e) => {
    if (String(e.className).includes('mini') && e.textContent === '重试') hasRetry = true;
    (e.children || []).forEach(walk);
  };
  walk(card);
  console.log('  ' + (hasRetry ? '✔' : '✘') + ' 失败条上有「重试」按钮');
  if (!hasRetry) bad++;
} catch (e) {
  console.log('  ✘ 抛异常 -> ' + e.message);
  bad++;
}

/* 安装包清理：设置里的状态与开关必须跟服务端一致 */
console.log('────────────────────────────────────────');
console.log('▸ 安装包清理（设置页）');
try {
  sandbox.__setState({
    apps: [], tasks: [], last_check: null, has_token: true,
    settings: { download_dir: '/vol2/x', concurrency: 4, mirror: '', auto_download: false,
      auto_clean: true, theme: 'auto' },
    packages: { count: 3, size: 62914560 },
    whitelist: [], mirror_presets: [], default_download_dir: '/vol2/x',
  });
  sandbox.__render();
  const onTxt = sandbox.__els('pkg-state').textContent;
  const onChk = sandbox.__els('auto-clean-input').checked;
  const okOn = onChk === true && onTxt.includes('3') && onTxt.includes('60');
  console.log(`  ${okOn ? '✔' : '✘'} 有包时显示数量+体积、开关为开  -> "${onTxt}" checked=${onChk}`);
  if (!okOn) bad++;

  sandbox.__setState({
    apps: [], tasks: [], last_check: null, has_token: true,
    settings: { download_dir: '/vol2/x', concurrency: 4, mirror: '', auto_download: false,
      auto_clean: false, theme: 'auto' },
    packages: { count: 0, size: 0 },
    whitelist: [], mirror_presets: [], default_download_dir: '/vol2/x',
  });
  sandbox.__render();
  const offTxt = sandbox.__els('pkg-state').textContent;
  const offChk = sandbox.__els('auto-clean-input').checked;
  const okOff = offChk === false && offTxt.includes('无');
  console.log(`  ${okOff ? '✔' : '✘'} 无包时如实说「无」、开关随服务端为关  -> "${offTxt}" checked=${offChk}`);
  if (!okOff) bad++;
} catch (e) {
  console.log('  ✘ 抛异常 -> ' + e.message);
  bad++;
}

console.log('────────────────────────────────────────');
console.log(bad === 0 ? '✅ 渲染自测通过（无异常、无重复）' : `✘ 有 ${bad} 处问题`);
process.exit(bad === 0 ? 0 : 1);
