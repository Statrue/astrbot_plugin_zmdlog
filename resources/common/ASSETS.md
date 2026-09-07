# ZmdLogBot 本地视觉资源

## `scene-background.svg`

- 用途：所有 ZmdLogBot 图片共用的页面底纹（浅灰点阵 + 细网格），以 `data:image/svg+xml` 内嵌进页面。
- 来源：本仓库自绘，无第三方素材。其余装饰（斜纹块、色条、描边水印）全部由 `base.css` 生成。

## `fonts/*.woff2`

由 `tools/build_fonts.py` 从上游字体子集化生成（GB2312 全集 6763 字 + 模板固定文案；拉丁字体只保留 ASCII 与常用符号）。渲染时页面通过保留域名 `https://fonts.zmdlog.invalid` 引用它们，由 Playwright 路由从内存直接返回；只有退回 AstrBot 文转图时才以 data URL 内嵌（见 `core/render.py`）。

| 文件 | 来源 | 许可 |
| --- | --- | --- |
| `MiSans-Heavy.woff2` / `MiSans-Bold.woff2` / `MiSans-Regular.woff2` | 小米 MiSans（https://hyperos.mi.com/font） | MiSans 字体知识产权许可协议，允许免费商用与嵌入分发 |
| `Barlow-*.woff2` / `BarlowSemiCondensed-ExtraBold.woff2` | Barlow by Jeremy Tribby（https://github.com/jpt/barlow） | SIL Open Font License 1.1 |

不在子集内的字符（生僻字、日文假名等）由 `base.css` 中的系统字体栈回退，因此部署环境仍建议安装一套中文字体（如 Noto Sans CJK）。

## 历史

早期版本曾复制 `astrbot_plugin_endfield` 的背景图 `吊车.jpg`（AGPL-3.0），已在改版时移除；当前仓库不再包含任何 AGPL 素材。
