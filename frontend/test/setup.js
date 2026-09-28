/** jsdom 缺少的浏览器 API 补齐。
 *
 * jsdom 只实现 DOM，不实现布局与渲染 —— 下面这些都是 antd / Leaflet / ECharts
 * 在挂载时就会碰到、而 jsdom 里不存在的接口。缺一个的表现都是
 * "测试直接崩在某个第三方库内部"，报错完全指不到自己的代码，很难查。
 * 所以统一在这里补齐，并逐条写明**谁需要它**。
 */

// React 18 的 act() 环境标记。@testing-library/react 通常会自己设，
// 但显式设上是幂等的，且能避免"act 警告刷屏把真失败淹掉"。
globalThis.IS_REACT_ACT_ENVIRONMENT = true

// ---- antd：响应式断点判断（Grid/Row/Col、Collapse 的 size 都用它）----
if (!window.matchMedia) {
  window.matchMedia = (query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent: () => false,
  })
}

// ---- antd（rc-resize-observer）+ 本项目的容器尺寸校正 ----
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}

// ---- ECharts：即便测试里 mock 掉 echarts，jsdom 自身也不提供 canvas 2d 上下文。
//      真去取会返回 null，依赖它的代码会抛在"取上下文"那一行，误导定位。----
if (!HTMLCanvasElement.prototype.getContext) {
  HTMLCanvasElement.prototype.getContext = () => null
}

// ---- Leaflet 初始化时会测量容器 / 调用 scrollTo ----
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {}
}
if (!window.scrollTo) {
  window.scrollTo = () => {}
}
