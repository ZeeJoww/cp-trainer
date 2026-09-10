# CP Trainer 0.4.0

基于 `cp-trainer-local-v0.1.zip` 的本地 Web 竞赛训练工具。复用 Python / SQLite 组题与评分逻辑，提供明暗主题、预约 / 立即开始、赛后按需显示标签、本地题库、离线组题、AtCoder 离线评测和原站提交。

## 打开应用

已部署的本机入口：<http://127.0.0.1:8765/>。

从本目录启动（Python 3.11+，无需 pip 安装）：

```sh
python3 -m cptrainer.web --open
```

Windows 将 `python3` 换成 `python`。应用只监听本机回环地址。可用 `--port 8766` 更改端口，或 `--home /path/to/state` 指定数据目录。Linux 当前用户服务安装命令：

```sh
python3 scripts/install_service.py
systemctl --user status cp-trainer
```

当前用户登录后自动启动，关闭网页不停止服务；停止和重启分别使用 `systemctl --user stop cp-trainer`、`systemctl --user restart cp-trainer`。这不是公网托管站点。

Windows 如需使用原 CLI 的周计划或时区导出，另需 `python -m pip install tzdata==2026.3` 提供 IANA 时区数据；Web 页面时间使用浏览器，不依赖这个包。

## 使用

- **比赛**：初始为空。创建比赛可选择平台、题数、时长、预约时间及是否计入本地训练分。时间按浏览器设备时区显示；预约到时自动开始，留空则手动开始。开赛前可以改时间或直接按“立即开始”。服务停机期间错过的预约在恢复时按原预约时间恢复。
- **真实组题**：自动抽选只使用已完整离线、有难度估计且未在近期分配的真实题目。题数不足时明确报错。训练分在顶栏调整，保留包内初值 CF 2274 / AtCoder 1860；这是独立于官方 rating 的本地训练分。
- **结果和标签**：在比赛中录入 AC、耗时和错误提交次数，然后结束并结算。计时到期或主动结束后才出现“显示标签”；默认隐藏，点击才读取标签，重开比赛页面后仍默认隐藏。未开始和进行中的标签接口拒绝请求，旧 HTML 导出也遵守此规则。
- **本地题库**：搜索、按平台 / 难度筛选、分页阅读题面。勾选同一平台的题目可以手动组题；未估分题目可保留并组成不计分比赛。英文 / 日文、公式、样例、配图与 PDF 题面均可在本机读取。
- **清空比赛**：清空前自动创建 SQLite 一致性备份，保留题库和训练分。测试使用独立临时数据库，正式比赛列表交付时为 0。

## 同步范围与离线数据

默认数据目录 `~/.cp-trainer/`，也可通过环境变量 `CP_TRAINER_HOME` 或 `--home` 指定。数据、缓存、日志和备份不进入 Git。

| 数据                                                   | 保存位置                  |
| ------------------------------------------------------ | ------------------------- |
| 题目名称、原比赛日期、难度、标签、原题链接、比赛及结果 | `trainer.sqlite3`         |
| 清洗后的完整题面 HTML、下载状态及错误                  | SQLite 的 `statements` 表 |
| 配图、内嵌 SVG / 图片、附件与 PDF                      | `assets/`                 |
| 上游 JSON 和整场题面缓存                               | `cache/`                  |
| 清空 / 迁移前的一致性数据库备份                        | `backups/`                |
| 个人设置                                               | `config.toml`             |

交题链接依据已保存的平台、比赛和题号在本机生成，不依赖在线查询。在线交题仍需原网站；下载了测试数据的 AtCoder 题目可在本机离线评测，见下节。离线指本地 Python 服务仍在运行、设备可以完全断开互联网；界面、KaTeX 0.18.6 和字体均随应用保存。

同步按钮可以增量更新、重试失败并继续未完成的题面；也可运行：

```sh
python3 -m cptrainer.archive
python3 -m cptrainer.archive --platform atcoder --statements-only
```

若数据目录还未初始化，先运行 `python3 -m cptrainer init --timezone Asia/Shanghai`。每道题单独提交保存，已完成题面不会重复下载；单平台原生文件锁防止 CLI 和网页重复同步。未下载或缺图题目不会进入自动离线组题。

范围按 UTC 的原比赛开始时间筛选，截止同步执行时刻：Codeforces 自 **2024-07-01** 起，AtCoder 自 **2023-07-03** 起。Codeforces 使用官方公共 `problemset.problems` 题库（含其中的 ICPC 镜像），不涵盖不在该接口内的 Gym、私有组题和未公开题。AtCoder 使用 AtCoder Problems 的公开题目及比赛关联表；每日练习复用旧题不改变原始发布日期或题号。

2026-09-10 核对结果：CF **1,752 / 1,752** 题离线就绪；AtCoder 原日期范围内 **3,321 / 3,322** 题离线就绪，另为测试数据清单补入 28 道较早题目，合计 **3,349 / 3,350** 题。AtCoder `awc0108_d` 的原题地址返回 HTTP 404，已保留元数据和失败状态，可再次同步重试，未以其他题目或演示内容替代。上游尚未公开难度的题目显示“暂无估分”；AtCoder 估分按 AtCoder Problems 的低分变换与取整规则保存。

## 本地看题与原站提交

在本地题面点击 **提交到原站**，可用自己的 AtCoder / Codeforces 账号提交代码，原站结果和代码自动保存到本机，并可计入当前比赛。联网评测使用原站的完整测试数据和语言环境，不受本地测试存档范围限制。

首次在个人浏览器中设置：

1. 安装 [Tampermonkey](https://www.tampermonkey.net/)，然后从本地提交窗口点击 **CP Trainer 提交助手** 安装。若浏览器提示，允许扩展运行用户脚本及访问本机服务；安装后刷新本地页面。
2. 在题面点击 **提交到原站 → 连接原站 / 更新语言**。助手打开原站交题页，在那里登录自己的账号；完成登录 / 验证后，可点击右下角 **打开交题页** 继续。
3. 返回本地页面，选择从原站读取的实际语言，粘贴或读取代码，点击 **提交到原站**。助手填入原站表单并点击原生提交按钮，随后回填结果。保持原站标签页打开。

后续同一题可直接再次提交；换题时连接并读取该题语言。提交记录页面同时保存离线与原站记录，可恢复代码并打开原站结果链接。应用不接收账号密码或 Cookie；登录状态由自己的浏览器和原站维护。退出登录、验证码、扩展权限及原站页面变更可能需要手动处理。

每个平台一次等待一个提交。重复点击、重新打开标签页或重复结果回调不会重复发送 / 记分。通过提交前的历史编号、题号和源代码摘要核对记录；不能确认时不会猜测 AC，也不会自动重发。等待上限 15 分钟；**停止等待** 只结束本地记录，已发送的原站代码无法撤回。遇到超时，请先到原站核对后决定是否重交。

远程待评测状态在本地服务重启后保留；运行中的浏览器助手可继续回传。完成或过期后删除助手中的临时代码；如果原站标签页已全部关闭，临时记录在下次打开该站时清理。本机 SQLite 保留代码与历史。浏览器同步该用户脚本存储的行为由扩展设置决定，不应开启跨设备同步个人代码。

本版本针对个人本机使用，默认直交 AtCoder / Codeforces。已用 Chromium 和隔离的原站表单验证完整流程；当前环境无可用的已登录浏览器，**尚未向真实账号提交，也未完成真实 Tampermonkey 扩展集成验收**。首次安装后需在自己的浏览器登录并验证一次真实提交；原站登录或验证提示必须由用户完成。

## 离线评测

在“评测记录”页面下载测试数据；在“本地题库”勾选 **可离线评测**，打开题面并点击 **离线评测**。可粘贴代码或读取本机文件，选择 C++17 / Python 3 后提交。页面显示每个已运行测试点的结果、耗时及首个失败点的输入、标准输出、实际输出预览；遇到失败后停止。提交代码和结果在刷新、关闭网页后保留。

比赛中打开题目提交时，可将结果计入比赛。有效 AC 自动记录提交时的比赛用时，WA / RE / TLE / MLE / OLE 增加错误次数，CE 不计罚；已经 AC 的题目不会被之后的错误提交覆盖。未完成评测会阻止比赛结算；取消评测后可以继续操作。赛后补题可独立评测，不更改已经结算的成绩。

测试数据来自 [conlacda/atcoder-testcases](https://github.com/conlacda/atcoder-testcases)，按 **存档内可匹配的比赛日期** 取最新 100 场，并非 AtCoder 当前最新 100 场。当前清单为 **2023-06-18 至 2024-11-24**，ABC 最新为 ABC377；混合 ABC / ARC / AGC 及有存档的其他比赛。清单范围内早于原题库起始日的题目会补充入库。界面显示实际下载数量、每场题数及数据缺失。

本机已入库 **100 / 100 场、676 题、25,749 个配对测试点、33.31 GiB 原始文件**。可直接评测 **615 题**；61 题因特殊规则或数据缺口暂不可评测。95 场配对完整，另外 5 场的缺口为 ABC373 F、ABC366 E、ABC355 G、ABC347 G、ARC167 B。

仅使用完整配对的真实存档数据。原存档缺少输入或输出时保留原文件并标明缺口，该题不评测；同场完整题目仍可使用。普通题按空白分隔比对，题面明确允许时支持大小写忽略，以及绝对 / 相对误差。多解构造、交互和输出型题目缺少专用评测器时明确拒绝评测，不能把单份输出差异当作 WA。规则识别采取保守策略，仍不能替代官方判题器。

编译和每个测试点均使用新的禁网 Linux 容器：只挂载当前提交，程序运行时只读；不挂载题库、标准答案、用户主目录或 Docker socket。启用非 root 用户、删除 capabilities、禁止提权、进程数 32、1 个 CPU 的配额，以及题面内存 / CPU 时间限制。墙钟时间另给 2 秒容器启动余量；页面耗时包含启动开销。代码限 128 KiB，输出限 16 MiB，编译限 30 秒 / 1 GiB。一次评测一份代码，等待队列最多 8 份。

离线评测另需 Docker 的 Linux 容器环境；网页阅读和组题仍只需要 Python。准备命令（首次需要联网下载环境，之后评测不联网）：

```sh
python3 scripts/setup_judge.py
systemctl --user restart cp-trainer
```

可用 `--base-image` 复用本机已有、含 g++ / Python / prlimit 的 Debian 镜像。准备脚本仅复制编译器与运行库目录，剔除 Python 第三方包，不复制项目与主目录，并把生成的镜像 ID 固定在 `judge-runtime.json`。本机实际环境为 **GCC 12.2.0、Python 3.9.22**；可在页面查看版本。仅包含标准库，不承诺与 AtCoder 官方完整语言环境一致。

```sh
python3 -m cptrainer.testdata --count 100
python3 -m cptrainer.testdata --verify
```

每场固定 Git 提交，保存压缩包 SHA-256 和各输入 / 输出文件 SHA-256；本次 51,585 个测试文件全量校验通过。`testdata/` 保存原始文件、许可和清单，`cache/testdata/` 保存下载缓存；每次评测前检查所用文件的哈希。重新同步可修复缺失的本地文件；服务器支持 Range 时续传当前压缩包，否则重下当前包。已完整下载的场次跳过。`--verify` 校验所有已下载文件。服务重启会把未完成的离线提交标记为中断，保留代码供再次提交。

迁移到另一台离线机器时，除复制数据目录，还应复制评测镜像；镜像名称为 `cp-trainer-judge:0.3`：

```sh
docker save -o cp-trainer-judge.tar cp-trainer-judge:0.3
docker load -i cp-trainer-judge.tar
```

镜像 ID 保持一致后，数据目录内的 `judge-runtime.json` 可继续使用。测试数据、代码和运行环境均不随源码 ZIP 分发。当前只在本机 Linux 验证离线评测；Windows / macOS 的 Docker Desktop 待实机验证。

## 维护与验证

```sh
pnpm --filter @apps/cp-trainer check
pnpm --filter @apps/cp-trainer test
pnpm --filter @apps/cp-trainer test:ui
pnpm --filter @apps/cp-trainer build
```

不安装 Node 也可运行应用和 Python 测试：`python3 -m unittest discover -s tests -v`。Node / Playwright 仅用于开发验证，便携包位于 `dist/cp-trainer-0.4.0.zip`，不含用户数据。

本次在 Ubuntu 24.04 x64、Python 3.12.3、Chromium 验证；32 项 Python 检查和 6 项浏览器流程覆盖日期归属校正、增量续传、数据库迁移与备份恢复、原子结算、接口边界、预约 / 开始、赛后标签、离线公式、手动组题和 320px 布局。Windows / macOS 与其他浏览器待实机验证；三系统 CI 配置已保留，尚未在远端执行。

完整迁移时，先停止服务，再复制整个数据目录；恢复前另存当前目录，以备回退。只复制运行中的 SQLite 主文件不能保证一致性。恢复 `backups/` 中的数据库时，同目录 `assets/` 保持配套，并在停止服务后处理原数据库和 WAL / SHM 文件。

## 来源与许可

- [conlacda/atcoder-testcases](https://github.com/conlacda/atcoder-testcases)：分支中的 MIT 许可随原始测试文件保留；不执行存档中的脚本。

- 原项目包：`~/Downloads/cp-trainer-local-v0.1.zip`，保留 MIT 许可；演示题仅用于隔离测试，生产 CLI 的 `demo` 入口已移除。
- [Codeforces API](https://codeforces.com/apiHelp)：公共题库及比赛日期，API 请求至少间隔 2.05 秒。
- [AtCoder Problems 数据接口](https://github.com/kenkoooo/AtCoderProblems/blob/main/doc/api.md)：公开元数据及估计难度，请求至少间隔 1.10 秒；题面来自 AtCoder 原站。
- [AtCoder Problems 难度规则](https://github.com/kenkoooo/AtCoderProblems/blob/main/atcoder-problems-frontend/src/utils/index.ts)；[ADT 覆盖题目归属问题](https://github.com/kenkoooo/AtCoderProblems/pull/1553)。
- 本地数学渲染采用工作区已有的 KaTeX 0.18.6，许可随 `cptrainer/static/vendor/katex/LICENSE` 保存。题面版权仍属于相应平台与作者，本地数据不随源码公开分发。
