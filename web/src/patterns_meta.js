// 形态图鉴元数据：中文名、简介、迷你蜡烛图数据、参与K线根数。
// candles: 每根 [open, close, high?, low?]，数值 0~100，未给 high/low 时取实体边界。
// bars: 该形态由几根K线构成（命中特效会让这几根一起触发）。

/* 按币种 icon 主色生成的渐变配色 */
export const COIN_GRADIENTS = {
  btc: ["rgba(247,147,26,0.6)", "rgba(255,215,0,0.45)", "rgba(255,120,0,0.4)"],
  eth: ["rgba(98,126,234,0.6)", "rgba(140,110,255,0.45)", "rgba(80,160,255,0.4)"],
  bnb: ["rgba(240,185,11,0.6)", "rgba(255,220,90,0.45)", "rgba(200,140,0,0.4)"],
  sol: ["rgba(20,180,140,0.6)", "rgba(120,220,180,0.45)", "rgba(0,150,120,0.4)"],
}
export const coinGradient = (symbol) =>
  COIN_GRADIENTS[symbol.replace(/usdt$/, "")] || COIN_GRADIENTS.btc

export const PATTERN_META = {
  hammer: {
    name: "锤子线", dir: "bullish", bars: 1,
    desc: "下跌末端出现长下影小实体，买盘承接有力，低位出现预示后市走强。",
    candles: [[88, 80], [80, 73], [60, 68, 72, 28]],
  },
  hanging_man: {
    name: "上吊线", dir: "bearish", bars: 1,
    desc: "上涨高位出现长下影小实体，抛压开始显现，警惕见顶回落。",
    candles: [[58, 66], [66, 73], [63, 71, 75, 30]],
  },
  inverted_hammer: {
    name: "倒锤子", dir: "bullish", bars: 1,
    desc: "下跌末端长上影试盘，多头试探性反攻，低位出现偏多。",
    candles: [[88, 80], [80, 73], [62, 70, 92, 58]],
  },
  shooting_star: {
    name: "流星线", dir: "bearish", bars: 1,
    desc: "上涨末端冲高回落留下长上影，空头反扑，高位偏空。",
    candles: [[58, 66], [66, 73], [62, 70, 92, 58]],
  },
  doji: {
    name: "十字星", dir: "neutral", bars: 1,
    desc: "开盘价与收盘价几乎持平：多空力量完全打平的停滞信号。它本身不指示方向，意思是「变盘在前」——趋势走到这里动能耗尽，接下来大概率选择方向；出现在大涨后偏看跌、大跌后偏看涨，需等下一根K线确认。",
    candles: [[55, 63], [63, 71], [66, 66.4, 86, 48]],
  },
  dragonfly_doji: {
    name: "蜻蜓十字", dir: "bullish", bars: 1,
    desc: "低位长下影十字，探底后全面收回，下方支撑坚实，偏多。",
    candles: [[70, 63], [63, 56], [55, 55.5, 84, 28]],
  },
  gravestone_doji: {
    name: "墓碑十字", dir: "bearish", bars: 1,
    desc: "高位长上影十字，冲高后被全面砸回，上方压力沉重，偏空。",
    candles: [[55, 63], [63, 70], [70, 70.5, 92, 50]],
  },
  bullish_engulfing: {
    name: "看涨吞没", dir: "bullish", bars: 2,
    desc: "阳线实体完全包住前一根阴线，多头强势反攻，偏多。",
    candles: [[86, 79], [80, 72], [71, 84, 88, 66]],
  },
  bearish_engulfing: {
    name: "看跌吞没", dir: "bearish", bars: 2,
    desc: "阴线实体完全包住前一根阳线，空头全面接管，偏空。",
    candles: [[58, 66], [66, 73], [73, 60, 78, 55]],
  },
  piercing: {
    name: "刺透线", dir: "bullish", bars: 2,
    desc: "低开后阳线刺入前阴实体过半，多头收复失地，偏多。",
    candles: [[86, 79], [79, 72], [60, 76, 80, 56]],
  },
  dark_cloud: {
    name: "乌云盖顶", dir: "bearish", bars: 2,
    desc: "高开后阴线吞掉前阳实体过半，空头大力打压，偏空。",
    candles: [[58, 66], [66, 73], [76, 60, 80, 54]],
  },
  bullish_harami: {
    name: "看涨孕线", dir: "bullish", bars: 2,
    desc: "大阴线体内孕出小实体，下跌动能衰竭，观望后偏多。",
    candles: [[88, 78], [70, 64, 76, 58]],
  },
  bearish_harami: {
    name: "看跌孕线", dir: "bearish", bars: 2,
    desc: "大阳线体内孕出小实体，上涨动能衰竭，观望后偏空。",
    candles: [[58, 68], [68, 74, 78, 62]],
  },
  tweezer_bottom: {
    name: "平头底部", dir: "bullish", bars: 2,
    desc: "两根K线低点几乎相同，下方支撑得到确认，偏多。",
    candles: [[86, 78], [79, 71, 73, 56], [70, 79, 76, 56]],
  },
  tweezer_top: {
    name: "平头顶部", dir: "bearish", bars: 2,
    desc: "两根K线高点几乎相同，上方压力得到确认，偏空。",
    candles: [[58, 66], [66, 74, 78, 60], [75, 66, 78, 60]],
  },
  morning_star: {
    name: "启明星", dir: "bullish", bars: 3,
    desc: "阴线、星线、阳线三段结构，多头夺回失地，见底回升。",
    candles: [[86, 78], [65, 60, 67, 55], [57, 72, 76, 54]],
  },
  evening_star: {
    name: "黄昏星", dir: "bearish", bars: 3,
    desc: "阳线、星线、阴线三段结构，空头发起反攻，见顶回落。",
    candles: [[58, 66], [68, 73, 75, 61], [74, 60, 78, 58]],
  },
  three_white_soldiers: {
    name: "三白兵", dir: "bullish", bars: 3,
    desc: "三连阳逐级抬升，多头持续进攻，上升趋势确立。",
    candles: [[56, 64], [64, 72], [72, 80, 84, 58]],
  },
  three_black_crows: {
    name: "三只乌鸦", dir: "bearish", bars: 3,
    desc: "三连阴逐级下探，空头步步紧逼，下跌趋势确立。",
    candles: [[80, 72], [72, 64], [64, 56, 84, 52]],
  },

  /* ------ 结构形态（跨多根K线；K线上会自动画出轮廓/颈线/目标位） ------ */
  head_shoulders_bottom: {
    name: "头肩底", dir: "bullish", bars: 30, structural: true,
    desc: "下跌后左肩、更低的头、右肩依次出现；收盘突破颈线即确认底部反转，目标为头部到颈线的等距上移。",
    candles: [[92, 88], [88, 76], [76, 84], [84, 62], [62, 80], [80, 72], [72, 78], [78, 94], [94, 104]],
  },
  head_shoulders_top: {
    name: "头肩顶", dir: "bearish", bars: 30, structural: true,
    desc: "上涨后左肩、更高的头、右肩依次出现；收盘跌破颈线确认顶部反转，目标为头部到颈线的等距下移。",
    candles: [[10, 16], [16, 28], [28, 20], [20, 40], [40, 22], [22, 30], [30, 24], [24, 12], [12, 4]],
  },
  triple_bottom: {
    name: "三重底", dir: "bullish", bars: 26, structural: true,
    desc: "三次下探同一支撑位均获承接，三个低点相近；突破两高点连成的颈线后确认反转。",
    candles: [[92, 86], [86, 70], [70, 82], [82, 70], [70, 82], [82, 70], [70, 88], [88, 100]],
  },
  triple_top: {
    name: "三重顶", dir: "bearish", bars: 26, structural: true,
    desc: "三次冲击同一压力位均被打回，三个高点相近；跌破颈线后确认见顶，下跌空间约等于顶部到颈线距离。",
    candles: [[10, 16], [16, 32], [32, 20], [20, 32], [32, 20], [20, 32], [32, 14], [14, 2]],
  },
  double_bottom: {
    name: "双底", dir: "bullish", bars: 18, structural: true,
    desc: "两次探底到相近价位后拉起，W 形；收盘站上中间反弹高点（颈线）确认反转。",
    candles: [[92, 84], [84, 68], [68, 82], [82, 68], [68, 86], [86, 98]],
  },
  double_top: {
    name: "双顶", dir: "bearish", bars: 18, structural: true,
    desc: "两次冲高到相近价位后回落，M 形；收盘跌破中间低点（颈线）确认见顶。",
    candles: [[10, 20], [20, 36], [36, 22], [22, 36], [36, 18], [18, 6]],
  },
  ascending_triangle: {
    name: "上升三角形", dir: "bullish", bars: 28, structural: true,
    desc: "高点连成水平压力线、低点逐级抬高，多方持续施压；突破水平压力线后通常延续上涨。",
    candles: [[86, 78], [78, 68], [68, 80], [80, 72], [72, 81], [81, 76], [76, 82], [82, 96]],
  },
  descending_triangle: {
    name: "下降三角形", dir: "bearish", bars: 28, structural: true,
    desc: "低点连成水平支撑线、高点逐级降低，空方持续施压；跌破水平支撑线后通常延续下跌。",
    candles: [[14, 22], [22, 32], [32, 20], [20, 30], [30, 19], [19, 26], [26, 18], [18, 4]],
  },
  symmetrical_triangle: {
    name: "对称三角形", dir: "neutral", bars: 28, structural: true,
    desc: "高点降低、低点抬高，波动逐级收敛，多空暂时均衡；突破哪一侧，方向就是哪边。",
    candles: [[20, 30], [30, 42], [42, 26], [26, 38], [38, 30], [30, 36], [36, 32], [32, 44]],
  },
  rising_wedge: {
    name: "上升楔形", dir: "bearish", bars: 28, structural: true,
    desc: "高低点同步上移但间距收窄，上涨动能衰竭的典型形态；跌破下边界后往往快速回落。",
    candles: [[88, 80], [80, 86], [86, 74], [74, 84], [84, 76], [76, 82], [82, 78], [78, 66]],
  },
  falling_wedge: {
    name: "下降楔形", dir: "bullish", bars: 28, structural: true,
    desc: "高低点同步下移但间距收窄，下跌动能衰竭；突破上边界后往往快速反弹。",
    candles: [[12, 20], [20, 14], [14, 26], [26, 16], [16, 24], [24, 18], [18, 22], [22, 34]],
  },
}
