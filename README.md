<div align="center">

# astrbot_plugin_zmdlog

### ZmdLogBot · ZMDLogs 公开榜单与战报查询插件

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-4A90E2?style=flat-square)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![GitHub stars](https://img.shields.io/github/stars/Statrue/astrbot_plugin_zmdlog?style=flat-square)](https://github.com/Statrue/astrbot_plugin_zmdlog/stargazers)

在聊天中查询 ZMDLogs 公开榜单、账号成绩与战报摘要，并渲染为统一视觉风格的 PNG 长图。

</div>

---

## 功能

- 查询全部公开榜单及各榜单前三名。
- 按榜单、副本或副本范围查询排名。
- 支持中文名称、榜单 slug、本地别名及模糊匹配。
- 具体榜单默认展示前 10 名，可通过 `--top` 展示前 1–30 名。
- 通过 `accountId` 或账号主页链接查询公开账号的各首领最佳记录。
- 通过 `battleId`、战报页、分享页或排轴页链接生成战报摘要卡。
- 可选在群聊中自动展开 ZMDLogs 战报链接，并对重复链接设置冷却。
- 固定使用 ZMDLogs 公开 DPS 口径，不提供指标切换。
- 内置缓存、同键并发合并、请求重试及统一异常提示。
- 帮助页和查询结果均输出为 1280px 宽的单张长图。

## 安装与更新

### AstrBot 插件管理器

插件发布到 AstrBot 插件市场后，可在插件管理器中搜索 `astrbot_plugin_zmdlog` 安装。

### Git 安装

在 AstrBot 数据目录的插件目录中克隆仓库：

```bash
cd AstrBot/data/plugins
git clone https://github.com/Statrue/astrbot_plugin_zmdlog.git
```

安装 Python 依赖和 Playwright Chromium。两条命令必须在 AstrBot 使用的同一 Python 环境中执行：

```bash
cd astrbot_plugin_zmdlog
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Docker 部署可在宿主机执行：

```bash
docker exec -it astrbot python -m pip install -r /AstrBot/data/plugins/astrbot_plugin_zmdlog/requirements.txt
docker exec -it astrbot python -m playwright install chromium
docker restart astrbot
```

更新插件：

```bash
cd AstrBot/data/plugins/astrbot_plugin_zmdlog
git pull --ff-only
```

更新完成后重启 AstrBot；如果 `requirements.txt` 有变化（例如新增了 `pypinyin`），重启前先重新执行一次 `pip install -r requirements.txt`。插件不会在运行时自动下载浏览器；未安装 Chromium 时会退回 AstrBot 自带的文转图服务（可用 `fallback_to_astrbot_renderer` 关闭），但样式与清晰度以内置 Chromium 为准。

渲染用的字体（MiSans / Barlow 子集）已内嵌在插件里，无需额外安装；昵称中出现生僻字或日文假名时会回退到系统字体，因此 Docker 环境仍建议装一套中文字体（如 `fonts-noto-cjk`）。生成的图片保存在 AstrBot 的 `data/plugin_data/astrbot_plugin_zmdlog/render/` 下并定期清理。

## 指令

> 默认指令前缀为 `/`；如果 AstrBot 修改了唤醒前缀，请使用实际前缀。

| 指令 | 说明 |
|:---|:---|
| `/zmdlog` | 显示帮助页 |
| `/zmdlog help` | 显示帮助页 |
| `/zmdlog 榜单` | 显示全部公开榜单及各榜单前三名 |
| `/zmdlog 榜单 <关键词>` | 查询具体榜单、副本或副本范围 |
| `/zmdlog <关键词>` | 智能匹配榜单或副本 |
| `/zmdlog <关键词> --top <数量>` | 展示具体榜单前 1–30 名，默认 10 名 |
| `/zmdlog 账号 <accountId或账号主页链接>` | 查询公开账号的各首领最佳记录 |
| `/zmdlog 战报 <battleId或战报链接>` | 生成公开战报摘要卡 |
| `/zmdlog 别名` | 查看自定义别名 |
| `/zmdlog 别名 添加 <榜单或副本> <别名...>` | 管理员添加别名，立即生效 |
| `/zmdlog 别名 删除 <别名>` | 管理员删除别名 |

示例：

```text
/zmdlog 罗丹
/zmdlog ld
/zmdlog 罗丹 --top 30
/zmdlog 危机合约
/zmdlog 丰碑4
/zmdlog 丰碑1
/zmdlog 账号 usr_9df6ce8b93e3335c8291c2389b834ef0
/zmdlog 账号 https://zmdlogs.com/records/usr_9df6ce8b93e3335c8291c2389b834ef0
/zmdlog 战报 https://zmdlogs.com/battle/btl_upload_65d03eadfb16?metric=dps
/zmdlog 别名 添加 白垩界卫 白垩 界卫
```

### 关键词怎么写

- **榜单名**可以省略难度后缀和副本前缀：`山犼争王`、`罗丹` 都能直达对应榜单。
- **副本名**可以只写其中一段或"系列+期数"：`山中见犼`、`丰碑4`、`影拓4` 都指向影拓丰碑4期；`丰碑1` 会把 1 期下的 3 个副本一起列出；`丰碑`、`影拓` 列出全部期数。
- **拼音**：支持全拼和首字母，如 `luodan`、`ld`、`sw`、`fb4`。
- 只有一个榜单的副本（如 `危机合约`）直接给出该榜单的排行。
- 匹配到多个目标时会回复一份编号候选列表，**引用那条消息回复序号**（如 `2`）即可选择，10 分钟内有效。
- 以上都命中不了的群内黑话，用 `/zmdlog 别名 添加` 补充即可，不需要改文件或重启。

## 配置

配置项可在 AstrBot 的插件配置页修改。

| 配置项 | 默认值 | 说明 |
|:---|:---|:---|
| `api_base_url` | `https://zmdlogs.com` | ZMDLogs API 地址 |
| `web_base_url` | `https://zmdlogs.com` | 排名记录对应的网页地址 |
| `request_timeout_ms` | `10000` | API 请求超时，单位毫秒 |
| `render_timeout_ms` | `30000` | 图片渲染超时，单位毫秒 |
| `fallback_to_astrbot_renderer` | `true` | 内置 Chromium 不可用时改用 AstrBot 自带文转图服务兜底（默认走 AstrBot 的远程渲染） |
| `alias_file_path` | `aliases.json` | 自定义别名文件，相对路径基于 `data/plugin_data/astrbot_plugin_zmdlog/`（首次启动自动从插件目录复制） |
| `ranking_cache_ttl_seconds` | `60` | 榜单缓存时间，单位秒 |
| `account_cache_ttl_seconds` | `60` | 公开账号成绩缓存时间，单位秒 |
| `battle_cache_ttl_seconds` | `300` | 公开战报摘要缓存时间，单位秒 |
| `auto_expand_battle_links` | `false` | 是否在群聊中自动展开 ZMDLogs 战报链接 |
| `battle_link_dedupe_seconds` | `300` | 同群同战报自动展开冷却时间，单位秒 |
| `fuzzy_match_threshold` | `0.65` | 模糊匹配最低可信阈值 |
| `ambiguity_score_gap` | `0.08` | 要求用户选择候选的最小分差 |

通常无需修改默认配置。使用自定义 API 地址时，建议同时核对网页地址及网络连通性。

## 缓存与异常处理

- `hot-bosses` 和具体榜单分别缓存，并合并同一查询的并发请求。
- 账号成绩与战报摘要分别缓存；完整战报响应只保留卡片所需的概要和角色统计。
- 群聊自动展开默认关闭；开启后每条消息只展开首个可信链接，同群同战报在冷却期内不重复回复。
- `hot-bosses` 刷新失败时可短暂使用最近一次成功结果，并在数据目录保存一份快照，重启后上游不可达时仍可搜索；具体榜单不使用过期数据。
- 网络异常和服务端 `5xx` 最多重试一次，客户端 `4xx` 不重试。
- 图片渲染最多同时执行 2 个任务，输出图片会定期清理；AstrBot 启动完成后会预热 Chromium 并预渲染一次帮助页。帮助页、具体榜单、账号、战报以 2 倍分辨率输出（宽 2560px），全部榜单与副本范围长图保持 1 倍。
- 具体榜单查询到已下线的榜单（上游 404）时提示“没有找到这个榜单”，与网络故障区分开。
- 上游、协议及渲染异常只返回简短提示，不向聊天消息暴露响应内容或堆栈。

## 常见问题

### 安装后提示“图片生成失败”

若同时关闭了 `fallback_to_astrbot_renderer`，请确认 Chromium 安装在 AstrBot 实际使用的 Python 环境中：

```bash
python -m playwright install chromium
```

Docker 用户还应确认容器内 `/AstrBot/data` 可写，并在安装后完整重启容器。

### 提示“ZMDLogs 暂时不可用”

优先检查 AstrBot 所在机器或容器到 `https://zmdlogs.com` 的网络连通性，并查看 AstrBot 日志中的 `ZmdLogBot ranking request failed` 记录。

### `--top` 没有效果

`--top` 只作用于具体榜单，允许值为 1–30；全部榜单和副本范围页面仍固定展示各榜单前三名。

### 输入昵称无法查询账号

当前版本只接受精确 `accountId` 或 `https://zmdlogs.com/records/<accountId>` 账号主页链接；昵称搜索等待上游公开查询能力稳定后再接入。

## 开发与测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖指令路由、帮助页、别名与模糊匹配、缓存并发、API 重试、协议校验和模板渲染边界。

视觉资源说明见 [resources/common/ASSETS.md](resources/common/ASSETS.md)；内嵌字体由 `tools/build_fonts.py` 从上游字体子集化生成。

## 数据说明

本插件只读取 ZMDLogs 已公开的榜单、账号成绩和战报数据，不处理 ZMDLogs 登录凭据或私人战斗记录。内容及可用性以 ZMDLogs 上游服务为准。

## 许可

本项目采用 [MIT License](LICENSE)。内嵌字体的许可见 [resources/common/ASSETS.md](resources/common/ASSETS.md)（MiSans 字体许可协议、SIL OFL 1.1）。

## 鸣谢

- [ZMDLogs](https://zmdlogs.com) 提供公开榜单数据。
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) 提供插件运行框架。

---

<div align="center">

如果这个插件对你有帮助，欢迎点亮一个 Star。

</div>
