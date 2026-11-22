# AGENTS.md — Typora Themes Manager 工作区

## 项目用途

通用 Typora 主题安装器：管理任意 GitHub 主题仓库（注册表 `repos.json`），流水线
「获取安装包 → 安全检查 → 基准清单比对 → 选择性安装 → 报告」。安装包按
**release 压缩包 → release 附件散文件 → 源码树** 自动降级，全部失败时可
**手动匹配**本地文件建立结构（约束 12/13）。

- 本地网页控制台：标准库 `http.server` + 原生 JS。**刻意零依赖，勿引入 pip 包**。
- **双语 zh-CN / en-US**（规范见约束 6）；公开仓库默认 **en-US**，中文系统经检测
  自动获得中文界面。
- git 用于版本管理与发布：远程 `origin` = github.com/ryanlew17/Typora-Themes-Manager；
  历史在 v0.1.0 发布时重新初始化，此后正常提交/推送。
- **运行数据不入库**：`repos/` 与 `repos.json` 均被 .gitignore 排除（各机注册表不同；
  删除后由控制台启动重新生成默认空注册表，只读命令零写入）。运行数据出问题直接
  删除即可；库内文件可 `git checkout -- <file>` 回滚。

## 文件说明

- `theme_manager.py` — 唯一后端源码：引擎 + CLI + 网页服务器 + 双语消息字典
  `MESSAGES`（键→带 `{占位符}` 模板，`t(key, **kw)` 取词，新增语言补一列）
- `ui.html` — 控制台前端（原生 JS，服务端只 serve 这一个文件）；内建 `I18N` + `t()`，
  静态文本用 `data-i18n` / `data-i18n-title` / `data-i18n-ph` 标注，页头按钮切换语言
- `test_theme_manager.py` — unittest 单测，网络/主题目录/注册表全部 mock 到临时目录，
  可安全直跑：`python -m unittest test_theme_manager`
- `README.md` / `README.zh-CN.md` — 最终用户文档（英/中，内容对应，改动两边同步）
- `LICENSE` — MIT（发布打包必须随附）
- `tools/package_release.py` — Release 打包：白名单最小集生成三平台 zip 到 `dist/`；
  `.command` 内置 0755；AGENTS.md/repos/repos.json 等开发与运行数据不进包
- `tools/smoke_test_release.py` — 冒烟测试：沙盒解压模拟首次运行，断言结构/zip 元数据/
  CLI 冷启动（隔离 APPDATA/HOME）/启动脚本语法与编码；**发版前必跑**
- `dist/` — 打包产物（gitignore 排除）
- `start-manager.bat` / `start-manager.command` — 双击启动脚本：探测 Python 3.8+
  （先 py 启动器后 python，商店占位与低版本视为未安装），无环境时英文报错并打开
  python.org；**注释与输出全英文纯 ASCII**（编码坑见「已知坑」）
- `repos.json` — 仓库注册表（运行数据，不入库）：`settings.keep_zips` / `settings.lang`
  + `repos`（以 id 为键的有序字典，旧列表格式自动迁移）。每仓库配置 css_dir /
  theme_re / assets_dirs / bridge / min_themes / source（`release-archive` |
  `release-files` | `git-tree`）/ branch / manual_matched（特化流程标记，见约束 14）。
  `theme_re=null` 表示首次安装时自动推断并回写
- `repos/<id>/` — 每仓库运行数据（不入库）：
  - `zips/<tag>/` — 按版本归档，保留最新 N 个。release 压缩包原样存放，旁有
    `<name>.meta.json` 侧车（下载时的大小/updated_at，缓存复用判据）；tar 资产/
    散文件/源码树组装为统一安装包 `package.zip`；源码树版本目录名 = 树 sha 前 7 位
  - `manifest.json` — 基准清单（仅路径）
  - `installed.json` — 安装记录（卸载/清空据此回滚），`mode` = `merge`/`clean`/
    `adopted`/`matched`，`version` 可为 null=未知；matched 额外有 `tracked`
    {逻辑路径→{hash,ts[,size]}} 与 `tracked_meta`{kind,asset/anchor[,branch],matched_at}。
    **一律 `write_json_atomic` 原子落盘**
  - `latest.json` — 检查缓存：release 完整投影 + etag + asset_pattern + loose_assets
    + source_kind；git-tree 存 anchor/branch；失败只更新 error 不覆盖旧缓存
  - `install-report.md` — 固定名报告，语言无关，每仓库一份

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
python theme_manager.py match ID FILE [FILE ...] # 手动匹配：本地文件按哈希与仓库内容对齐并登记
python theme_manager.py remove ID [--purge]    # 移除注册（--purge 同时删 repos/<id>/）
python tools/package_release.py --version vX.Y.Z [--platform all|windows|macos|linux]
python tools/smoke_test_release.py --version vX.Y.Z   # 发版前必跑
```

退出码：0 成功 / 1 下载或查询失败 / 2 安全检查不通过 / 3 安装失败 / 4 清单不一致 /
5 暂停等待 CSS 远程主机确认（未做任何改动）。

`list`/`check` 只读安全；完整 `install` 会改写本机主题目录，非必要别跑（单测已全 mock）。

## 关键设计约束（改动前必读）

1. **不执行、不内容分析压缩包内的 .bat/.cmd**（用户明确决定；统计数量但跳过内容）。
   其余脚本（.sh/.ps1/.vbs/.py 等）按 `SCRIPT_RULES` 逐行审查，BLOCK/WARN/INFO 三级，
   任一 BLOCK 即中止且不解压；删除类指令按 SAFE_SCOPE_HINTS 判范围（主题目录内=INFO，
   目录外=BLOCK）。
2. **清单比对是防投毒核心**：安装包逻辑清单（剥离顶层目录后）与 `manifest.json`
   不一致 → 取消安装、报告差异、退出码 4，**绝不自动接受**；只有显式
   `--update-manifest`（UI 清单差异弹窗的「接受新清单」）才重建。基准口径恒等于
   最终安装包 zip 的逻辑清单：压缩包=全部条目、散文件=全部附件名、源码树=安装集。
3. **选择性安装**：只写主题 CSS（→ 主题目录根）+ assets_dirs 资源 + 可选桥接；
   README/bat/演示页等一律不写入（多仓库共存互不污染）。
4. **installed.json 驱动卸载/清空重装**：常规仓库移除范围 = 记录文件 ∪ 根目录
   theme_re 匹配 ∪ assets_dirs ∪ 本仓库桥接（归属按 import 文件名判定）；matched
   仓库严格按记录（约束 14）。全部约束在主题目录内，先备份后移除，**绝不动**
   其他仓库/用户主题。
5. **报告在所有失败路径也要写出**（含 UI 任务），固定名 `repos/<id>/install-report.md`
   （save 时清理历史变体，含旧版中文名）。
6. **消息层 i18n**：所有用户可见文本（CLI/日志/报告/HTTP 应答/result.message/前端）
   一律走字典——后端 `MESSAGES`+`t()`、前端 `I18N`+`t()`，**新增文案必须同时补
   zh/en 两列，禁止硬编码**（连标点分隔符也走字典，如 `state_join`）。语言优先级：
   CLI `--lang`（预扫描，先于 `--help` 生效）/ `TTM_LANG` > `settings.lang` >
   系统检测（Windows GetUserDefaultUILanguage：0x09=英/0x04=中；Unix LANG/LC_ALL）
   > 默认 en-US。UI 切换：localStorage + `POST /api/lang`（过 `_guard_mutation`）
   同步服务端 `CURRENT_LANG`。**运行时文件名与 JSON 存储值一律英文/语言无关**，
   只有内容随语言。代码注释保持中文。
7. **UI 安全**：只绑定 127.0.0.1；变更类请求必须带 `X-Local-Tool: 1` 头且 Host 匹配
   （防跨站驱动本地接口），改动鉴权需保持等强度。拒绝请求前必须排空请求体
   （`_drain_body`，见「已知坑」）。
8. **任务结果单次处理**：JobManager 单调递增 `seq`，前端 pollJob 按 seq 门控——
   日志只在有新增时写、结果每任务只处理一次；破坏该语义会复现「完成后每秒重放
   结果、弹窗关不掉」。结果横幅存 `banners` 映射，`render()` 重建卡片后据此恢复。
9. **识别既有安装只登记不写入**（`present_footprint` → `try_adopt`；入口：`add`
   本地检索、`rescan`）：仅扫描主题目录并（重）写 installed.json（+缺失时的基准），
   不改动主题目录、不做安检。结构推断优先本地线索（manifest → zips 最新归档 →
   旧 installed.json），皆无才联网 `infer_structure_online`（与安装同序）。版本号
   只按路径齐整度推断，不吻合则 null。matched 仓库跳过重识别（约束 14）。
10. **repos.json 并发写保护**：所有「load → 修改 → save」整体持 `CONFIG_LOCK`
    （单仓库回写用 `update_repo_cfg`），否则并行任务与 UI 增删互相丢更新。落盘走
    `_write_config_atomic`；`ensure_default_config` 刻意不取锁（冷启动无并发写者）。
11. **CSS 远程主机确认门**：`css_remote_hosts` 词法扫描（字符串/注释感知，原文与
    反转义各一遍防 `\75rl` 混淆；字符串内同形文本不算引用），主机经 `normalize_host`
    归一化（小写/去尾点/IDNA）。存在未同意主机时**任何写入之前暂停**，退出码 **5**
    （区别于安检不通过的 2）；UI 弹窗 / CLI `--allow-remote-host` 逐主机同意，同意
    仅覆盖本次列出的主机。重试参数经 `result["install_options"]` 带回前端。
    **常规与 matched 特化流程同门同语义**。
12. **安装包来源链**（`acquire_package`；rescan 推断 `infer_structure_online` 同序）：
    ① release 压缩包（zip 优先其次 tar 系，`ARCHIVE_EXTS`；7z/rar 及**裸 .gz/.bz2/.xz
    单文件压缩件**在 `UNSUPPORTED_ARCHIVE_EXTS`，提示后跳过、绝不当散文件安装；
    `unsupported_archive_assets` 先排除受支持扩展名，防 `.tar.gz` 被 `.gz` 误伤）
    → ② release 附件散文件（`loose_release_assets`，扁平资产名即逻辑路径，非安装集
    文件由选择性安装自然排除）→ ③ 源码树（`fetch_git_tree` + raw 逐文件下载，只取
    安装集）。②③与 tar 资产均重组为本地 `package.zip`（`build_staging_zip`），此后
    与压缩包**完全同流水线**。**多压缩包需 asset_re 的错误原样抛出不静默降级**。
    git-tree 首装结构推断在确认门前只做临时计算（`pkg["inferred"]`），门后才随
    source/branch 落盘；版本标识 = 树 sha 前 7 位，比较走 `_check_git_tree` 专用
    分支（`version_key` 对非语义化字符串返回 (0,)，不经 tag 比较）。
13. **手动匹配（`run_match`）**：CLI `match ID FILE...` / UI multipart 上传
    （`_read_multipart`，文件名只取 basename，CLI 与 UI 同受 `MAX_UPLOAD_BYTES`=64MB
    总量上限，CLI 读文件失败友好报错）。上传文件算 sha256 + git blob sha，候选
    **覆盖全部 release（新→旧，`fetch_all_releases` 单页 100；每 release 内压缩包→
    散文件）与源码树**（`iter_match_inventories` 惰性生成，命中即停、未消费候选零
    下载；枚举失败退回单最新+重定向降级）。与安装优先级**刻意不同**：匹配是兜底
    恢复，本地文件可能来自任一历史版本。首个「有匹配且能建立结构（含 CSS）」的
    候选胜出；只登记不写主题目录，按需推断结构、补建基准（口径与安装一致），收尾
    prune 历史归档。哈希口径：下载类=sha256、源码树=git blob sha，**勿混用**。
    **重复匹配防范围收缩**：旧登记中未重新匹配、但与胜出候选同 kind 且仍在其清单
    里的路径**原记录原样并入**（结构按「匹配+并入」全集推断）；来源不同或上游已无
    的旧记录被替换并日志明示（`match_carry_log`/`match_replace_log`）。
    **check 校验 tracked**：git-tree 逐文件比对树 blob sha；release 类先比资产元数据
    （大小/updated_at），有变才下载做哈希比对（仅 cur==tag 时执行，历史命中由 tag
    比对直接报新版本）——**始终比记录 vs 仓库侧，绝不读本地主题文件**（用户可能
    自定义过，明确设计决定）。**matched 更新不自动纳入上游新增**，需要时重新 match。
14. **matched 特化流程**（`manual_matched` 标记驱动，`run_install` 路由到
    `run_matched_update`）：一切基于 tracked 记录——**不重新解析结构、不做清单比对、
    不按 theme_re/assets_dirs 模式重推占用范围**（推断模式可能极宽，如单 CSS 无
    连字符时 `^[^/]+\.css$`，按模式清会误伤他仓库主题）。三方可比「记录哈希 /
    仓库侧当前哈希（`fetch_matched_state`，元数据未变免下载）/ 本地哈希
    （`_tracked_file_hash`，**仅用于写入前的自定义保护判断**，run_check 绝不用）」：
    - merge：上游有变且本地未自定义→更新；已自定义→保留不动；本地缺失→恢复；
      上游移除→报告并保留本地。
    - clean：记录范围全部先备份移除，再一律按上游内容重写（**含上游未变化的文件，
      只清不装是曾出过的错误**）；上游已无对应内容时移除后即结束；桥接同常规安装
      重新生成。
    - 待写文件组 staging 包走完整安检与远程主机确认门（退出码语义同常规）。
    - 成功后刷新 tracked：写入项记新哈希；**本地已自定义的保留项完整维持原记录
      （hash/ts/size 都不动）**——只刷元数据会让 check 免下载快路径误判一致；上游
      移除项剔除；files = 旧记录减 clean 移除项再并本次写入（**先减后并**）。
    - `removal_set` 严格按记录 files 取范围；`try_adopt`/rescan 跳过重识别（防按
      模式重写抹掉 tracked；记录文件缺本地时提示可安装恢复）。
    - **终态清理**：卸载成功、或 clean 后上游全移除 → 清记录 + 清 manual_matched
      标记，仓库回到常规流程。
    - **标记在而记录缺失/损坏：绝不静默回退常规流程**——run_install/removal_set
      报错 `err_matched_records_gone`（要求重新 match 或 remove --purge），防宽
      模式清空波及他仓库主题。installed.json 一律 `write_json_atomic`（半截 JSON
      会让 matched 记录静默失效）。

## GitHub 网络层

- API 带 **ETag 条件请求**（304 不消耗匿名配额、复用 latest.json 投影）+ gzip；
  超时常量集中在文件头部。ETag 复用条件：tag + 任一资产（压缩包或 loose_assets）。
- **无 release**：API/重定向 404 → `fetch_latest_release` 返回 `no_release=True`
  （不报错），check/install 降级源码树。
- **源码树**：`/repos/<slug>` 取默认分支（失败回退 main）→ `/git/trees/<branch>?recursive=1`
  （blob sha 即内容哈希；truncated 仅告警）；文件本体走 raw.githubusercontent.com
  （`download_bytes`，不走 API 配额）。
- **限流降级链**：API 403 → releases/latest 302 拿 tag → 常见命名 HEAD 探测资产名；
  `asset_pattern` 记住命中模式下次优先。
- **多仓库 check 并行**（ThreadPoolExecutor）：每仓库日志独立缓冲、按完成顺序整段
  输出；worker 内绝不直接 print（兼容 UI stdout 捕获）。

## 平台与路径

- Typora 主题目录：Windows `%APPDATA%\Typora\themes`；macOS
  `~/Library/Application Support/abnerworks.Typora/themes`；Linux `~/.config/Typora/themes`。
- 所有产物路径基于 `Path(__file__).parent`，与运行时 cwd 无关。
- 备份目录在主题目录同级：`themes-backup-<id>-<时间戳>[-uninstall]/`——**不要自动清理**。

## 已知坑

- GitHub 匿名限流 60 次/小时，本机常 403——**被限流时条件请求同样 403**（304 只在
  配额窗口内生效），降级链接管，报告注明元数据来源。
- 多压缩包未配 asset_re 时 pick_asset 直接报错（不猜）；只有「压缩包全部为不支持
  格式」才提示并继续降级。
- zip/tar 统一走 `ArchiveReader` 适配器（entries/read/test），新代码勿直接开
  zipfile/tarfile；文本读取 utf-8→gbk 回退链（`read_text`）。
- 清单用「剥离单一顶层目录后的逻辑路径」；散文件/源码树组装的 package.zip 无顶层
  目录（top_name=None）。
- multipart 由 `_read_multipart` 手工解析（cgi 模块 3.13 已移除，勿引回）。
  **HTTP/1.1 keep-alive 下必须恰好读满 Content-Length**——被拒绝的请求（超限/缺
  boundary/守门 403）也要先 `_drain_body` 排空，否则残留字节被当作下一个请求解析，
  浏览器复用该连接的后续调用莫名失败（有真实回环连接的回归测试）。
- `download_asset` 跳过下载的条件 = 同名同大小**且**侧车 meta.json 的 size/updated_at
  未变；上游同字节数重传会强制重下。测试直接放缓存文件（无侧车）会被重下一次，属预期。
- UI 任务日志靠 `redirect_stdout` 捕获 print（JobManager._run），引擎必须用传入的
  `log` 参数输出，勿自行 print 到别处。
- 控制台输出依赖 `sys.stdout.reconfigure(line_buffering=True, errors="replace")`，勿移除。
- `start-manager.bat` 必须**纯 ASCII + CRLF**（cmd 解析 UTF-8 批处理有字节偏移 bug，
  中文行会被拆碎当命令执行；全英文后任意代码页等价安全）。smoke_test 已断言。
- `start-manager.command` 三坑：① 文件名纯 ASCII（Finder→Terminal 非 ASCII 转码
  错误报「文件不存在」）；② 需可执行位（本仓库 core.fileMode=false，改完
  `git update-index --chmod=+x start-manager.command` 记录 100755）；③ LocalSend/AirDrop
  接收的文件带 quarantine+provenance，Gatekeeper 拦截且 `xattr -c` 删不掉 provenance，
  需 `cat 文件 > 临时 && mv 回来` 重建 inode。脚本内已 export PYTHONIOENCODING 兜底；
  验证双击路径用 `open start-manager.command`。
- UI 实测隔离真实主题目录：让服务进程 `APPDATA` 指向临时目录（进程内包装最可靠，
  环境变量可能被后台运行器剥离）；测试产生的运行数据直接删除即可（不入库）。

## 已注册仓库（历史参考）

`repos.json` 不入库，以下为原 Windows 开发机的历史记录（以各机实际注册表为准）：

- `typora-theme-phycat`（sumruler/typora-theme-phycat）— 自动推断示例：根目录
  `phycat-*.css` + `phycat/` 字体目录，无桥接。
- `esther-obsidian-typora-themes`（lisitan/esther-obsidian-typora-themes）— 自动推断。
- `visonic-themes`（YXX168/Visonic-Themes）— 自动推断（CSS 在嵌套 `themes/`）；
  曾有手工桥接配置（github.user.css → visonic-streamer-nebula），移除重加会丢失，
  如需恢复在 repos.json 手工补回。
