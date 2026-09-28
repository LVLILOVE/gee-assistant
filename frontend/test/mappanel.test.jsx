/** MapPanel 的行为测试。
 *
 * 【为什么先补它】
 * 这个文件承载了本项目**历史上最贵的一次事故**：问"恩施大峡谷"，地图显示苏州。
 * 后端数据全对，只是前端把视口开在太湖 —— 用户因此判定"分析错了地方"。
 * 第一巡又在这里发现 3 处缺陷（F8 地图泄漏 / F10 瓦片失败完全静默 / F11 指纹太粗）。
 * 本次第三巡还在这里发现「提示让用户点『重新运行』但控件不存在」（T6）。
 *
 * 【测的是行为契约，不是渲染效果】
 * Leaflet 在 jsdom 里没有布局尺寸，算不出真实缩放（见 leaflet-mock.js 的说明）。
 * 所以这里断言的是"**调了哪个方法、传了什么**"，这些是可信的；
 * 而"定位准不准"由无头浏览器在真页面上核验（backend/tools/test_region_map_fallback.py）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor, act } from '@testing-library/react'

import fakeLeaflet, { registry, boxOf } from './leaflet-mock.js'

// 用替身取代真 leaflet。注意：MapPanel 里 `import 'leaflet/dist/leaflet.css'`
// 不需要额外处理 —— vitest 默认不处理 CSS，会替换成空模块。
vi.mock('leaflet', async () => {
  const m = await import('./leaflet-mock.js')
  return { default: m.default }
})

import MapPanel from '../src/MapPanel.jsx'

const TILE_URL = 'https://earthengine-highvolume.googleapis.com/v1/abc/tiles/{z}/{x}/{y}'

/** 拿告警框。用 textContent 断言而不用 getByText：
 *  告警正文由多段 JSX 表达式拼成，会被切成多个文本节点，
 *  getByText 的整串匹配会漏（实测：文案里插入 <strong> 之后就匹配不到了）。 */
const warnText = () => {
  const nodes = document.querySelectorAll('.map-warn')
  return [...nodes].map((n) => n.textContent).join(' | ')
}

/** 地图最终停在哪。
 *
 * ⚠️ 不能直接断言 `map.views` 的长度：`L.map(...).setView(initial, ...)` 在**建图时**
 * 就会记一条（这是 MapPanel 的既有写法），定位分支随后还会再设一次。
 * 所以"定位对不对"的判据是**最后一条视野**，以及"有没有出现过别的坐标"。
 * 一开始我按长度断言，三条用例全红 —— 红的是判据，不是产品。 */
const finalView = (map) => map.views[map.views.length - 1]

beforeEach(() => registry.reset())
afterEach(() => {
  cleanup()
  registry.reset()
})

describe('MapPanel · 定位契约（本项目最贵的一次事故就在这块）', () => {
  it('有 bbox 时按真实 AOI fitBounds —— 而不是按区域名猜一个地点', () => {
    const layer = {
      name: 'NDVI 分布',
      kind: 'raster',
      tile_url: TILE_URL,
      bbox: [111.0, 30.0, 113.0, 32.0], // [minLng, minLat, maxLng, maxLat]
    }
    render(<MapPanel layers={[layer]} center={[31.2, 120.1]} />) // center 故意给太湖
    const map = registry.maps[0]

    expect(map.fits).toHaveLength(1)
    expect(map.fits[0].box).toEqual({ minLat: 30, maxLat: 32, minLng: 111, maxLng: 113 })
    // 创建时的初始 setView 只有一次；有 bbox 之后不该再按区域中心重设视野
    expect(map.views).toHaveLength(1)
  })

  it('既没有图层范围也没有区域中心 → 全局视图 + **显式警告**，绝不猜具体地点', () => {
    const layer = { name: '栅格图层', kind: 'raster', tile_url: TILE_URL }
    render(<MapPanel layers={[layer]} center={null} />)
    const map = registry.maps[0]

    expect(map.fits).toHaveLength(0)
    expect(finalView(map)).toEqual({ center: [35, 105], zoom: 4 })
    // 兜底必须落在中性全局视野，且**全程**没有出现过任何别的坐标 ——
    // 原先这里写死太湖 [31.2,120.1]，任何字典外的地名都会被静默带到苏州。
    expect(map.views.every((v) => v.center[0] === 35 && v.center[1] === 105)).toBe(true)

    // 定位不了就必须说出来（错误的自信比明确的失败危险得多）
    expect(warnText()).toContain('未能确定该图层的地理位置')
    expect(warnText()).toContain('该任务未记录分析区域范围')
  })

  it('纯栅格图层没有 bbox 时，用区域中心兜底但不给出"定位不了"的误导提示', () => {
    render(
      <MapPanel layers={[{ name: 'x', kind: 'raster', tile_url: TILE_URL }]} center={[30.5, 114.3]} />,
    )
    const map = registry.maps[0]

    expect(map.views.every((v) => v.center[0] === 30.5 && v.center[1] === 114.3)).toBe(true)
    expect(finalView(map)).toEqual({ center: [30.5, 114.3], zoom: 9 })
    expect(map.fits).toHaveLength(0)
    expect(warnText()).not.toContain('未能确定')
  })

  it('"定位不了"的两种原因要分开说 —— 别让用户按错的提示去查后端', () => {
    // 这里的 bbox 是**长度 4 的合法数组但含非数字**：前端 hasRange 判为真，
    // 却算不出可用范围。此时文案若仍写"该任务未记录分析区域范围"，
    // 用户会去查后端为什么没回传 bbox —— 而后端其实回传了（2026-09-23 实测踩到）。
    const layer = { name: '矢量', kind: 'vector', bbox: [111, 30, 'oops', 32] }
    render(<MapPanel layers={[layer]} center={null} />)

    expect(warnText()).toContain('未能确定该图层的地理位置')
    expect(warnText()).toContain('请在地图上自行查找分析区域')
    expect(warnText()).not.toContain('该任务未记录分析区域范围')
  })
})

describe('MapPanel · 图层刷新（F11 内容指纹 + 头号回退风险）', () => {
  const gj = (shift = 0) => ({
    type: 'FeatureCollection',
    features: [
      {
        type: 'Feature',
        properties: { name: 'A' },
        geometry: {
          type: 'Polygon',
          coordinates: [[[111 + shift, 30], [112 + shift, 30], [112 + shift, 31], [111 + shift, 30]]],
        },
      },
    ],
  })

  it('⭐ 父组件传新数组但内容没变 → effect 不重跑（否则视野会被打回全局视图）', () => {
    // 这是地图定位修复的**头号回退风险**：App 里写的是 layers={[layer]}，
    // 每次渲染都是新数组。若把 layers 直接放进 useEffect 依赖，effect 会重跑；
    // 而"父组件先渲染空态、再渲染结果"的时序会让第二次重跑拿着空图层把刚
    // fitBounds 好的视野覆盖成全局视图 —— 整个定位修复被静默抵消。
    const build = () => [{ name: 'A', kind: 'vector', geojson: gj(0) }]
    const { rerender } = render(<MapPanel layers={build()} />)
    const map = registry.maps[0]
    expect(map.fits).toHaveLength(1)
    const geoAfterFirst = registry.geoJsons.length

    rerender(<MapPanel layers={build()} />) // 内容相同、数组引用不同
    rerender(<MapPanel layers={build()} />)

    expect(map.fits).toHaveLength(1) // 没有被重新定位
    expect(registry.geoJsons.length).toBe(geoAfterFirst) // 也没有重新叠加图层
  })

  it('同区域同任务类型跑两次、矢量内容不同 → 必须重新叠加（不然是"旧图上贴新标题"）', () => {
    const { rerender } = render(<MapPanel layers={[{ name: 'A', kind: 'vector', geojson: gj(0) }]} />)
    const map = registry.maps[0]
    expect(map.fits).toHaveLength(1)

    // name / kind 完全一致，只有坐标不同 —— 原先指纹只记"有没有 geojson"，
    // 于是判成"没变化"，地图上留着上一次的矢量，卡片标题却是新任务的名字。
    rerender(<MapPanel layers={[{ name: 'A', kind: 'vector', geojson: gj(0.5) }]} />)

    expect(registry.geoJsons.length).toBe(2)
    expect(map.fits).toHaveLength(2)
  })
})

describe('MapPanel · 瓦片加载失败必须让用户知道（F10 + T6）', () => {
  const layer = { name: '水体', kind: 'raster', tile_url: TILE_URL, bbox: [111, 30, 113, 32] }

  it('挂了 tileerror 监听，且失败后出现"带生成时间 + 真按钮"的提示', async () => {
    const onRerun = vi.fn()
    const createdAt = 1700000000
    render(
      <MapPanel layers={[layer]} center={null} createdAt={createdAt} onRerun={onRerun} />,
    )

    // ① 监听本身是前提：原先前端完全没有 tileerror 监听，
    //    Leaflet 拿不到图只把瓦片留成灰色，用户看到"一张没有色块的地图"。
    const tl = registry.tileLayers.find((l) => String(l.arg.url).startsWith('/api/tile/'))
    expect(tl).toBeTruthy()
    expect(tl.handlers.tileerror?.length).toBe(1)

    // ② 三次失败 → 提示里要如实说是 3 张（去抖后合并成一条）
    await act(async () => {
      tl.fire('tileerror')
      tl.fire('tileerror')
      tl.fire('tileerror')
    })
    await waitFor(() => expect(warnText()).toContain('3 张瓦片加载失败'))
    expect(warnText()).toContain('水体')
    expect(warnText()).toContain(new Date(createdAt * 1000).toLocaleString())

    // ③ 提示里让用户点的动作**必须真的存在**（T6：原先文案写"点「重新运行」"，
    //    而全前端根本没有这个控件 —— 用户会去找、找不到，然后认为界面坏了）
    const btn = screen.getByRole('button', { name: /重新运行/ })
    fireEvent.click(btn)
    expect(onRerun).toHaveBeenCalledTimes(1)
  })

  it('⚠️ 告警正文里不许漏出 Markdown 星号（T6b：JSX 文本节点不解析 Markdown）', async () => {
    render(<MapPanel layers={[layer]} center={null} createdAt={1700000000} onRerun={() => {}} />)
    const tl = registry.tileLayers.find((l) => String(l.arg.url).startsWith('/api/tile/'))
    await act(async () => {
      tl.fire('tileerror')
    })
    await waitFor(() => expect(warnText()).toContain('张瓦片加载失败'))

    // 真实渲染出来的文本里出现 `**` 就是把 Markdown 语法写进了 JSX 正文，
    // 用户在屏幕上会原样看到星号。这条只有"真渲染"才测得出来 ——
    // 扫源码是看不出的（本仓库注释里到处是 `**`，grep 全是噪音）。
    expect(warnText()).not.toContain('**')
    expect(document.querySelector('.map-warn').innerHTML).toContain('<strong>')
  })

  it('换个图层后必须重置上一轮的失败计数（旧警告不能继续挂着）', async () => {
    const { rerender } = render(
      <MapPanel layers={[layer]} center={null} createdAt={1700000000} onRerun={() => {}} />,
    )
    const tl = registry.tileLayers.find((l) => String(l.arg.url).startsWith('/api/tile/'))
    await act(async () => {
      tl.fire('tileerror')
    })
    await waitFor(() => expect(warnText()).toContain('1 张瓦片加载失败'))

    rerender(
      <MapPanel
        layers={[{ ...layer, bbox: [112, 30, 114, 32] }]}
        center={null}
        createdAt={1700000000}
        onRerun={() => {}}
      />,
    )
    expect(warnText()).not.toContain('张瓦片加载失败')
  })
})

describe('MapPanel · 瓦片地址必须走后端代理', () => {
  it('GEE 模板改写成 /api/tile 代理地址；识别不出的模板**绝不退回裸地址**', () => {
    const good = { name: 'g', kind: 'raster', tile_url: TILE_URL, bbox: [111, 30, 113, 32] }
    const bad = { name: 'bad', kind: 'raster', tile_url: 'https://earthengine.googleapis.com/v1/x/tiles', bbox: [111, 30, 113, 32] }
    render(<MapPanel layers={[good, bad]} center={null} />)

    // 底图也必须是代理地址：本机浏览器直连 OSM 是 HTTP 000
    expect(registry.tileLayers[0].arg.url).toBe('/api/basemap/{z}/{x}/{y}')
    // 业务图层的 GEE 模板 → 代理
    expect(registry.tileLayers[1].arg.url).toBe(
      `/api/tile/{z}/{x}/{y}?u=${encodeURIComponent(
        'https://earthengine-highvolume.googleapis.com/v1/abc/tiles',
      )}`,
    )
    // 只有两条：格式不认识的那个**没有**被建出来。
    // 塞裸地址在这台机器上必然连通失败 → 地图静默空白，比明确提示更糟。
    expect(registry.tileLayers).toHaveLength(2)
    expect(warnText()).toContain('无法识别')
    expect(warnText()).toContain('bad')
  })
})

describe('MapPanel · 资源释放与显示参数', () => {
  it('F8：卸载时必须 map.remove()，且此后不再对这张地图做任何操作', async () => {
    // 不销毁的话，L.map 挂在 window 上的 resize 监听不会解绑，
    // 每切换一次任务就泄漏一张活地图（DOM 容器、瓦片层、监听全留着）。
    const { unmount } = render(<MapPanel layers={[]} center={null} />)
    const map = registry.maps[0]
    expect(map.removed).toBe(0)

    unmount()
    expect(map.removed).toBe(1)

    // 卸载后延迟校正的定时器必须失效：否则会作用在一张已被 remove() 的地图上
    await new Promise((r) => setTimeout(r, 120))
    expect(map.invalidated).toBe(0)
  })

  it('T7：zoomSnap 必须是 0.25（默认 1 会把 fitBounds 的小数缩放向下取整）', () => {
    render(<MapPanel layers={[]} center={null} />)
    expect(registry.mapCalls[0].opts).toEqual({ zoomSnap: 0.25 })
  })

  it('没有图层时也要把地图建起来（否则结果区会留一块空白容器）', () => {
    render(<MapPanel layers={[]} center={null} />)
    expect(registry.mapCalls).toHaveLength(1)
    expect(registry.layerGroups.length).toBeGreaterThanOrEqual(1)
  })

  it('bbox 非法（长度不对 / 含非数字）时不当作有效范围，走兜底而不是算错包围盒', () => {
    const layer = { name: 'x', kind: 'raster', tile_url: TILE_URL, bbox: [111, 30, 'oops'] }
    render(<MapPanel layers={[layer]} center={null} />)
    const map = registry.maps[0]
    expect(map.fits).toHaveLength(0)
    expect(finalView(map)).toEqual({ center: [35, 105], zoom: 4 })
    expect(warnText()).toContain('未能确定该图层的地理位置')
  })
})

describe('leaflet 替身自身的健全性检查', () => {
  // ⚠️ 为什么连"判据"也要测：本项目已经吃过两次亏 ——
  //    ① 无头浏览器按版本号找路径失效，整套校验静默变成"没跑"（T1）；
  //    ② 焦点可见性判据手工乘 dpr 裁错位置，把"有焦点环"判成"没有"。
  //    判据坏了，测试是会**全绿**的。所以替身的关键语义必须自己也被钉住。
  it('boxOf() 能正确读出包围盒', () => {
    const b = fakeLeaflet.latLngBounds([])
    expect(b.isValid()).toBe(false)
    b.extend([30, 111])
    b.extend([32, 113])
    expect(b.isValid()).toBe(true)
    expect(boxOf(b)).toEqual({ minLat: 30, maxLat: 32, minLng: 111, maxLng: 113 })
  })

  it('空数组 extend 不会把 bounds 变成"有效"（否则"无范围"这一支永远测不到）', () => {
    const b = fakeLeaflet.latLngBounds([])
    b.extend([])
    expect(b.isValid()).toBe(false)
  })

  it('latLngBounds(sw, ne) 两个点都要被算进包围盒（firstBbox 分支靠它）', () => {
    const b = fakeLeaflet.latLngBounds([30, 111], [32, 113])
    expect(b.isValid()).toBe(true)
    expect(boxOf(b)).toEqual({ minLat: 30, maxLat: 32, minLng: 111, maxLng: 113 })
  })
})
