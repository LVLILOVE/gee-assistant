/** 统计图表面板：ECharts 渲染折线 / 柱状 / 饼图。
 *
 * 独立成文件是为了让 echarts（约 1MB，是最大的一块依赖）**延迟加载** ——
 * 它只在用户看到任务结果时才需要，登录页与应用外壳没必要先下载。
 * 由 App.jsx 通过 `React.lazy(() => import('./ChartPanel'))` 引入。
 */
import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'
import { chartPalette, palette } from './theme'

export default function ChartPanel({ chart }) {
  const ref = useRef(null)

  useEffect(() => {
    if (!ref.current || !chart) return
    const c = echarts.init(ref.current)

    // 坐标轴与网格文字统一走令牌，避免 ECharts 默认的偏灰蓝色调破坏主题一致性。
    const axisCommon = {
      axisLine: { lineStyle: { color: palette.line } },
      axisTick: { show: false },
      axisLabel: { color: palette.ink700, fontSize: 11 },
      splitLine: { lineStyle: { color: palette.line, type: 'dashed' } },
    }

    let option
    if (chart.kind === 'pie') {
      option = {
        // 显式指定色序。ECharts 默认色序里相邻两色的灰度值可能接近，
        // 色觉障碍用户会分不清；同时**保留下方图例与数值标签**，
        // 保证不依赖颜色也能读出占比（不只靠色相表意）。
        color: chartPalette,
        tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
        legend: {
          bottom: 0,
          icon: 'circle',
          itemWidth: 8,
          itemHeight: 8,
          textStyle: { color: palette.ink700, fontSize: 11 },
        },
        series: [
          {
            type: 'pie',
            radius: '62%',
            data: (chart.labels || []).map((l, i) => ({
              name: l,
              value: chart.series[0]?.data[i] ?? 0,
            })),
            label: { color: palette.ink900, fontSize: 11 },
            labelLine: { lineStyle: { color: palette.line } },
            itemStyle: { borderColor: palette.paper, borderWidth: 2 },
          },
        ],
      }
    } else if (chart.kind === 'bar') {
      option = {
        color: chartPalette,
        tooltip: { trigger: 'axis' },
        grid: { left: 48, right: 20, top: 30, bottom: 30 },
        xAxis: { type: 'category', data: chart.labels, ...axisCommon },
        yAxis: { type: 'value', ...axisCommon },
        series: (chart.series || []).map((s) => ({
          type: 'bar',
          name: s.name,
          data: s.data,
          itemStyle: { borderRadius: [3, 3, 0, 0] },
          barMaxWidth: 36,
        })),
      }
    } else {
      option = {
        color: chartPalette,
        tooltip: { trigger: 'axis' },
        grid: { left: 48, right: 20, top: 30, bottom: 30 },
        xAxis: { type: 'category', data: chart.labels, ...axisCommon },
        yAxis: { type: 'value', ...axisCommon },
        series: (chart.series || []).map((s) => ({
          type: 'line',
          name: s.name,
          data: s.data,
          smooth: true,
          // 数据点多于 2 个才画面积，否则视觉噪音大于信息量
          areaStyle:
            (s.data?.length ?? 0) > 2
              ? { color: palette.moss400, opacity: 0.16 }
              : undefined,
          lineStyle: { width: 2 },
          symbol: 'circle',
          symbolSize: 6,
          // 缺测月份不要跨点连线，否则会假装出并不存在的趋势
          connectNulls: false,
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
