# ZmdBot 本地视觉资源

## `zmd-industrial-base.jpg`

- 用途：四类 ZmdBot 图片共用的顶部场景背景。
- 来源：2026-08-09 使用 Codex 内置 OpenAI ImageGen 为本项目生成；没有输入参考图，也没有复制第三方插件素材。
- 后处理：保持 1536×1024 原始尺寸，将生成的 PNG 转换为质量 88 的渐进式 JPEG，以避免过大的 CSS 内嵌资源被 Chromium 丢弃。

最终生成提示词：

```text
Use case: stylized-concept
Asset type: local background image for a 1280px-wide ranking long-image renderer
Primary request: an original futuristic industrial frontier base in a vast pale wilderness, with tall cargo cranes, modular research buildings, elevated pipes and restrained sci-fi machinery
Scene/backdrop: open industrial outpost at early morning, distant mountains and light atmospheric haze
Style/medium: polished cinematic environment concept art with grounded industrial realism; entirely original and not based on any recognizable game, franchise, logo, or location
Composition/framing: wide 3:2 landscape establishing view; important silhouettes concentrated near the upper and outer thirds; calm lower edge that can transition naturally into a light page gradient; enough visual structure to remain legible behind a large translucent UI panel
Lighting/mood: soft overcast dawn, quiet and exploratory, low contrast beneath overlays
Color palette: cool gray-green, pale stone, muted cyan accents, restrained warm gold sunlight
Materials/textures: weathered painted steel, concrete, dust and distant rock
Constraints: background scenery only; no people; no characters; no UI; no text; no logos; no trademarks; no watermark; no border; no excessive bloom; no dark vignette
Avoid: recognizable franchise designs, anime characters, busy center composition, strong saturated colors, illegible signage
```
