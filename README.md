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
- 具体榜单默认展示前 10 名，可通过 `--top` 展示前 1–30 名，可用 `--角色` 只看带某个角色的记录。
- 查看全部副本或某个榜单的六星角色 DPS 分布（箱线图），也可查看单个角色在各榜单的分布；均可按时间范围与潜能筛选。
- 查看某个榜单的职业位出场率、前 N 名常见阵容与主C分布。
- 通过昵称、`accountId` 或账号主页链接查询公开账号的各首领最佳记录；昵称支持模糊搜索。
- 通过 `battleId`、战报页、分享页或排轴页链接生成战报摘要卡。卡片自上而下是：战斗贡献与本场角色、「DPS 曲线」（每秒累计折算的平均 DPS，每条线的终点等于表里那个人的 DPS）、「BUFF 覆盖」（横向时间轴，每行一个增益，条形是它在场的时段）与「施法节奏」（竖向时间轴，每个角色一条色轨），底部是每个角色的配装概览与主要伤害来源。
- `配装` 查看某场战报每个角色的等级、潜能、武器精炼、技能等级和四件装备的套装、强化、词条数值；`技能` 查看各技能的次数、总伤、占比、均伤与最高伤害（普攻各段合并）。
- `技能轴` 把战报卡里的施法节奏放大成整页：时间自上而下，每个角色一条色轨，同时进行的招在轨内并排，召唤物退到轨旁的斜纹细条，回复技力的重击标黄点；比例按战斗时长自适应。数据来自 ZMDLogs 的公开导出接口，旧版客户端上传的战斗没有施法序列，战报卡会写明原因。
- `对比` 把同一首领的两场战报并排：写榜单加名次（默认第 1 名对第 2 名），或直接给两个 battleId / 链接（首领不同会拒绝，每个首领的排轴都不同）；并排看通关时间、总 DPS、总伤害与主 C（占优一侧标黄），阵容并排，两条全队 DPS 曲线共用横轴，同一角色的等级、武器、四件装备和技能等级逐项对照，不同的项标出，各自附前三伤害来源。
- 可选在群聊中自动展开 ZMDLogs 战报链接，并对重复链接设置冷却。
- 关注公开账号后，它的榜单名次掉下来时在本群通报，并列出这段时间内新出现在它上方的纪录（谁、多少 DPS、战报链接）。单次榜单查询无法证明是谁把它顶下去的，所以只陈述新增了哪些纪录，认不出来时只报名次变化。
- 关注某个榜单后，它的前三名出现新纪录时在本群通报（谁、主C、用时、战报链接），并说明谁跌出了前三；整份榜单关注列表每轮只多一个请求。
- `趋势` 查看关注过的账号各榜名次随时间的变化（阶梯折线，越靠上名次越好）：名次通报每轮记一次，只在名次变化时留点，本地保存 90 天、每榜最多 120 个点。
- 向大模型提供 4 个函数工具（function calling），可以直接用自然语言问，不必记指令：榜单（排名、阵容组合、某角色最常同队的人）、战报（单场或两场对比）、角色 DPS 分布、公开账号成绩。工具会把对应的长图发出来，同时把图里的事实以文字交给模型解读——数值以图为准。
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
| `/zmdlog <关键词> --角色 <角色名> [角色名…]` | 只看带某个角色的排名；写多个角色名时只看同时带上他们的队伍，名次保持全榜名次 |
| `/zmdlog 角色统计` | 全部副本的六星角色 DPS 分布 |
| `/zmdlog 角色统计 <榜单关键词>` | 某个榜单的六星角色 DPS 分布 |
| `/zmdlog 角色统计 <角色名>` | 该角色在各榜单的 DPS 分布与榜内名次 |
| `/zmdlog 角色统计 [榜单关键词] --范围 <7d\|14d\|30d\|all> --潜能 <0\|1-5\|all>` | 限定统计的时间范围与角色潜能 |
| `/zmdlog 阵容 <榜单关键词> [--top <数量>]` | 某个榜单的职业位出场率与前 N 名常见阵容 |
| `/zmdlog 账号 <昵称、accountId或主页链接>` | 查询公开账号的各首领最佳记录；昵称至少 2 个字符，多个结果时回复序号选择 |
| `/zmdlog 战报 <battleId或战报链接>` | 生成公开战报摘要卡（含 DPS 曲线、BUFF 覆盖、施法节奏与配装概览） |
| `/zmdlog 战报 <榜单关键词> [名次]` | 直接看该榜单第 N 名的战报，默认第 1 名（如 `战报 罗丹 3`） |
| `/zmdlog 配装 <battleId、链接或榜单关键词 [名次]>` | 该场战报每个角色的等级、潜能、武器、技能等级与四件装备的词条（别名 `装备`） |
| `/zmdlog 技能 <battleId、链接或榜单关键词 [名次]>` | 该场战报每个角色各技能的伤害统计，普攻各段合并（别名 `技能统计`） |
| `/zmdlog 技能轴 <battleId、链接或榜单关键词 [名次]>` | 该场战报的施法节奏放大成整页，每个角色一条色轨（别名 `排轴` / `时间轴`） |
| `/zmdlog 对比 <榜单关键词> [名次A] [名次B]` | 同一榜单两场战报并排对比，默认第 1 名对第 2 名，只写一个名次就是榜首对它（如 `对比 罗丹 1 3`）；别名 `比较` |
| `/zmdlog 对比 <battleId或链接> <battleId或链接>` | 指定两场战报并排对比；两场须是同一首领，每个首领的排轴不同，跨首领不做 |
| `/zmdlog 关注 <昵称、accountId或主页链接>` | 把公开账号加入本群关注列表，名次掉了会通报 |
| `/zmdlog 关注 榜单 <榜单关键词>` | 把榜单加入本群关注列表，前三名有新纪录会通报 |
| `/zmdlog 关注` | 查看本群关注的账号与榜单及各自序号 |
| `/zmdlog 取关 <序号或昵称>` | 取消关注账号，添加者本人或管理员可操作 |
| `/zmdlog 取关 榜单 <序号或榜单关键词>` | 取消关注榜单，添加者本人或管理员可操作 |
| `/zmdlog 趋势 <昵称、accountId或主页链接> [--范围 7d\|14d\|30d\|all]` | 关注过的账号各榜名次随时间的变化，默认近 30 天 |
| `/zmdlog 别名` | 查看自定义别名 |
| `/zmdlog 别名 添加 <榜单或副本> <别名...>` | 管理员添加别名，立即生效 |
| `/zmdlog 别名 删除 <别名>` | 管理员删除别名 |

示例：

```text
/zmdlog 罗丹
/zmdlog ld
/zmdlog 罗丹 --top 30
/zmdlog 罗丹 --角色 黎风
/zmdlog 角色
/zmdlog 角色统计 罗丹 --范围 7d --潜能 0
/zmdlog 角色统计 洛茜
/zmdlog 阵容 罗丹 --top 30
/zmdlog 危机合约
/zmdlog 丰碑4
/zmdlog 丰碑1
/zmdlog 账号 CPU 0
/zmdlog 账号 usr_9df6ce8b93e3335c8291c2389b834ef0
/zmdlog 账号 https://zmdlogs.com/records/usr_9df6ce8b93e3335c8291c2389b834ef0
/zmdlog 关注 CPU 0
/zmdlog 取关 1
/zmdlog 关注 榜单 罗丹
/zmdlog 取关 榜单 1
/zmdlog 趋势 CPU 0 --范围 7d
/zmdlog 战报 https://zmdlogs.com/battle/btl_upload_65d03eadfb16?metric=dps
/zmdlog 战报 罗丹
/zmdlog 战报 罗丹 3
/zmdlog 配装 罗丹
/zmdlog 技能 罗丹 2
/zmdlog 技能轴 罗丹
/zmdlog 对比 罗丹 1 3
/zmdlog 别名 添加 白垩界卫 白垩 界卫
```

### 关键词怎么写

- **榜单名**可以省略难度后缀和副本前缀：`山犼争王`、`罗丹` 都能直达对应榜单。
- **副本名**可以只写其中一段或"系列+期数"：`山中见犼`、`丰碑4`、`影拓4` 都指向影拓丰碑4期；`丰碑1` 会把 1 期下的 3 个副本一起列出；`丰碑`、`影拓` 列出全部期数。
- **拼音**：支持全拼和首字母，如 `luodan`、`ld`、`sw`、`fb4`。
- 只有一个榜单的副本（如 `危机合约`）直接给出该榜单的排行。
- 直接输入昵称（如 `/zmdlog 某某玩家`）时，只要榜单/副本没有精确命中就会同时搜索公开账号：精确同名账号直接出图，双方都是模糊结果时合并为一份候选列表。
- 匹配到多个目标时会回复一份编号候选列表，**引用那条消息回复序号**（如 `2`）即可选择，10 分钟内有效。
- 以上都命中不了的群内黑话，用 `/zmdlog 别名 添加` 补充即可，不需要改文件或重启。

### 选项怎么写

- 选项统一写在查询末尾，形如 `--选项 值`，顺序不限，每个只能写一次。
- `--top <1–30>`：具体榜单与阵容页的展示条数。
- `--角色 <角色名>`（也可写 `--char`）：优先只看以该角色为主C的排名；该角色在这个榜没有主C记录时（辅助、重装等从不当主C的角色），自动改为匹配整个四人阵容，页面上会注明并高亮它所在的位置。支持全名、拼音全拼或首字母（如 `lf`），多个角色都匹配时会提示写全名。写两个以上角色名（最多 4 个，如 `罗丹 --角色 黎风 洛茜`）时只看同时带上他们的队伍，页面高亮每个匹配到的位置。
- `--范围 <7d|14d|30d|all>`（也可写 `--range`，或 `7天/30天/全部`）用于 `角色统计` 与 `趋势`（趋势默认 30d）；`--潜能 <0|1-5|all>`（也可写 `--potential`）只用于 `角色统计`。潜能只有零潜 / 有潜能（1–5 合并）/ 全部三档，这是 ZMDLogs 接口本身的粒度，不能只看某一层潜能。
- `角色统计` 后面接榜单关键词或角色名都可以：接榜单看"该榜谁强"，接角色名看"该角色哪里强"；`阵容` 只接榜单关键词；关键词命中副本或系列时会给出该副本下的榜单候选，引用回复序号即可。危机合约不提供角色统计。

## 大模型工具

在 AstrBot 里接入了大模型并开启函数调用后，群友可以直接用自然语言提问，不必记指令。
插件注册了 4 个工具，一个主题一个，不按功能点拆分（工具名以 `query_endfield_` 开头）：

| 工具 | 回答什么 | 附图 |
| --- | --- | --- |
| `query_endfield_board` | 某榜前几名的用时、DPS、主C、阵容、battleId，职业位出场率，出现过的阵容各几次；给角色名则只看带它的记录并统计它最常和谁同队 | 榜单页 |
| `query_endfield_battle` | 某场战报的用时、各角色 DPS 与占比、配装养成、伤害来源、BUFF 覆盖、施放次数；给第二场则改为同一首领两场的逐项对比 | 战报卡 / 对比页 |
| `query_endfield_character` | 某六星干员的 DPS 分布与各榜名次，可限定一个榜 | 角色统计页 |
| `query_endfield_account` | 某公开账号各首领的最好成绩 | 账号页 |

工具少是刻意的：开着函数调用时，每条经过模型的消息都会带上全部工具的说明，
工具越多越贵；相近的工具越多，模型越容易选错。

几条刻意的限制：

- **图是权威，文字是解读。** 工具会把长图发到群里，同时把事实以文字交给模型。
  工具描述里明确要求模型不要复述数字，只解释——图里的数值不经过模型，不会被说错。
- **一轮最多发一张图。** 模型回答一个问题时常连调两三个工具，三张长图会刷屏。
- **只回答"公开记录里有什么"，不回答"谁更强"。** 榜单是自我选择的样本：
  只有成功且值得上传的记录才在里面，没有失败样本，用它的人往往也更会玩。
  出场次数、名次、分布都是数出来的，可以答；"某角色更强""某阵容循环 DPS 更高"
  不能答。循环 DPS、专武收益这类需要伤害计算器，不在本插件范围内。
- **名字拿不准就说拿不准。** 榜单关键词只是勉强相似时，工具会直接说没有可靠匹配，
  而不是猜一个最接近的——指令查询有人看着图能自己判断，工具调用没有。

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
| `character_stats_cache_ttl_seconds` | `120` | 角色统计缓存时间，单位秒 |
| `battle_cache_ttl_seconds` | `300` | 公开战报摘要缓存时间，单位秒 |
| `auto_expand_battle_links` | `false` | 是否在群聊中自动展开 ZMDLogs 战报链接 |
| `battle_link_dedupe_seconds` | `300` | 同群同战报自动展开冷却时间，单位秒 |
| `fuzzy_match_threshold` | `0.65` | 模糊匹配最低可信阈值 |
| `ambiguity_score_gap` | `0.08` | 要求用户选择候选的最小分差 |
| `rank_watch_enabled` | `true` | 是否开启名次通报（账号名次下降与榜单新纪录；关注 / 取关 指令与轮询） |
| `rank_watch_interval_seconds` | `900` | 名次检查间隔，单位秒，最低 120 |
| `rank_watch_rank_threshold` | `10` | 只通报原名次在前几名以内的下降 |
| `ranking_index_enabled` | `true` | 后台常驻全部榜单排名（榜单索引） |
| `ranking_index_pace_seconds` | `20` | 榜单索引重读节奏，每隔多少秒重读一个榜，最低 5 |

通常无需修改默认配置。使用自定义 API 地址时，建议同时核对网页地址及网络连通性。

## 缓存与异常处理

- `hot-bosses` 和具体榜单分别缓存，并合并同一查询的并发请求。
- 账号成绩与战报摘要分别缓存；完整战报响应保留概要、角色统计、上传时记录的阵容配装与技能统计，另外读取逐次命中里的伤害时刻（画 DPS 曲线）和按角色分组的增益与减益（画 BUFF 覆盖）。装备词条、逐次命中与增益都按宽松规则解析，缺失或畸形的条目会被跳过而不是让整张卡失败。
- 技能轴读的是公开导出接口 `GET /api/v1/battles/{id}/export`（免登录，按 IP 限流每分钟 60 次，上游缓存 60 秒），与战报详情分开缓存；被限流时提示稍后再试。战报卡也会尽量读一次导出接口来画施法节奏，拿不到时只省略该段或写明原因，不影响出卡。
- 后台常驻**榜单索引**：启动后把全部榜的排名读进内存（约 5MB，十几秒），之后每 20 秒重读一个榜、每 2 分钟核对一次各榜前三名，前三名变了的榜立即重读。榜单查询、角色排名、名次通报都从索引读，几乎不再等上游；可用 `ranking_index_enabled` 关闭，`ranking_index_pace_seconds` 调节奏。
- 渲染时拉取的角色头像、武器和装备图标在内存中长期缓存（30 天，上限 32MB），同一批头像不会每次出图都重新下载。
- 群聊自动展开默认关闭；开启后每条消息只展开首个可信链接，同群同战报在冷却期内不重复回复。
- `hot-bosses` 刷新失败时可短暂使用最近一次成功结果，并在数据目录保存一份快照，重启后上游不可达时仍可搜索；具体榜单不使用过期数据。
- 网络异常和服务端 `5xx` 最多重试一次，客户端 `4xx` 不重试。
- 名次通报每轮对每个被关注账号发一个请求（最多 2 个并发），只有检测到掉名次时才额外拉一次该榜单，列出这段时间内新出现在它上方的纪录（不代表就是这条纪录把它顶下去的）；某个账号这一轮拉不到时保留它上一轮的名次（绝不拿空档去比对，避免误报）；停机或断网超过一段时间后，过期的基线会被丢弃并静默重建，不会把积压的掉名次当成新消息重播。关注列表与名次快照存在插件数据目录（`watchlist.json` / `rank-snapshot.json`），只包含公开 `accountId`、公开昵称和名次，不含任何非公开信息。
- 榜单关注每轮只额外读一次 `hot-bosses`（绕过查询缓存与本地快照，保证比对的是最新数据），把每个被关注榜单的前三名与上一轮比较：只有出现新的战报才通报，并列出被挤出前三的纪录；单纯少了一条（战报被删除）不算新闻。前三名快照存在 `board-snapshot.json`，与账号名次一样过期即静默重建。
- 名次趋势不请求上游：`趋势` 直接读名次通报留下的记录（`rank-history.json`，只含公开 `accountId`、公开昵称快照与名次点）。账号未被任何群关注时没有记录；关注时先记下当前名次作为起点，取关后没人再关注该账号时记录一并删除。昵称先在本地记录里匹配，匹配不到才走一次账号搜索。
- 图片渲染最多同时执行 2 个任务，输出图片会定期清理；AstrBot 启动完成后会预热 Chromium 并预渲染一次帮助页。帮助页、具体榜单、账号、战报、配装、技能统计、技能轴、趋势以 2 倍分辨率输出（宽 2560px），全部榜单与副本范围长图保持 1 倍。
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

优先检查 AstrBot 所在机器或容器到 `https://zmdlogs.com` 的网络连通性，并查看 AstrBot 日志中的 `ZmdLogBot … request failed` 记录。

### `--top` 没有效果

`--top` 只作用于具体榜单，允许值为 1–30；全部榜单和副本范围页面仍固定展示各榜单前三名。

### 输入昵称查不到账号

昵称需要 2–64 个字符，只能搜到有公开榜单记录的账号；多个结果时回复序号选择。查不到时可改用 `accountId` 或 `https://zmdlogs.com/records/<accountId>` 账号主页链接。

## 开发与测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖指令路由、帮助页、别名与模糊匹配、候选列表、缓存并发、API 重试、协议校验、名次通报逻辑和模板渲染边界；`tests/test_main_handlers.py` 用假事件直接驱动 `main.py` 的处理器，需要本地环境装有 AstrBot，否则自动跳过。

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
