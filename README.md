# CP Trainer 0.2.0

基于 `cp-trainer-local-v0.1.zip` 的本地 Web 竞赛训练工具。复用 Python / SQLite 组题与评分逻辑，新增明暗主题、可预约和立即开始的比赛、赛后按需显示标签、本地题库与离线组题。

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

交题链接依据已保存的平台、比赛和题号在本机生成，不依赖在线查询。在线交题与评测仍需原网站；本工具不保存隐藏测试数据，不提供本地判题。离线指本地 Python 服务仍在运行、设备可以完全断开互联网；界面、KaTeX 0.18.6 和字体均随应用保存。

同步按钮可以增量更新、重试失败并继续未完成的题面；也可运行：

```sh
python3 -m cptrainer.archive
python3 -m cptrainer.archive --platform atcoder --statements-only
```

若数据目录还未初始化，先运行 `python3 -m cptrainer init --timezone Asia/Shanghai`。每道题单独提交保存，已完成题面不会重复下载；单平台原生文件锁防止 CLI 和网页重复同步。未下载或缺图题目不会进入自动离线组题。

范围按 UTC 的原比赛开始时间筛选，截止同步执行时刻：Codeforces 自 **2024-07-01** 起，AtCoder 自 **2023-07-03** 起。Codeforces 使用官方公共 `problemset.problems` 题库（含其中的 ICPC 镜像），不涵盖不在该接口内的 Gym、私有组题和未公开题。AtCoder 使用 AtCoder Problems 的公开题目及比赛关联表；每日练习复用旧题不改变原始发布日期或题号。

2026-09-10 核对结果：CF **1,752 / 1,752** 题离线就绪；AtCoder **3,321 / 3,322** 题离线就绪。AtCoder `awc0108_d` 的原题地址返回 HTTP 404，已保留元数据和失败状态，可再次同步重试，未以其他题目或演示内容替代。上游尚未公开难度的题目显示“暂无估分”；AtCoder 估分按 AtCoder Problems 的低分变换与取整规则保存。

## 维护与验证

```sh
pnpm --filter @apps/cp-trainer check
pnpm --filter @apps/cp-trainer test
pnpm --filter @apps/cp-trainer test:ui
pnpm --filter @apps/cp-trainer build
```

不安装 Node 也可运行应用和 Python 测试：`python3 -m unittest discover -s tests -v`。Node / Playwright 仅用于开发验证，便携包位于 `dist/cp-trainer-0.2.0.zip`，不含用户数据。

本次在 Ubuntu 24.04 x64、Python 3.12.3、Chromium 验证；21 项 Python 检查、3 项浏览器流程覆盖日期归属校正、增量续传、数据库迁移与备份恢复、原子结算、接口边界、预约 / 开始、赛后标签、离线公式、手动组题和 320px 布局。Windows / macOS 与其他浏览器待实机验证；三系统 CI 配置已保留，尚未在远端执行。

完整迁移时，先停止服务，再复制整个数据目录；恢复前另存当前目录，以备回退。只复制运行中的 SQLite 主文件不能保证一致性。恢复 `backups/` 中的数据库时，同目录 `assets/` 保持配套，并在停止服务后处理原数据库和 WAL / SHM 文件。

## 来源与许可

- 原项目包：`~/Downloads/cp-trainer-local-v0.1.zip`，保留 MIT 许可；演示题仅用于隔离测试，生产 CLI 的 `demo` 入口已移除。
- [Codeforces API](https://codeforces.com/apiHelp)：公共题库及比赛日期，API 请求至少间隔 2.05 秒。
- [AtCoder Problems 数据接口](https://github.com/kenkoooo/AtCoderProblems/blob/main/doc/api.md)：公开元数据及估计难度，请求至少间隔 1.10 秒；题面来自 AtCoder 原站。
- [AtCoder Problems 难度规则](https://github.com/kenkoooo/AtCoderProblems/blob/main/atcoder-problems-frontend/src/utils/index.ts)；[ADT 覆盖题目归属问题](https://github.com/kenkoooo/AtCoderProblems/pull/1553)。
- 本地数学渲染采用工作区已有的 KaTeX 0.18.6，许可随 `cptrainer/static/vendor/katex/LICENSE` 保存。题面版权仍属于相应平台与作者，本地数据不随源码公开分发。
