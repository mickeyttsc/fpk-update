# FPK更新 · 维护文档

> **一句话定位：一个 GitHub 上 fpk 的下载更新器。** 不生产包、不首次安装、不自动安装。
> 面向维护者（构建 / 门禁 / 排障）；用户向说明见 [README.md](README.md)。

## 版本号规则

`<主>.<次>.<修>-<封装迭代号>`，如 `1.0.0-2`。功能变化抬前三位，仅封装/文案变化时迭代号 +1。

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
10. 图标四个槽位均为 512×512 PNG
11. 三个动作（下载/安装/下载并更新）与设置接口必须都在 —— 少一个即判功能回退；
    产物里不得残留登录/密码相关代码（本应用为无密码设计）

## 已知坑（都是实测换来的）

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
