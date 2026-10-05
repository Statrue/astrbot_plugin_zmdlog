# ZmdLogBot 本地视觉资源

## `../shell/` 的纹理（V2 外壳）

- `sky-art.webp`：页头黄天右侧的机械线条半调图（1080×224，无损 WebP，即 540px 的 2 倍）；`box-head.svg` / `box-page.svg`：页头与页面底的大方格加对角线；`topo-strip.svg`：深色名条右端的等高线。
- 来源：本仓库自绘，均由 V2 原型的程序化脚本一次生成（`prototype/frontend-v2` 分支 `prototype/frontend_v2/shards.py` 的 `sky_art()`、`topo.py` 的 `box_grid(90, …)` / `box_grid(108, …)` / `contours(280, 56, …, seed=3, levels=12, base=2, fade_left=True)`），之后作为静态文件提交；运行时不依赖 numpy / Pillow。渲染时由 `core/render.py` 以 data URL 内嵌进页面。
- `comic-topo.svg`：漫画风页面（帮助）黄底上的白色等高线，由同一个 `topo.py` 的 `contours(480, 300, "rgba(255,255,255,0.55)", seed=11, levels=9, base=2, fade_left=False)` 生成。
- `topo-card.svg`：战报 养成 角色名条右端较窄的等高线，`contours(240, 48, "rgba(255,255,255,0.10)", seed=7, levels=10, base=2, fade_left=True)`；`rings.svg`：养成 武器与装备方块右下角的同心波纹，`rings(200)`。同一个 `topo.py` 生成。养成页的潜能 / 精炼星是照游戏美术描出的五片刀刃多边形，直接写在 `shell/wide-parts.html` 的宏里，不是图片。
- 无第三方素材。

## `../help/chibi-*.webp`

- 用途：帮助页上的五个 Q 版小人（持矛的猫耳、冰淇淋、抱臂、挥手、举团子），渲染时由 `core/render.py` 以 data URL 内嵌进页面，不在截图时联网取。
- 来源：用户提供的《加油吧！终末地》官方 Q 版贴纸（鹰角网络），在 V2 原型中切成单张（`prototype/frontend_v2/chibi.py`），再按各自在页面上的 CSS 宽度的 2 倍缩放、存为 WebP（质量 86），共约 106 KB。版权归鹰角所有，不在本仓库的 MIT 许可范围内；用户确认不另做许可审查。

## `../notice/chibi-*.webp`

- 用途：顶屁股通告页头的两个 Q 版小人——左边吃惊的女孩（`chibi-surprised.webp`，页面上 200 px 宽），右边猫耳女孩连同她踢人的想象气泡（`chibi-dreaming.webp`，250 px 宽）。后者是**一张图**：原型试过把人物和气泡拆成两张，拆开就不成一对了，不要再拆。渲染时由 `core/render.py` 以 data URL 内嵌进页面，不在截图时联网取。
- 来源：与帮助页的小人同一份用户提供的《加油吧！终末地》官方 Q 版贴纸（鹰角网络），在 V2 原型中切出（`prototype/frontend-v2` 分支 `289b2c9` 的 `assets/chibi/notice-left.png` / `notice-right.png`），再按页面上 CSS 宽度的 2 倍（400 / 500 px）缩放、存为 WebP（质量 86），共约 105 KB。版权与许可同上。

## `fonts/*.woff2`

由 `tools/build_fonts.py` 从上游字体子集化生成（GB2312 全集 6763 字 + 模板固定文案；拉丁字体只保留 ASCII 与常用符号）。渲染时页面通过保留域名 `https://fonts.zmdlog.invalid` 引用它们，由 Playwright 路由从内存直接返回；只有退回 AstrBot 文转图时才以 data URL 内嵌（见 `core/render.py`）。

| 文件 | 来源 | 许可 | 许可原文 |
| --- | --- | --- | --- |
| `NotoSansSC-Black.woff2` / `NotoSansSC-Bold.woff2` / `NotoSansSC-Regular.woff2` | Noto Sans SC / 思源黑体（https://github.com/notofonts/noto-cjk 的 `Sans/SubsetOTF/SC`） | SIL Open Font License 1.1 | [NotoSansSC-OFL.txt](fonts/NotoSansSC-OFL.txt) |
| `Barlow-*.woff2` / `BarlowSemiCondensed-ExtraBold.woff2` | Barlow by Jeremy Tribby（https://github.com/jpt/barlow） | SIL Open Font License 1.1 | [Barlow-OFL.txt](fonts/Barlow-OFL.txt) |

两份 OFL 原文与字体放在同一目录，这是 OFL 明文要求的分发方式。

**为什么不是 MiSans**：0.12.0 之前中文用的是子集化的 MiSans。它的协议写明
「不得对字体或其任何单独组件进行改编或二次开发」「不得单独将字体或其组件
对外……进一步分发字体软件或其任何副本」，而仓库里放的正是子集化后的副本，
公开仓库即构成再分发。换成 OFL 字体后这两条都不再适用：OFL 明确允许子集化
与再分发，只要求随附许可原文。字形差别很小（Noto 略宽、笔画收尾更圆），
版面、行高、换行都没有变化。

不在子集内的字符（生僻字、日文假名等）由 `base.css` 中的系统字体栈回退，因此部署环境仍建议安装一套中文字体（如 Noto Sans CJK）。

## 历史

早期版本曾复制 `astrbot_plugin_endfield` 的背景图 `吊车.jpg`（AGPL-3.0），已在改版时移除；当前仓库不再包含任何 AGPL 素材。
