import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'
import { antdTheme } from './theme'
import './style.css'

// 注意：leaflet 的样式**不要**在这里 import。
// main.jsx 是入口，任何在这里静态 import 的东西都会被 Vite 放进首屏并 modulepreload ——
// 包括它所属的 vendor-leaflet chunk（约 150KB）。挪到 MapPanel.jsx 后，
// leaflet 的 JS 与 CSS 都只在真正渲染地图时才下载。
//
// 同理：主题令牌在 theme.js 里是**纯数据**（几十行对象），import 它不会拖进任何依赖，
// 对首屏体积的影响可忽略。

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    {/* 自然主题：主色改为深林绿。用 ConfigProvider 注入而不是覆盖 CSS ——
        antd 会据此重新派生 hover / active / 边框 / 聚焦环等全部色阶，
        避免"改了主色但悬停态还是蓝色"的半吊子效果。
        locale 一并设为 zh_CN：日期选择器、分页、空状态此前是英文。 */}
    <ConfigProvider theme={antdTheme} locale={zhCN}>
      <App />
    </ConfigProvider>
  </React.StrictMode>,
)
