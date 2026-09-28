/** ChartPanel 的行为测试。
 *
 * 对应第一巡的 F9：饼图分支写成 `chart.series[0]?.data[i]` —— 只对 `[0]` 之后
 * 加了可选链。只要 `chart.series` 本身是 undefined 就会先抛 TypeError，
 * 而**项目没有 ErrorBoundary** → React 把整棵结果树卸载 → 整个右栏白屏。
 *
 * 这条缺陷的要害是"**崩得毫无提示**"，所以测试的判据不是"数值对不对"，
 * 而是"**喂进畸形数据也不许抛异常**"。畸形数据在真实场景里拿得到：
 * 后端换版本、图表类型新增分支、模型生成的 stats 结构变化，都会产生它。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'

// vi.hoisted：vi.mock 的工厂函数会被提升到 import 之前，
// 直接引用普通 const 会撞 "Cannot access before initialization"。
const h = vi.hoisted(() => ({ options: [], disposed: 0, inits: 0 }))

vi.mock('echarts', () => ({
  init: () => {
    h.inits += 1
    return {
      setOption: (o) => h.options.push(o),
      resize: () => {},
      dispose: () => {
        h.disposed += 1
      },
    }
  },
}))

import ChartPanel from '../src/ChartPanel.jsx'

const last = () => h.options[h.options.length - 1]

beforeEach(() => {
  h.options = []
  h.disposed = 0
  h.inits = 0
})
afterEach(cleanup)

describe('ChartPanel · F9 畸形数据不许白屏', () => {
  it('饼图缺 series 时不抛异常，数值回落为 0（而不是整块右栏卸载）', () => {
    // 这是修复前**必崩**的输入：chart.series 为 undefined
    expect(() =>
      render(<ChartPanel chart={{ kind: 'pie', title: '占比', labels: ['水体', '其他'] }} />),
    ).not.toThrow()

    const data = last().series[0].data
    expect(data).toEqual([
      { name: '水体', value: 0 },
      { name: '其他', value: 0 },
    ])
  })

  it('饼图 series 为空数组 / 内层 data 缺失时同样不抛', () => {
    expect(() =>
      render(<ChartPanel chart={{ kind: 'pie', labels: ['a'], series: [] }} />),
    ).not.toThrow()
    expect(last().series[0].data).toEqual([{ name: 'a', value: 0 }])

    cleanup()
    expect(() =>
      render(<ChartPanel chart={{ kind: 'pie', labels: ['a'], series: [{ name: 'x' }] }} />),
    ).not.toThrow()
    expect(last().series[0].data).toEqual([{ name: 'a', value: 0 }])
  })

  it('饼图缺 labels 时也不抛（标签是后端给的，不是必然存在）', () => {
    expect(() => render(<ChartPanel chart={{ kind: 'pie' }} />)).not.toThrow()
    expect(last().series[0].data).toEqual([])
  })

  it('饼图正常数据：数值与图例都到位（不依赖颜色也能读出占比）', () => {
    render(
      <ChartPanel
        chart={{ kind: 'pie', labels: ['水体', '陆地'], series: [{ name: '占比', data: [10, 20] }] }}
      />,
    )
    const opt = last()
    expect(opt.series[0].data).toEqual([
      { name: '水体', value: 10 },
      { name: '陆地', value: 20 },
    ])
    // 图例 + 数据标签是"不只靠色相表意"的保证，不能被顺手删掉
    expect(opt.legend).toBeTruthy()
    expect(opt.tooltip.formatter).toContain('{d}%')
  })
})

describe('ChartPanel · 时序图的诚实性', () => {
  const line = (data) => ({
    kind: 'line',
    title: 'NDVI 逐月',
    labels: ['1月', '2月', '3月'],
    series: [{ name: 'NDVI', data }],
  })

  it('缺测月份不跨点连线（connectNulls=false），否则会假装出并不存在的趋势', () => {
    render(<ChartPanel chart={line([0.3, null, 0.5])} />)
    expect(last().series[0].connectNulls).toBe(false)
  })

  it('数据点 <= 2 时不画面积（视觉噪音大于信息量）', () => {
    render(<ChartPanel chart={line([0.3, 0.4])} />)
    expect(last().series[0].areaStyle).toBeUndefined()
  })

  it('数据点 > 2 时才画面积', () => {
    render(<ChartPanel chart={line([0.3, 0.4, 0.5])} />)
    expect(last().series[0].areaStyle).toBeTruthy()
  })

  it('柱状图缺 series 时不抛', () => {
    expect(() => render(<ChartPanel chart={{ kind: 'bar', labels: ['a'] }} />)).not.toThrow()
    expect(last().series).toEqual([])
  })
})

describe('ChartPanel · 生命周期', () => {
  it('卸载时 dispose 实例（ECharts 实例不释放会持续占内存与监听）', () => {
    const { unmount } = render(<ChartPanel chart={{ kind: 'line', labels: [], series: [] }} />)
    expect(h.inits).toBe(1)
    expect(h.disposed).toBe(0)
    unmount()
    expect(h.disposed).toBe(1)
  })

  it('chart 为空时不初始化实例（避免建一个永远没数据的空图）', () => {
    render(<ChartPanel chart={null} />)
    expect(h.inits).toBe(0)
  })
})
