# FPK更新 · 维护文档

> **一句话定位：一个 GitHub 上 fpk 的下载更新器。** 不生产包、不首次安装、不自动安装。
> 面向维护者（构建 / 门禁 / 排障）；用户向说明见 [README.md](README.md)。

## 版本号规则

两种形态都合法：

| 形态 | 用途 | 例 |
|---|---|---|
| `X.Y.Z` | **正式版** | `1.0.0` |
| `X.Y.Z-N` | 同一功能版本的第 N 次封装/文案迭代 | `1.0.0-2` |

飞牛生态里两种都存在（自建三仓用 `-N`，第三方如 CleanFnOS 用纯 semver），安装器两种都收
（实测：纯 `1.0.0` 能正常装在 `1.0.0-15` 之上，不会被当成降级拒绝）。
功能变化抬前三位；只有封装/文案改动时用 `-N` 递增。构建与门禁都接受这两种形态。

## 本地构建与验证

```bash
bash scripts/build-fpk.sh dist                 # 构建 + 跑全部门禁，产物在 dist/
bash scripts/verify-fpk-gates.sh <fpk>         # 只跑门禁（CI 与本机共用同一份）
python3 scripts/verify-server-safety.py app/bin/server.py   # 只跑能力边界检查
python3 scripts/make-icons.py .                # 重新生成图标（可复现）
```

依赖：系统自带 `python3`（只用标准库）、`fnpack`（`/usr/local/bin/fnpack`）、`tar`。
**不下载任何外部载荷**，所以构建是离线、可复现的（图标由脚本生成，二进制素材不入库）。

## 安装与升级

- 全新安装：应用中心 → 手动安装 → 选 fpk
- ⚠️ **升级不能用 `appcenter-cli install-fpk`**：它对已安装应用直接短路
  （`[Info]Application [x] is installed.`，退出码还是 0），什么都不做。
- ⛔ **绝不能改用 `appcenter-cli install-local`**：那是「卸载再安装」，在 fnOS 上
  重装步骤会失败并把应用连同 `@appdata` 一起抹掉（见 conversun/fnos-store 的
  `internal/platform/upgrade.go` 实测记录）。
- 升级走应用中心守护进程 RPC（与界面点「更新」同一条路）：

```bash
bash scripts/upgrade-via-rpc.sh <appname> <fpk路径> [期望的当前版本]
```

该脚本会核对「版本已变 + 数据目录指纹未变」，两者都满足才算通过。

## 守护进程 RPC 速查（fnOS 1.2.0800 实测）

socket：`/var/run/com.trim.app.center.sock`（`root:root`，组 `OfficialAppUsers` 有 rw；
第三方应用用户在 `AppUsers` 组，**没有**权限 → 所以本应用必须 `run-as: root`）。

| 步骤 | 接口 | 要点 |
|---|---|---|
| 1 暂存 | `POST /rpc/v1/download/task` `{packageSourceType:"file", path}` | 返回 `downloadTaskId` |
| 2 暂存状态 | `POST /rpc/v1/download/status` `{downloadTaskId}` | `status`: 1=进行中 **2=成功**；成功后有 `appName`/`version`/`packageType` |
| 3 预检 | `POST /rpc/v1/update/info` `{appName, updateVersion, packageType, language}` | 取 `data.wizardInfo.installedVolumeID`；**取不到就中止，绝不猜卷** |
| 4 提交 | `POST /rpc/v1/update/task` `{appName, updateVersion, packageType, systemParameters:{agreedToProtocol:true, installVolumeID, dataVolumeId, immediateStart:false}, customParameters:[], language:"zh-CN"}` | 返回 `taskId` |
| 5 轮询 | `POST /rpc/v1/common/status` `{taskId}` | `status`: 1=进行中 **2=成功** **5=守护进程已不再持有该任务（不代表失败！用最终版本判定）** |

其它已确认的事实：

- 不先暂存就调 `update/info` → `code 10100`（包不存在）
- 参数名写错（如 `paramKey`/`paramValue`）→ `code 10030`
- 升级前后服务端日志是 `class=upgrade` + `flag=upgrade_success`（就地替换，不是卸载重装）
- 暂存会把包复制到 `/vol2/appcenter-downloads/<name>-<ver>-tpk/`，那是应用中心自己的
  暂存区，不要手动删（升级成功后会自然更新）

## 构建期门禁（全绿才交付）

`scripts/verify-fpk-gates.sh` 覆盖：

1. 外层结构 + 第一条必须是 `manifest`（安装器依赖顺序）
2. `manifest.checksum` == `md5(app.tgz)` == manifest 的 `checksum` 字段
3. 内层结构、首条无前缀、不含构建机路径、不混入 `scripts/`
4. `cmd/*` 可执行位
5. manifest 字段完整/无重复、`appname`/`distributor`/`service_port`/`ctl_stop` 断言、
   桌面入口端口与 `service_port` 一致
6. **`privilege` 必须是 `run-as: root`**（否则连不上升级通道）
7. **能力边界（AST 级，`verify-server-safety.py`）**：所有 `subprocess` 调用只允许
   `appcenter-cli status/start`；所有 `daemon_call` 路由只允许上表那 5 条。
   用 AST 而不是 grep —— 因为代码里在文档字符串中**警告**不要用 `install-local`，
   grep 会把这个警告本身当成违规。
8. `bin/server.py` 语法、`config/apps.json` 合法性
9. 零隐私 / 零机器绑定：产物内不得出现 IP 字面量、机器相关域名、个人路径、令牌字面量；
   前端不得引用任何外部 URL（离线可用）
10. 图标四个槽位均为 512×512 PNG，且**与 `scripts/make-icons.py` 的生成结果逐字节一致**、
    四个槽位彼此相同、页头标志引用共享文件（不内联手写）
11. 三个动作（下载/安装/下载并更新）与设置接口必须都在 —— 少一个即判功能回退；
    产物里不得残留登录/密码相关代码（本应用为无密码设计）
12. 清理能力（`verify-clean-safety.py`，真跑函数而非 grep）：台账之外的文件一个都不许删、
    同名外来包不许删、符号链接/非 fpk/目录外/穿越路径/占用中逐条拒绝、
    装后自动清理的开关与核验逻辑
13. 运行时健壮性（`verify-runtime-recovery.py`，真跑函数 + 本地起 HTTP 桩）：
    残留任务自愈（且必须接在启动流程里、在 worker 之前）、5xx 会重试而 404 不重试、
    「读不到 Release」的三种情况分别给出可操作的提示、指定附件对不上时不静默换包、
    下载残留按闲置时间判定并清理、**并发读改写的互斥探针**（没锁必然被抓到）、
    save_json 并发写临时名不撞
14. 接口延迟（`verify-endpoint-latency.py`）：18 个接口逐个 < 200 ms；检查类接口立刻返回；图标可缓存
15. 门禁自身不许空转（`verify-gates-reverse.py`）：逐条破坏上述修复点，确认每条都被拦住
    （9 条破坏全绿才算过；这一步放在所有断言之前，因为门禁逻辑坏掉后别的断言都可能是假绿）

## CI 与发布（GitHub Actions）

仓库：<https://github.com/mickeyttsc/fpk-update>（本仓库自己就是源码，没有上游载荷）

- 打 tag `v<manifest 版本>` → `.github/workflows/build-fpk.yml` 构建 → 建 Release 并附 fpk
- workflow 第一步是 `scripts/scan-secrets.sh`：公开仓库，令牌/私钥/内网 IP/个人域名/
  个人存储路径（`/volN/<uid>/`）进了历史就撤不回来，命中直接 fail
- 构建前会断言 **tag 与 manifest 里的 version 一致**（否则会出现「Release 叫 v1.0.0-15、
  包里写 1.0.0-14」这种对不上的产物）
- CI 上没有 fnpack → 构建脚本默认走 tar+gzip，**与本机同一条路径**（见下）
- 同一版本要重发时：**先删 Release + tag 再重打**（同一 tag 同名不同字节会让回滚包失去意义）

### 可复现性

载荷（`app.tgz` 解压后的 tar）在 CI 与本机**逐字节一致** —— 条目顺序、内容、元数据全同。
整包的 sha256 仍会因 **gzip 包壳**随环境不同而不同（gzip/zlib 版本差异），这不影响功能：
安装器读的是包内自洽的 `manifest.checksum`，应用校验的是「本地文件 vs Release 附件摘要」，
两者都只依赖同一份文件自身。

要让 `app.tgz` 可复现，条目顺序必须显式确定（见下面的坑）。

## 性能（「点一下就卡一秒」是一类问题，要按类防）

原则：**前端任何一次显示都必须快**，慢的活交给后台 + 给进度。实测过的问题与对策：

| 症状 | 根因（实测） | 对策 |
|---|---|---|
| 点「检查更新」卡 6 秒 | 服务端**同步**串行查 6 个应用，前端 await 到底、零反馈 | 各应用**并行** + 请求立刻返回 + 顶栏「正在检查 x/y」 |
| 点「获取版本/预览/添加」各卡 1 秒 | 每一步都重新打 GitHub（0.7~1.4 秒） | Release 列表 15 秒缓存；`获取版本` 时**后台预取** manifest，下一步预览直接命中 |
| 重复添加要等 0.9 秒才说「已在列表」 | 先联网识别、再查重 | **先本地查重**（亚毫秒）再联网 |
| 钉版本等 0.7 秒 | 内容没变也联网校验 | 只有**真的改了**才校验 |
| 页面持续抖、流量莫名大 | 每轮轮询 `cards.textContent=''` **全量重建卡片** → 每次重建都新建 `<img>` → 重拉图标（6 个/轮回） | 卡片**差分渲染**（签名没变复用节点）+ 图标加 `max-age`/`ETag`（URL 带 `?v=<已装版本>` 自动换新） |
| 按钮点了没反应 | 只 `disabled`，没有转圈/文字 | `busyOn()`：转圈（文字按钮用 `.spin::before`）+ 顶栏写清在做什么 |

- **门禁**：`scripts/verify-endpoint-latency.py` 起一个真的 HTTP 服务（网络调用换瞬时桩），
  对前端会调的**每个**接口做 5 次取最坏值，要求 < 200 ms；并断言「检查类接口立刻返回」、
  「图标带可缓存头」。想再加接口时把用例补进 `CASES`，别只靠人眼。
- **量之前别猜**：/api/state 只有 2 ms（不是它慢），慢的是那四个同步打 GitHub 的接口。
  先量再改，否则会去优化错的地方。

## 已知坑（都是实测换来的）

- **落盘的任务队列会留下永久卡死的「进行中」**（实测 2026-10-16）：tasks.json 里一条
  `status=running` 的任务，在服务重启后**新 worker 只认 queued，永远不会再碰它** →
  入队去重把它当「同一动作已在跑」，新请求被直接吃掉（实测再点下载返回的还是那条旧任务 id）；
  界面 `busy = running` 让该应用按钮永久禁用；而界面唯一的「清理已完成的任务」
  **明确保留 running 任务** → 用户自救不了。修法：启动时（起 worker **之前**）把残留的
  running 一律标为中断。这条必须真跑验证，见 `scripts/verify-runtime-recovery.py`。
- **不要把「上游读不到东西」报成「仓库没有」**（实测 2026-10-16）：GitHub 瞬时返回空列表时，
  `find_release` 返回空 → 界面报「仓库没有可用的 Release（含 .fpk 附件）」，几秒后同样调用正常。
  这条**误导性错误还会被写进 last-check 缓存**，一直显示到下次检测。修法：①请求层对 5xx/429/
  超时/坏 JSON 自动重试（4xx 直接抛，重试没意义）；②错误信息按「列表为空 / 无 .fpk 附件 /
  指定 tag 不存在」分开说，并给出「点检查更新重试」的指引。
- **用户填的 tag / 附件名必须校验后再生效**（实测 2026-10-16）：`/api/apps/pin` 原来不校验，
  钉一个不存在的 tag 会返回成功并写进 apps.json → 之后每次检测都失败，而错误看起来像仓库的问题
  （我自己的 QA 就因此把配置写坏了一次）。同类问题：`_pick_asset` 在 pattern 匹配不到时
  **静默回退成自动挑选** —— 可能挑到另一个架构、或另一个入口形态的包，用户以为钉的是 A、
  实际装的是 B。修法：pin 前 `find_release(want_tag=…)` 校验，附件用 `strict=True`。
- **下载残留 `<包名>.fpk.partN` 会永久留下**：目标文件名带版本号，换一个版本就是新文件名，
  旧分片再没人管；而清理只认 `.fpk` 台账 → 一辈子清不掉。修法：按**闲置时间**（>1 小时）
  判定孤儿并纳入一键清理。用闲置时间而不是「有没有任务在跑」：进行中的下载在持续写自己的
  分片、mtime 必然很新，所以绝不会误删正在下的分包。
- **`save_json` 的临时名只用了 pid**：同一进程两个线程同时写同一个文件会撞名（互相 rename），
  可能导致内容交错或整条更新丢失。改成 `<pid>.<线程号>.<单调纳秒>`。
- **设置 / 台账的读改写没加锁**：两个并发请求各自读到旧值再写回，后写的覆盖前一次
  （用户以为改好了其实没生效）。`save_settings` / `record_package` / `forget_package`
  整体加锁（`_lock` 用 RLock，这些操作彼此会调用）。
- **`tar tzf x.tgz | head -1` 会让构建静默死掉**（实测 2026-10-16）：`head` 读完一行就退出，
  `tar` 收到 SIGPIPE，在 `set -o pipefail` 下整个脚本以退出码 **141** 结束、
  产物半个字都没打出来。改用 `sed -n '1p'`（会把输入读完）。
- **tar 递归目录用的是文件系统 readdir 顺序**（实测 2026-10-16）：只固定顶层名字不够 ——
  同一个源码在 CI 与本机打出的 `app.tgz` **内容全同、条目顺序不同、字节不同**。
  正确做法：`find . -mindepth 1 -printf '%P\0' | LC_ALL=C sort -z` 出整棵树清单，
  再用 `--no-recursion -T 清单` 让 tar 严格照单执行。
- **`-C` 与 `-T` 是位置敏感参数**：`-C` 必须写在 `-T` 之前，否则它不生效，tar 会警告
  `has no effect` 并按当前目录找文件，结果全部 `Cannot stat`。
- **`ls` 的排序随 locale 变**：构建脚本里凡是会影响产物字节的顺序，都要 `LC_ALL=C` 钉住。
- **下载目录往往就是用户的归档目录**（实测 2026-10-16）：那里可能躺着 357MB 的 Ekko Studio
  回滚包，而它在 GitHub 上的附件已被重传过，删掉就再也拿不回逐字节相同的一份。
  所以「清理安装包」**只能清本应用自己下载过的**（下载完记台账），
  同名但来源不明的文件一个都不许删 —— 宁可少清，不可误删。
- **自动清理必须等核验通过**：装完的版本确实变成目标版本、应用目录还在，才删那个包；
  否则它此刻是回滚包。失败路径一律保留。
- **日志重复两遍**：`log()` 若同时写文件又 print，而 `cmd/main` 已把 stdout 重定向到
  同一个文件，每行会出现两次。只 print，不要自己写文件。
- **不要靠替换按钮的 innerHTML 表达"进行中"**（实测 2026-10-16 踩过）：手机端 CSS 有
  `#check-btn span{display:none}`，一替换就成了"图标被换掉、新文字又被隐藏"的**空按钮**。
  正确做法是保留图标与文字，加 `.loading` 让图标转圈 + 禁用，并把状态写到顶栏文字上。
  门禁已加断言：`app.js` 里出现 `btn.innerHTML` 直接构建失败。
- **主题暗色变量必须写两处**：`@media(prefers-color-scheme:dark){:root:not([data-theme])}`
  给"跟随系统 + 防无 JS 闪白"，`:root[data-theme="dark"]` 给"手动强制"。
  CSS 变量没法复用整块，只能复制 → 会漂。`scripts/verify-theme-css.py` 把它钉住
  （已用"只改一处"实测能抓到）。两处主题共用的变量（`--accent`/`--radius` 等）
  只写 `:root` 一次，不必进暗色块。
- **设置项多了就要分页**：一长条滚动在手机上没人看得完。分页后每个 pane 里的
  `h4` 自带分隔线，别再叠 `.set-block` 的 border（会出现双线）。
- **队列要串行 + 去重**：并发下载几个大包会互相抢带宽、还容易把磁盘打满；
  同一个应用的同一动作重复点会堆一串，必须在入队时合并。
- **分段下载必须先探测 Range**：先发 `Range: bytes=0-0`，只有返回 206 且能读到总长度才分段。
  部分 CDN 直接回 **501 Unsupported client range**（实测 lemon-music 的附件），
  这时必须退回单流，否则整个下载会失败。
- **分段要有长度校验**：每段下完比对 `end-start+1`，不符就报错 —— 不然合并出来的包
  会静默损坏（虽然最后 sha256 能兜住，但错误信息会很难懂）。
- **GitHub 匿名额度按出口 IP 共享**（60 次/小时），NAS 所在出口很容易已被别的程序用尽；
  检测会 403。所以令牌是事实上的必需项，不是可选项。
- **`install-fpk` 静默失败**：对已安装应用返回退出码 0 但什么都不做 —— 自动化里用它
  做升级会"看着成功、实际没动"。
- **重名应用撞源**：社区已有别人打包的 Ekko Studio（如 `shuangji66/FnDepot` 的
  `Ekko-0.7.35`）。若以后同时装社区商店，`appname` 相同会被判为重复源。
- **manifest 不在第一条**（实测 2026-10-16）：第三方打包（如 `jia070310/lemon-muisc`）
  把 `app.tgz` 放在最前、`manifest` 放在**最后**；只有 fnpack / 本仓的产物才是 manifest 在前。
  所以远端读 manifest 必须**首尾两头都探**，且不能假设位置。
- **同一架构可能有多个包**（实测 `guan-ry/FanControlServerApp` 同时发 `-iframe.fpk` 与
  `-url.fpk`，体积只差 184 字节）。取舍必须是**确定规则**（优先 iframe、`untested` 排最后），
  不能靠"取最大文件"撞运气 —— 否则哪天体积反过来，用户点桌面图标会变成弹浏览器。
  同时也必须允许用户手动改选（`asset` 字段 / 界面上「换版本」）。
- **同版本 Release 附件被替换**（实测 2026-10-16 发现）：`ekko-studio-fpk` 的
  `v0.7.36-1` 附件在发布约 1 小时后被重新上传 —— 附件 `created_at` 16:03:17Z
  晚于 release `published_at` 14:49:44Z，且与本地 15:30Z 下载到的那份
  **大小差 8389 字节、sha256 不同**（本地 `306c9030…`，现服务端 `cc0bc702…`）。
  影响：同一个版本号在不同时间下载会得到不同文件，回滚包与服务端不再逐字节一致。
  本应用的处理是**如实的**：比对本地文件大小与 Release 的 `size`，不一致就标
  「文件不完整」并禁用「安装」，要用户重新下载（绝不拿来源不明的包去装）。
  根治要在 CI 侧：已发布的「版本号 + 架构」文件应视为不可变，内容变了必须发新版本号。

## 打包者

Mickey · 本项目为自用打包。

### 反向验证的锚点会漂移，必须定期重锚

`verify-gates-reverse.py` 里的每条破坏都靠一段源码锚点定位。**改了那段代码，锚点就失效**，
脚本会打印「? 跳过…锚点命中 0 次」并把它算作失败（skip 也算失败，不然「没测」会被当成「通过」）。
加了 `force` 参数之后，「Release 列表不缓存」那条就漂了 —— 是这次跑出来的。
另外判据要落在**行为**上而不是**文案**上：曾经用提示语「这是本应用自己」当判据，
把条件改成 `if False:` 文案还在、守卫照样绿。

### 冒烟测试千万别点「自身更新」

本机跑一份临时实例做冒烟时（`FPKU_VAR` 指到临时目录）看起来是隔离的，但
**应用中心 socket 是绝对路径** `/var/run/com.trim.app.center.sock` —— 一旦走到
`upgrade_via_daemon`，它升的会是 NAS 上**真身**那一个。只读接口（`/api/state`、
`/api/self/check`、`/api/apps/add`）随便点，`/api/self/update`、`/api/update`、
`/api/install`、`/api/download` 在临时实例里**不要碰**。
