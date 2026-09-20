import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './style.css'

// 注意：leaflet 的样式**不要**在这里 import。
// main.jsx 是入口，任何在这里静态 import 的东西都会被 Vite 放进首屏并 modulepreload ——
// 包括它所属的 vendor-leaflet chunk（约 150KB）。挪到 MapPanel.jsx 后，
// leaflet 的 JS 与 CSS 都只在真正渲染地图时才下载。

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
