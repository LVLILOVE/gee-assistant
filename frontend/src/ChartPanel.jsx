/** 统计图表面板：ECharts 渲染折线 / 柱状 / 饼图。
 *
 * 独立成文件是为了让 echarts（约 1MB，是最大的一块依赖）**延迟加载** ——
 * 它只在用户看到任务结果时才需要，登录页与应用外壳没必要先下载。
 * 由 App.jsx 通过 `React.lazy(() => import('./ChartPanel'))` 引入。
 */
import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'

export default function ChartPanel({ chart }) {
  const ref = useRef(null)

  useEffect(() => {
    if (!ref.current || !chart) return
    const c = echarts.init(ref.current)
    let option
    if (chart.kind === 'pie') {
      option = {
        tooltip: { trigger: 'item' },
        legend: { bottom: 0 },
        series: [
          {
            type: 'pie',
            radius: '62%',
            data: (chart.labels || []).map((l, i) => ({
              name: l,
              value: chart.series[0]?.data[i] ?? 0,
            })),
          },
        ],
      }
    } else if (chart.kind === 'bar') {
      option = {
        tooltip: { trigger: 'axis' },
        grid: { left: 48, right: 20, top: 30, bottom: 30 },
        xAxis: { type: 'category', data: chart.labels },
        yAxis: { type: 'value' },
        series: (chart.series || []).map((s) => ({ type: 'bar', name: s.name, data: s.data })),
      }
    } else {
      option = {
        tooltip: { trigger: 'axis' },
        grid: { left: 48, right: 20, top: 30, bottom: 30 },
        xAxis: { type: 'category', data: chart.labels },
        yAxis: { type: 'value' },
        series: (chart.series || []).map((s) => ({
          type: 'line',
          name: s.name,
          data: s.data,
          smooth: true,
        })),
      }
    }
    c.setOption(option)
    const onResize = () => c.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      c.dispose()
    }
  }, [chart])

  return <div className="chart-container" ref={ref} />
}
