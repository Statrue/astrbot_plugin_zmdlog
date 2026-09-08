# ZmdLogBot 本地视觉资源

## `scene-background.svg`

- 用途：所有 ZmdLogBot 图片共用的页面底纹（浅灰点阵 + 细网格），以 `data:image/svg+xml` 内嵌进页面。
- 来源：本仓库自绘，无第三方素材。其余装饰（斜纹块、色条、描边水印）全部由 `base.css` 生成。

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
