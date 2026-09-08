# ZmdLogBot 本地视觉资源

## `scene-background.svg`

- 用途：所有 ZmdLogBot 图片共用的页面底纹（浅灰点阵 + 细网格），以 `data:image/svg+xml` 内嵌进页面。
- 来源：本仓库自绘，无第三方素材。其余装饰（斜纹块、色条、描边水印）全部由 `base.css` 生成。

## `fonts/*.woff2`

由 `tools/build_fonts.py` 从上游字体子集化生成（GB2312 全集 6763 字 + 模板固定文案；拉丁字体只保留 ASCII 与常用符号）。渲染时页面通过保留域名 `https://fonts.zmdlog.invalid` 引用它们，由 Playwright 路由从内存直接返回；只有退回 AstrBot 文转图时才以 data URL 内嵌（见 `core/render.py`）。

| 文件 | 来源 | 许可 | 许可原文 |
| --- | --- | --- | --- |
| `MiSans-Heavy.woff2` / `MiSans-Bold.woff2` / `MiSans-Regular.woff2` | 小米 MiSans（https://hyperos.mi.com/font） | MiSans 字体知识产权许可协议 | [MiSans-NOTICE.md](fonts/MiSans-NOTICE.md) |
| `Barlow-*.woff2` / `BarlowSemiCondensed-ExtraBold.woff2` | Barlow by Jeremy Tribby（https://github.com/jpt/barlow） | SIL Open Font License 1.1 | [Barlow-OFL.txt](fonts/Barlow-OFL.txt) |

OFL 要求许可原文随字体文件一起分发，因此 `Barlow-OFL.txt` 与字体放在同一目录。

**MiSans 待办**：其协议写明「不得对字体或其任何单独组件进行改编或二次开发」
「不得单独将字体或其组件对外……进一步分发字体软件或其任何副本」。仓库里的
MiSans 是子集化后的副本，公开分发是否越界没有把握，计划换成 SIL OFL 授权、
明确允许子集化与再分发的中文字体（如 Noto Sans SC / 思源黑体），换完这三个
文件即可从仓库移除。渲染出的图片是「使用字体创作的作品」，协议明确不受限制。

不在子集内的字符（生僻字、日文假名等）由 `base.css` 中的系统字体栈回退，因此部署环境仍建议安装一套中文字体（如 Noto Sans CJK）。

## 历史

早期版本曾复制 `astrbot_plugin_endfield` 的背景图 `吊车.jpg`（AGPL-3.0），已在改版时移除；当前仓库不再包含任何 AGPL 素材。
