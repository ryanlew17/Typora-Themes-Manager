# AGENTS.md — Typora Themes Manager 工作区

## 项目用途

通用 Typora 主题安装器：管理任意 GitHub 主题仓库（注册表 `repos.json`），统一流水线
「下载最新 release → 简易安全检查 → 基准清单比对 → 选择性安装 → 报告」。
自带本地网页控制台（标准库 `http.server` + 原生 JS，**零框架零依赖，刻意只用标准库，
勿引入 pip 依赖**）。**界面/报告/输出双语（zh-CN / en-US）**，消息层 i18n 规范见
关键设计约束 6；**本项目作为 GitHub 公开仓库，默认语言为英文**
（`DEFAULT_LANG = "en-US"`，即无线索/未识别系统语言时的兜底；中文系统经
系统检测仍自动获得中文界面）。**git 用于版本管理与发布**：远程 `origin` 已配置
（github.com/ryanlew17/Typora-Themes-Manager）；历史在 v0.1.0 发布时**重新初始化**
（旧开发历史不保留），此后正常提交/推送。`repos/` 运行数据与 `repos.json` 注册表
均已被 `.gitignore` 完全排除、不入库（各机器主题目录与已注册仓库不同，注册表属于
本机运行数据；删除后由控制台启动重新生成默认空注册表，只读命令零写入）；运行数据出
问题直接删除即可，库内文件仍可 `git checkout -- <file>` 回滚。

## 文件说明

- `theme_manager.py` — 唯一后端源码：通用引擎 + CLI + 网页界面服务器 + 双语消息字典
  `MESSAGES`（zh-CN/en-US 各一列，键→带 `{占位符}` 模板；`t(key, **kw)` 取词，
  新增语言补一列即可）
- `ui.html` — 网页控制台前端（原生 JS；服务端只 serve 这一个文件，无文件系统浏览）；
  内建 `I18N` 双语字典 + `t()`，静态文本用 `data-i18n` / `data-i18n-title` /
  `data-i18n-ph` 标注由 `applyI18n()` 应用，页头按钮切换语言
- `README.md` — 面向最终用户的英文文档（顶部链接跳转中文版）
- `README.zh-CN.md` — 中文版文档（与英文版内容对应，改动需两边同步）
- `LICENSE` — MIT 许可证（发布与打包必须随附，`package_release.py` 已包含）
- `tools/package_release.py` — Release 打包：白名单最小集（通用 5 文件 + 平台启动
  脚本）生成三平台 zip 到 `dist/`；`.command` 内置 0755（`create_system=Unix`）；
  AGENTS.md/repos/repos.json 等开发与运行数据不进包
- `tools/smoke_test_release.py` — Release 冒烟测试：沙盒解压模拟「下载→解压→
  首次运行」，断言结构/zip 元数据/CLI 冷启动（隔离 APPDATA/HOME）/
  `bash -n` 语法；发版前必跑
- `dist/` — 打包产物输出目录（gitignore 排除，不提交）
- `start-manager.bat` / `start-manager.command` — 双击启动脚本（Windows / macOS+Linux）：
  探测 Python 3.8+（先 py 启动器后 python，微软商店占位程序与低版本视为未安装），
  无环境时英文报错并自动打开 python.org 下载页，结尾 pause/read 防止窗口闪退；
  **两脚本注释与输出全英文（纯 ASCII）**——面向公开仓库的各语言用户
- `repos.json` — 仓库注册表（**运行数据，.gitignore 排除、不入库**；缺失时由控制台
  启动 `ensure_default_config` 生成默认空注册表，临时文件 + `os.replace` 原子落盘；
  `load_config` 缺文件时只返回默认值**不落盘**——只读命令 list/check 保持零写入，
  `smoke_test_release` 对此有断言）：`settings.keep_zips` + `settings.lang`（UI 切换语言时
  持久化于此）+ `repos`（**以 id 为键的有序字典**；
  旧列表格式由 `load_config` 自动迁移并回写）。每仓库配置 css_dir / theme_re /
  assets_dirs / bridge / min_themes；`theme_re=null` 表示首次安装时自动推断并回写
- `repos/<id>/` — 每仓库独立数据（**运行数据，.gitignore 完全排除、不入库**）：
  `zips/<tag>/*.zip`（按版本归档，保留最新 N 个）、
  `manifest.json`（基准清单，仅路径，不含大小/哈希/时间）、`installed.json`（安装记录，
  卸载/清空重装据此精确回滚；「识别本地既有安装」也会写它，`mode` 字段标注来源，
  `version` 可为 null=版本未知）、`latest.json`（检查缓存：完整 release 投影 + `etag` +
  `asset_pattern` 降级探测记忆；检查失败只更新 `error` 字段不覆盖旧缓存）、
  `install-report.md`（固定名，语言无关，每仓库最多一份）

## 常用命令

```bash
python -m py_compile theme_manager.py          # 语法检查
python theme_manager.py                        # 启动网页控制台（默认 127.0.0.1:17888，自动开浏览器）
python theme_manager.py list                   # 查看仓库与安装状态（只读，安全）
python theme_manager.py --lang en-US list      # 以英文输出（也可用 TTM_LANG 环境变量）
python theme_manager.py check [ID ...]         # 检查最新版本（多仓库并行，只查询不安装）
python theme_manager.py install ID             # 安装/更新（会真实写入本机 Typora 主题目录！）
python theme_manager.py install ID --mode clean       # 清空重装（仅本仓库文件，先备份）
python theme_manager.py install ID --update-manifest  # 清单被拦截后，人工核对差异再重建
python theme_manager.py install ID --allow-remote-host HOST  # 同意指定 CSS 远程主机（可重复）
python theme_manager.py uninstall ID           # 卸载（先备份到 themes-backup-<id>-*-uninstall/）
python theme_manager.py add <URL>              # 添加新仓库（先检索本地遗留数据与主题目录，已装则直接恢复）
python theme_manager.py rescan [ID ...]        # 重新索引：刷新版本缓存+识别既有安装（只登记不写主题目录）
python theme_manager.py remove ID [--purge]    # 移除注册（--purge 同时删 repos/<id>/）
python tools/package_release.py --version v0.1.0 [--platform all|windows|macos|linux]
                                               # 打包 Release 压缩包到 dist/
python tools/smoke_test_release.py --version v0.1.0
                                               # 冒烟测试（沙盒解压+CLI 冷启动，发版前必跑）
```

退出码：0 成功 / 1 下载或查询失败 / 2 安全检查不通过 / 3 安装失败 / 4 清单不一致 /
5 暂停等待 CSS 远程主机确认（未做任何改动）。

测试：`test_theme_manager.py`（unittest，mock 隔离网络/主题目录/注册表，可安全直跑：
`python3 -m unittest test_theme_manager`）。此外 `list`/`check` 只读安全；完整
`install` 会改写 `%APPDATA%\Typora\themes`，非必要别跑。

## 关键设计约束（改动前必读）

1. **不执行、不内容分析压缩包内的 .bat/.cmd**（用户明确决定；统计数量但跳过内容）。
   其余脚本类型（.sh/.ps1/.vbs/.py 等）逐行规则表审查，规则在 `SCRIPT_RULES`，
   BLOCK/WARN/INFO 三级，任一 BLOCK 即中止且不解压。删除类指令按 SAFE_SCOPE_HINTS
   判定范围（主题目录内=INFO，目录外=BLOCK）。
2. **清单比对是防投毒核心**：压缩包逻辑文件清单（剥离顶层目录后）与该仓库
   `manifest.json` 不一致 → 取消安装、报告差异、退出码 4，**绝不自动接受**；
   只有显式 `--update-manifest`（UI 中是清单差异弹窗里的"接受新清单"按钮）才重建。
3. **选择性安装**：只写入主题 CSS（→ 主题目录根）+ assets_dirs 资源 + 可选桥接文件；
   README/bat/演示页等一律不写入主题目录（多仓库共存互不污染）。
4. **installed.json 驱动卸载/清空重装**：移除范围 = 记录文件 ∪ 根目录 theme_re 匹配
   ∪ assets_dirs ∪ 本仓库桥接（桥接归属用 import 文件名判定，防止误删他仓库桥接）；
   全部约束在主题目录内，先备份后移除。清空重装**绝不动**其他仓库/用户主题。
5. **报告在所有失败路径也要写出**（含 UI 任务），文件为 `repos/<id>/install-report.md`
   （save 时顺带清理历史变体，含旧版中文名「安装报告*.md」）。
6. **消息层 i18n（双语 zh-CN / en-US）**：所有用户可见文本（CLI 输出、任务日志、报告、
   HTTP 应答、result.message、前端界面）一律走字典取词——后端 `MESSAGES` + `t()`、
   前端 `I18N` + `t()`，**新增文案必须同时补两列字典，禁止硬编码中文/英文字符串**。
   语言解析优先级：CLI `--lang`（主 parser 预扫描，先于 `--help` 生效）/ 环境变量
   `TTM_LANG` > `repos.json settings.lang` > 系统语言检测（Windows GetUserDefault-
   UILanguage：0x09=英语/0x04=中文；Unix LANG/LC_ALL）> 默认 **en-US**
   （公开仓库默认语言；`detect_system_lang` 未识别的系统语言同样落入英文）。UI 页头按钮切换：写 localStorage +
   `POST /api/lang`（变更类请求，过 `_guard_mutation` 门控）同步服务端全局
   `CURRENT_LANG`，此后该进程生成的日志/报告/消息随之切换；前端无 localStorage
   记忆时跟随 `/api/state` 回报的服务端语言。**运行时产生的文件名与 JSON 存储值
   一律英文/语言无关**（报告固定名 `install-report.md`；`installed.json` 的 `mode`
   取 `merge`/`clean`/`adopted`，旧版中文值无消费方、保留无害），只有报告/日志/
   界面的**内容**随语言。代码注释保持中文。
7. UI 安全：只绑定 127.0.0.1；变更类请求必须带 `X-Local-Tool: 1` 头且 Host 匹配
   （防浏览器跨站驱动本地接口），改动鉴权逻辑需保持等强度。
8. **任务结果单次处理**：JobManager 给每个任务分配单调递增 `seq`，前端 pollJob 按
   `seq` 门控——日志只在有新增行时写入、结果（横幅/清单弹窗/状态刷新）每个任务只
   处理一次。改动轮询逻辑时必须保持该语义，否则会复现"完成后每秒重放结果、
   页面持续刷新、清单弹窗关不掉"的 bug。结果横幅状态存于 `banners` 映射，
   `render()` 重建卡片后据此恢复（5 秒自动刷新会整卡重建 DOM）。
9. **识别既有安装只登记不写入**（`present_footprint` → `try_adopt`，入口为 `add` 时
   的本地检索与 `rescan`/UI「重新索引本地」按钮）：仅扫描主题目录并（重）写
   `repos/<id>/installed.json`（+缺失时的 manifest 基准），**绝不改动主题目录文件**，
   也不做安全检查（不安装任何东西）。结构推断优先本地线索：manifest.json →
   zips 最新归档 → 旧 installed.json，三者皆无才联网下载 release 资产推断
   （`infer_cfg_from_zip`）。版本号只按文件路径齐整度推断（吻合→精确版本，
   否则 null=版本未知）。
10. **repos.json 并发写保护**：并行任务（check-all/rescan-all 的 ThreadPoolExecutor）
   与 UI 增删仓库可能同时写注册表，所有「load → 修改 → save」必须整体持
   `CONFIG_LOCK`（单仓库回写用 `update_repo_cfg`），否则互相丢更新。落盘统一走
   `_write_config_atomic`（临时文件 + `os.replace`）；`ensure_default_config`
   **刻意不取锁**（冷启动时尚无并发写者，默认内容确定，覆盖检查 + 原子替换无害）。
11. **CSS 远程主机确认门**：`security_scan` 静态提取压缩包内 CSS `url()`/`@import`
   直接引用的 HTTP(S) 主机（`css_remote_hosts`：字符串/注释感知的词法扫描，
   原文与反转义各一遍防 `\75rl` 转义混淆；`content:"url(...)"` 等字符串内同形
   文本**不算**引用；主机经 `normalize_host` 归一化——小写/去尾点/IDNA punycode）。
   存在未同意主机时安装**在任何写入之前暂停**（推断/清单重建/清空/写主题目录均
   未发生），退出码 **5**（与安全检查不通过的 2 区分），UI 弹窗确认 /
   CLI `--allow-remote-host HOST` 逐主机同意；同意仅覆盖本次列出的主机，
   上游新增主机需再次确认。重试参数经 `result["install_options"]` 随结果带回
   前端（mismatch 弹窗重试时保留原 mode 与已同意主机）。

## GitHub 网络层

- API 请求带 **ETag 条件请求**（If-None-Match，304 命中不消耗匿名配额并直接复用
  latest.json 缓存投影）+ **gzip** 压缩；超时常量 `API_TIMEOUT`/`REDIRECT_TIMEOUT`/
  `HEAD_TIMEOUT`/`DOWNLOAD_TIMEOUT` 集中定义在文件头部。
- **限流降级链**：API 403 → 解析 `releases/latest` 302 重定向拿 tag → 按常见命名
  `<repo>-<tag>.zip` / `<repo>_<tag>.zip` / `<repo>.zip` HEAD 探测资产名；
  `asset_pattern` 记住上次命中的命名模式，下次优先探测。
- **多仓库 check 并行**（`check_repos_parallel`，ThreadPoolExecutor）：每仓库日志
  收集进独立缓冲、按完成顺序整段输出——为兼容 UI 的 stdout 捕获，worker 内
  绝不直接 print。

## 平台与路径

- Typora 主题目录：Windows `%APPDATA%\Typora\themes`；macOS
  `~/Library/Application Support/abnerworks.Typora/themes`；Linux `~/.config/Typora/themes`
  （`detect_themes_dir()`）。
- 所有产物路径基于 `Path(__file__).parent`，与运行时 cwd 无关。
- 备份目录在主题目录同级：`themes-backup-<id>-<时间戳>[-uninstall]/`——**不要自动清理**。

## 已知坑

- GitHub API 匿名限流 60 次/小时，本机常触发 403——**已被限流时条件请求同样 403**
  （ETag/304 只在配额窗口内生效），此时降级链接管，报告会注明元数据来源。
- release 有多个 zip 且未配 asset_re 时 pick_asset 直接报错（不猜），提示人工配置。
- zip 内文件名可为 UTF-8 中文；zip 文本读取用 utf-8→gbk 回退链。
- 解压/清单用"剥离单一顶层目录后的逻辑路径"，与磁盘实际解压目标解耦。
- UI 任务日志靠 `contextlib.redirect_stdout` 捕获 print（JobManager._run），
  引擎代码必须用传入的 `log` 参数输出而不是自行 print 到别处。
- 控制台输出依赖 `sys.stdout.reconfigure(line_buffering=True, errors="replace")`，勿移除。
- `start-manager.bat` 必须保持**纯 ASCII 内容 + CRLF 行尾**：历史上该文件含中文
  输出时必须以 ANSI/GBK 保存（cmd 对 UTF-8 批处理及批处理内任何 chcp 切换都存在
  字节偏移解析 bug，中文行会被拆碎当命令执行）；现已全英文化，纯 ASCII 在任意
  代码页/编码下等价安全。`tools/smoke_test_release.py` 已断言包内该文件为
  ASCII+CRLF。
- `start-manager.command` 有三个坑，改动后需全部满足：
  1. 文件名保持**纯 ASCII**：双击 .command 时 Finder→Terminal 传递非 ASCII 文件名
     会被错误转码，Terminal 执行乱码路径报「文件不存在」；
  2. 需要**可执行位**（本仓库 `core.fileMode=false`，git 不跟踪权限位，改完用
     `git update-index --chmod=+x start-manager.command` 显式记录 100755）；
  3. 经 LocalSend/AirDrop 等途径接收的文件带 `com.apple.quarantine`，且其
     `com.apple.provenance` 标记为不受信来源，Gatekeeper 会拦「无法打开」——
     `xattr -c` 删不掉 provenance（系统保护），需 `cat 文件 > 临时 && mv 回来`
     重建 inode 才能换成可信本地标记。脚本内已 `export PYTHONIOENCODING=utf-8:replace`
     兜底输出编码。验证双击路径用 `open start-manager.command`。
- UI 实测若要隔离真实主题目录：让服务进程的 `APPDATA` 指向临时目录即可（进程内
  包装脚本最可靠，环境变量可能被后台运行器剥离；install/uninstall 会改写
  `repos/<id>/installed.json` 与报告，**`repos/` 与 `repos.json` 均已不入库，
  测试产生的运行数据直接删除即可**；单测 `test_theme_manager.py` 已把
  CONFIG_PATH/DATA_DIR/主题目录/网络层全部 mock 到临时目录，直跑无副作用）。

## 已注册仓库（历史参考）

`repos.json` 自 2026-10 起不入库，以下为原 Windows 开发机的历史注册记录（macOS
开发机注册表与本机实际安装的仓库不同，以各机 `repos.json` 为准）：

- `typora-theme-phycat`（sumruler/typora-theme-phycat）— `add` 命令添加、结构自动推断的
  示例：根目录 `phycat-*.css` + `phycat/` 字体资源目录，无桥接。
- `esther-obsidian-typora-themes`（lisitan/esther-obsidian-typora-themes）— 自动推断。
- `visonic-themes`（YXX168/Visonic-Themes）— 自动推断（CSS 在嵌套 `themes/`）。
  历史注记：曾以手工精调 id `visonic` 注册（含 `github.user.css` 桥接 →
  visonic-streamer-nebula）；移除重加后桥接配置丢失，如需恢复在 repos.json 手工补回。
