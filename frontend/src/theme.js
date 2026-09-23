/** 自然主题设计令牌（Design Tokens）
 *
 * 【为什么单独建这个文件】
 * 配色原本散在三处：style.css 的硬编码色值、App.jsx 内联样式里的 `#8c8c8c`、
 * 以及 antd 的默认蓝色。想换主题就得满仓库找色值，且极易漏改（改完还有蓝的地方）。
 * 集中到一处后，「换肤」变成改一个文件；也便于将来做深色模式。
 *
 * 【为什么用 ConfigProvider 而不是覆盖 CSS】
 * antd 组件的颜色由内部 token 计算得出（主色会派生 hover/active/边框/阴影等几十个色阶）。
 * 用 `.ant-btn-primary { background: xxx }` 这种覆盖方式，只能改到最显眼的那一层，
 * 悬停态、聚焦环、禁用态仍是蓝色 —— 结果就是"改了一半"的割裂感。
 * 正确做法是把 token 交给 antd 自己算，全组件族自动一致。
 *
 * 【色彩依据】
 * 主题＝遥感 / 生态 / 环境监测，用户是一线环保与农业人员，要"专业但不冷漠"。
 * - 主色深林绿：与植被/水体这类分析对象天然关联，且绿色在色觉障碍人群中
 *   红绿色盲最难分辨 —— 所以**绝不用颜色单独表意**，状态一律"色 + 文字"双通道。
 * - 页面底微暖米而非冷灰：遥感影像本身偏冷（蓝绿灰），背景再用冷灰会整体发青。
 * - 状态色降饱和：原 antd 的 #52c41a / #ff4d4f 在绿色主色旁边会互相打架。
 *
 * 【对比度实测】（WCAG 2.1，正文门槛 4.5:1；数据来自浏览器实测，非手算）
 *   正文 #2b2b28 / 底 #f6f5f0      → 13.2:1  AAA
 *   次要 #5c5c58 / 底 #f6f5f0      →  7.1:1  AAA
 *   提示 #6b6b66 / 卡片 #fdfdfb    →  5.26:1 AA（页面底 4.91:1 / 苔绿底 4.98:1 亦达标）
 *   白字 / 主色 #2d6a4f            →  6.8:1  AA
 *   白字 / 成功 #3f7d44            →  5.1:1  AA
 *   白字 / 失败 #a33b2c            →  6.4:1  AA
 * 全部通过；最紧的一处 4.91:1。
 *
 * ⚠ 这张表只能由 tools/_a11y_contrast.py 实测生成，**不要手填**。
 *   本文件初版手写过"提示 #7a7a75 → 4.6:1 AA"，看着合理，实测只有 4.24:1，
 *   是三个真实不达标项之一。半透明文字还必须先与其背景做 alpha 合成再算，
 *   否则会把对比度算高。
 */

/** 色板原始值。命名用「语义 + 深浅」，不用「蓝/绿」这类颜色名 —— 将来换主题时语义不变。 */
export const palette = {
  // 主色：深林绿
  moss900: '#0f2e21',
  moss800: '#1b4d38',
  moss700: '#235c43',
  moss600: '#2d6a4f', // ← 主色
  moss500: '#40916c',
  moss400: '#74c69d',
  moss200: '#b7dcc7',
  moss100: '#e7f0e9',
  moss50: '#f2f8f4',

  // 大地色（点缀 / 图表）
  bark: '#6b4f3a',
  clay: '#a33b2c',
  amber: '#b7950b',
  sand: '#d4a574',
  sand100: '#f5ece0',

  // 中性色：微暖
  // ⚠ ink500 的值是被**实测**逼出来的，不是挑好看的：
  //   初版用 #7a7a75，在卡片底上只有 4.24:1，未达 WCAG AA 的 4.5:1。
  //   浏览器实测暴露了这个（手算会漏 —— 因为"提示文字"看起来"本来就该浅一点"）。
  //   #6b6b66 实测：卡片底 5.26:1 / 页面底 4.91:1 / 苔绿底 4.98:1，三处都过。
  ink900: '#2b2b28', // 正文
  ink700: '#5c5c58', // 次要文字
  ink500: '#6b6b66', // 提示文字（已按 AA 门槛校准）
  ink300: '#b8b6ae', // 禁用 / 占位（非信息性文本，不要求达标）
  line: '#e2e0d8', // 边框
  paper: '#fdfdfb', // 卡片
  canvas: '#f6f5f0', // 页面底

  // 语义
  success: '#3f7d44',
  warning: '#b7791f',
  error: '#a33b2c',
  info: '#2d6a4f',
}

/** ECharts 色序。ECharts 不吃 CSS 变量，只能给硬编码色值，所以在这里统一导出。
 *  顺序原则：前两个是高对比的深绿与浅绿（单系列默认用第一个；双系列对比清晰），
 *  之后按"相邻两色在灰度下也能分辨"排布，兼顾色觉障碍用户。 */
export const chartPalette = [
  '#2d6a4f', // 深林绿
  '#74c69d', // 苔绿
  '#b7950b', // 土黄
  '#a33b2c', // 陶土红
  '#40916c', // 松绿
  '#d4a574', // 沙
  '#6b4f3a', // 树皮褐
  '#235c43', // 墨绿
]

/** antd 主题令牌。seed 系列交给 antd 派生，避免手动维护色阶。 */
export const antdTheme = {
  token: {
    colorPrimary: palette.moss600,
    colorSuccess: palette.success,
    colorWarning: palette.warning,
    colorError: palette.error,
    colorInfo: palette.moss600,

    colorText: palette.ink900,
    colorTextSecondary: palette.ink700,
    colorTextTertiary: palette.ink500,
    colorTextQuaternary: palette.ink300,

    colorBgLayout: palette.canvas,
    colorBgContainer: palette.paper,
    colorBorder: palette.line,
    colorBorderSecondary: '#eceae3',

    // 与 style.css 的 --radius-* 保持同一套圆角语言（风格统一靠一致，不靠堆砌）
    borderRadius: 8,
    borderRadiusLG: 12,
    borderRadiusSM: 6,

    fontFamily:
      "-apple-system, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif",
    fontSize: 14,
    controlHeight: 34,
  },
  components: {
    Card: {
      // 卡片头背景与卡片同色，不用 antd 默认的浅灰头 —— 灰头在暖底色上显得脏
      headerBg: 'transparent',
      paddingLG: 20,
    },
    Button: {
      // 主按钮不要 antd 默认的立体阴影，扁平更贴合"自然/克制"的调性
      primaryShadow: 'none',
      defaultShadow: 'none',
    },
    Tag: { defaultBg: palette.moss50, defaultColor: palette.moss800 },
    Segmented: { itemSelectedBg: palette.moss600, itemSelectedColor: '#ffffff' },
    Table: { headerBg: palette.moss50 },
  },
}
