/** 前端单测配置。
 *
 * 【为什么要单独一个文件，而不是往 vite.config.js 里加 `test:` 段】
 * vite.config.js 是**构建产物**的输入（分包规则、chunk 命名都在里面，
 * 线上 dist 的哈希由它决定）。构建是已经验证过的链路，测试基建不该有机会
 * 影响它 —— 分开之后，改测试配置永远不可能改到构建行为。
 *
 * 环境选 jsdom：本项目前端缺陷**绝大多数不是"没渲染出来"，而是
 * "渲染出来了但行为不对"**（轮询链断掉、旧结果覆盖新结果、文案指了不存在的
 * 控件、告警正文里漏出 Markdown 星号）。这类问题只有把组件真渲染一遍、
 * 再看 DOM 与回调才能钉住，光扫源码是钉不住的（见 backend/tools/
 * test_jsx_markdown_leak.py 的注释：grep 在本仓库里全是噪音）。
 *
 * ⚠️ 这里**不引入 @testing-library/jest-dom**：断言只用 vitest 原生的
 * expect 与 DOM API（`el.textContent` / `queryByRole`），少一层依赖、
 * 少一处版本兼容面。
 */
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['test/**/*.test.{js,jsx}'],
    setupFiles: ['./test/setup.js'],
    // antd 首次渲染（含 message 的 Portal 初始化）比纯组件慢，默认 5s 偶发不够。
    testTimeout: 20000,
    // 关掉并发写同一个 jsdom 时的相互干扰：这里没有共享全局状态的场景，
    // 但 antd 的 message 是模块级单例，多文件并行时断言会互相看到对方的提示。
    fileParallelism: false,
  },
})
