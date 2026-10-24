import type { GlassOptics } from "@samasante/liquid-glass";

/** liquid-glass material 模式的光学预设（frost 在该模式下就是 CSS blur 的 px）。
 *  折射(url(#svg))只在 Blink 生效，Safari/Firefox 自动降级为磨砂+着色，不会坏。 */

/** 小圆钮（图表设置触发按钮、滚动箭头） */
export const PILL_OPTICS: Partial<GlassOptics> = {
  strength: 0.09,
  depth: 0.55,
  curvature: 0.3,
  bend: 0.5,
  bendWidth: 0.16,
  dispersion: 0.22,
  frost: 9,
  saturate: 1.25,
  sheen: 0.32,
  sheenWidth: 3,
  sheenFalloff: 1.5,
  sheenAngle: 45,
  glow: 0.1,
  glowSpread: 1,
  glowFalloff: 0.5,
  specular: 1,
};

/** 展开菜单面板：面积大，dispersion 压低（每条色散通道都是一遍位移滤波），磨砂给足保证可读性 */
export const MENU_OPTICS: Partial<GlassOptics> = {
  strength: 0.07,
  depth: 0.6,
  curvature: 0.26,
  bend: 0.55,
  bendWidth: 0.1,
  dispersion: 0.14,
  frost: 14,
  saturate: 1.2,
  sheen: 0.35,
  sheenWidth: 3,
  sheenAngle: 45,
  glow: 0.07,
  specular: 0.9,
};
