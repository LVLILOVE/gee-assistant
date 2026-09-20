import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8010',
      '/health': 'http://127.0.0.1:8010',
    },
  },
  build: {
    // 默认把 node_modules 全打进一个 index-*.js（2MB+）。分开的好处：
    //   1. 改业务代码不会让用户重下整个 vendor（cpolar 免费隧道带宽有限，这点很实在）；
    //   2. 浏览器能并行下载多个 chunk；
    //   3. leaflet / echarts 本来就被 App 里的 React.lazy 延迟加载，单独成 chunk 才真正生效。
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          // 顺序重要：@ant-design/icons 里也含 "antd" 之外的 ant- 前缀，先判 antd 家族
          if (id.includes('echarts') || id.includes('zrender')) return 'vendor-echarts'
          if (id.includes('leaflet')) return 'vendor-leaflet'
          if (
            id.includes('antd') ||
            id.includes('@ant-design') ||
            id.includes('rc-') ||
            id.includes('@rc-component')
          ) {
            return 'vendor-antd'
          }
          if (id.includes('react') || id.includes('scheduler')) return 'vendor-react'
          return 'vendor'
        },
        chunkFileNames: 'assets/[name]-[hash].js',
        entryFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
    // 单个 chunk 超过 600KB 才警告（echarts 自身就接近 1MB，属预期）
    chunkSizeWarningLimit: 1100,
  },
})
