/** 地图面板：Leaflet 渲染 GEE 栅格瓦片 + 矢量图层。
 *
 * 独立成文件是为了让 leaflet（约 150KB）**延迟加载** —— 它只在用户看到
 * 任务结果时才需要，登录页与应用外壳没必要先下载。
 * 由 App.jsx 通过 `React.lazy(() => import('./MapPanel'))` 引入。
 */
import { useEffect, useRef, useState } from 'react'
import L from 'leaflet'
// 样式跟着组件走：放在 main.jsx 里会让 Vite 把整个 vendor-leaflet chunk
// 提前塞进首屏并 modulepreload，延迟加载就白做了。
import 'leaflet/dist/leaflet.css'

function styleFeature(feat, legend) {
  const p = feat.properties || {}
  if (p.name && legend && legend.length) {
    const m = legend.find((l) => l.label === p.name)
    if (m) return { color: m.color, fillColor: m.color, fillOpacity: 0.6, weight: 0.5 }
  }
  if (p.value !== undefined) {
    const v = Number(p.value)
    let color
    if (v < 0) color = '#3182bd'
    else if (v < 0.4) color = '#fc8d59'
    else if (v < 0.7) color = '#fee08b'
    else color = '#1a9850'
    return { color, fillColor: color, fillOpacity: 0.7, weight: 0.5 }
  }
  return { color: '#3388ff', fillOpacity: 0.05, weight: 1 }
}

function computeBounds(layers) {
  const bounds = L.latLngBounds([])
  const walk = (coords) => {
    if (!coords || !coords.length) return
    if (typeof coords[0] === 'number') {
      bounds.extend([coords[1], coords[0]])
      return
    }
    coords.forEach(walk)
  }
  ;(layers || []).forEach((l) => {
    // 优先用后端直接给出的 bbox（栅格图层唯一可靠的地理范围来源）
    const bb = l.bbox
    if (Array.isArray(bb) && bb.length === 4 && bb.every((n) => Number.isFinite(n))) {
      bounds.extend([bb[1], bb[0]])
      bounds.extend([bb[3], bb[2]])
      return
    }
    const gj = l.geojson
    if (!gj) return
    const feats = gj.type === 'FeatureCollection' ? gj.features : [gj]
    feats.forEach((f) => f.geometry && walk(f.geometry.coordinates))
  })
  return bounds.isValid() ? bounds : null
}

/** 从所有图层里挑一个能用的 bbox。 */
function firstBbox(layers) {
  for (const l of layers || []) {
    const bb = l.bbox
    if (Array.isArray(bb) && bb.length === 4 && bb.every((n) => Number.isFinite(n))) {
      return bb
    }
  }
  return null
}

/** 把 GEE 瓦片模板改写成后端代理地址。
 *
 * 国内浏览器直连 earthengine-highvolume.googleapis.com 是 HTTP 000（TCP 层就失败），
 * 所以栅格图层必须由后端经本机代理转发。
 *
 * 返回 null 表示模板格式不认识。此时【绝不能退回裸地址】：那个地址在这台机器上
 * 必然连不通，地图只会静默空白，比直接告诉用户"这个图层加不上"更糟。
 */
function proxiedTileUrl(tileUrl) {
  if (!tileUrl || typeof tileUrl !== 'string') return null
  const base = tileUrl.replace(/\/\{z\}\/\{x\}\/\{y\}\/?$/, '')
  if (base === tileUrl) return null // 模板格式不属于预期，别硬套代理
  return `/api/tile/{z}/{x}/{y}?u=${encodeURIComponent(base)}`
}

/** 给 geojson 算一个「内容指纹」。
 *
 * 为什么不能只记「有没有矢量」（原先用的是 `l.geojson ? 1 : 0`）：
 * 对同一区域、同一任务类型跑两次（时间范围不同，真实 GEE 返回的矢量内容不同），
 * name / kind / bbox 完全一致而 geojson 内容不同 → 指纹相同 → effect 不重跑
 * → 地图上仍是**上一次**的矢量叠加，而卡片标题写的是新任务的名字，
 * 用户看到"旧图上贴着新标题"。
 *
 * 为什么也不整个 JSON.stringify：多要素的 geojson 可能有几十万字符，
 * 每次渲染都全量序列化会明显掉帧。取「类型 + 要素数 + 首尾坐标片段」
 * 足以区分不同结果，代价是常数级。
 */
function geoFingerprint(gj) {
  if (!gj) return 0
  try {
    const feats = gj.type === 'FeatureCollection' ? gj.features || [] : [gj]
    const head = JSON.stringify(feats[0]?.geometry?.coordinates ?? null)
    const tail = JSON.stringify(feats[feats.length - 1]?.geometry?.coordinates ?? null)
    return `${gj.type}|${feats.length}|${head.length}|${head.slice(0, 32)}|${tail.slice(-32)}`
  } catch {
    return 1
  }
}

export default function MapPanel({ layers, center }) {
  const ref = useRef(null)
  const mapRef = useRef(null)
  const groupRef = useRef(null)
  const sizeTimerRef = useRef(null)
  const [tileWarn, setTileWarn] = useState(null)
  // 瓦片**加载失败**（与上面的 tileWarn 不是一回事：tileWarn 是"模板解析不出来"，
  // 这里是"请求真的失败了"）。见下方 noteTileError 的注释。
  const [tileErr, setTileErr] = useState(null)
  const tileErrRef = useRef({ count: 0, names: new Set() })
  const tileErrTimerRef = useRef(null)
  const [locWarn, setLocWarn] = useState(false)
  const c0 = center ? center[0] : null
  const c1 = center ? center[1] : null

  // 给图层数组做一个「内容指纹」。
  //
  // ⚠ 必须这样做的原因（2026-09-23 实测到的真实缺陷，不是理论担忧）：
  //   `layers` 由父组件每次渲染都**新建数组**（`layers={[layer]}`），
  //   引用永远在变。把它直接写进 useEffect 依赖，效果就会反复重跑；
  //   而重跑的时机常常是「父组件先渲染一次空态、再渲染出结果」——
  //   第二次运行会拿着**已经清空**的图层重新定位，把第一次
  //   刚 fitBounds 好的视野**重新打回全局视图 [35,105]**。
  //   实测证据：修复后的任务接口已返回 bbox，且正确答案在西安/宜昌一带，
  //   但截图上地图停在全局视图 + 弹出了"定位不了"的提示
  //   （即 setLocWarn(true) 是后跑的覆盖了前面的 setLocWarn(false)）。
  //   这正好把这次修复的意义抵消掉 —— 所以指纹是这次修复的必要组成部分。
  const layerKey = JSON.stringify(
    (layers || []).map((l) => [l.name, l.kind, l.tile_url, geoFingerprint(l.geojson),
      Array.isArray(l.bbox) ? l.bbox : null]),
  )

  // 瓦片加载失败的上报入口。
  //
  // 【为什么必须有】栅格图层的 `tile_url` 里嵌着 GEE 签发的**短期令牌**，任务当时
  // 存下的地址过一段时间就失效；后端代理把它映射成 400（实测：无效 mapid →
  // `{"detail":"瓦片请求被拒绝：GEE 返回 404"}`）。而 Leaflet 拿不到图只会把该瓦片
  // 留成灰色 —— **原先前端完全没有 tileerror 监听**，用户看到的是一张"没有色块的
  // 地图"，既无报错也无提示，他唯一能得出的结论是"分析结果错了/没结果"。
  // 这恰好踩中本项目最忌讳的一条：**静默失败**。
  //
  // 去抖的原因：一次地图加载会并发请求几十张瓦片，逐张 setState 会造成渲染风暴。
  const noteTileError = (name) => {
    const acc = tileErrRef.current
    acc.count += 1
    if (name) acc.names.add(name)
    clearTimeout(tileErrTimerRef.current)
    tileErrTimerRef.current = setTimeout(() => {
      setTileErr({ count: tileErrRef.current.count, names: [...tileErrRef.current.names] })
    }, 400)
  }

  // 卸载时销毁 Leaflet 实例。**必须有**：L.map 会在 window 上挂 resize 监听
  // （trackResize 默认为 true），而只有 map.remove() 才会解绑它。
  // 不销毁的话，每次切换任务 / 切换图层数量都会**泄漏一张活地图**
  // （DOM 容器、瓦片层、事件监听全部留着不放），用户表现为长时间使用后越来越卡。
  useEffect(
    () => () => {
      clearTimeout(sizeTimerRef.current)
      clearTimeout(tileErrTimerRef.current)
      if (mapRef.current) {
        mapRef.current.remove()
        mapRef.current = null
      }
      groupRef.current = null
    },
    [],
  )

  useEffect(() => {
    if (!ref.current) return
    const initial = c0 && c1 ? [c0, c1] : [35, 105]
    if (!mapRef.current) {
      mapRef.current = L.map(ref.current).setView(initial, c0 && c1 ? 9 : 4)
      // 底图同样经后端代理（浏览器直连 OSM 也是 HTTP 000）
      L.tileLayer('/api/basemap/{z}/{x}/{y}', {
        attribution: '&copy; OpenStreetMap',
      }).addTo(mapRef.current)
      groupRef.current = L.layerGroup().addTo(mapRef.current)
    }
    const map = mapRef.current
    if (!groupRef.current) groupRef.current = L.layerGroup().addTo(map)
    const group = groupRef.current
    group.clearLayers()

    const unusable = []
    // 重新渲染图层就重置上一轮的失败计数（换了任务/换了图层，旧警告不该继续显示）
    tileErrRef.current = { count: 0, names: new Set() }
    clearTimeout(tileErrTimerRef.current)
    setTileErr(null)
    ;(layers || []).forEach((layer) => {
      // 栅格图层（真实 GEE 返回的是瓦片模板）
      if (layer.tile_url) {
        const proxied = proxiedTileUrl(layer.tile_url)
        if (proxied) {
          const tl = L.tileLayer(proxied, {
            opacity: 0.85,
            attribution: 'Google Earth Engine',
          })
          // 挂 tileerror：图层链接过期 / 上游 4xx 时必须让用户知道，
          // 而不是留一张"没有色块的地图"让他自己猜。
          tl.on('tileerror', () => noteTileError(layer.name))
          tl.addTo(group)
        } else {
          // 解析不出来就别塞裸地址（浏览器直连必然失败），改为显式提示
          unusable.push(layer.name || '栅格图层')
          console.warn('[map] 无法代理的瓦片模板：', layer.tile_url)
        }
      }
      // 矢量图层
      if (layer.geojson) {
        L.geoJSON(layer.geojson, { style: (f) => styleFeature(f, layer.legend) }).addTo(group)
      }
    })
    setTileWarn(unusable.length ? unusable : null)

    // ---- 定位优先级（这个顺序本身就是一次 bug 修复）----
    //  ① 图层自带的地理范围（后端从 AOI 算出的 bbox，或 geojson 坐标）
    //  ② 区域名查到的中心点（次选：字典只有十几个地名）
    //  ③ 都没有 → **保持中性全局视图并明确提示**，绝不猜一个具体地点。
    //
    // 为什么 ③ 必须存在：原先这里的兜底是硬编码的太湖坐标 [31.2,120.1]。
    // 任何不在内置字典里的地名（恩施大峡谷、南京、任意景区…）都会静默跳到苏州，
    // 而真正的图层在 1000 km 外完全不可见 —— 用户看到的是"一张苏州地图"，
    // 会直接判定"分析错了地方"。**错误的自信比明确的失败危险得多。**
    const bounds = computeBounds(layers)
    if (bounds) {
      map.fitBounds(bounds, { padding: [20, 20] })
      setLocWarn(false)
    } else {
      const bb = firstBbox(layers)
      if (bb) {
        map.fitBounds(L.latLngBounds([bb[1], bb[0]], [bb[3], bb[2]]), { padding: [20, 20] })
        setLocWarn(false)
      } else if (c0 && c1) {
        map.setView([c0, c1], 9)
        setLocWarn(false)
      } else {
        // 既没有图层范围、也没有区域中心 —— 老实说"定位不了"，别假装知道
        map.setView([35, 105], 4)
        setLocWarn(true)
      }
    }
    // 容器此时可能还没拿到最终尺寸（父级先渲染空态再填内容），延后一次校正。
    // 定时器要存起来并在卸载时清掉 —— 否则组件已卸载才执行 invalidateSize，
    // 作用在一张已被 remove() 的地图上。
    clearTimeout(sizeTimerRef.current)
    sizeTimerRef.current = setTimeout(() => {
      if (mapRef.current === map) map.invalidateSize()
    }, 50)
    // 依赖用 layerKey（内容指纹）而不是 layers 本身 —— 见上方注释。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layerKey, c0, c1])

  return (
    <div className="map-wrap">
      <div className="map-container" ref={ref} />
      {locWarn && (
        <div className="map-warn">
          {(() => {
            // 提示语要**如实**区分两种"定位不了"，否则会误导用户去改错东西：
            //  ① 图层根本没带范围信息（老任务，后端没回传 bbox）→ 该建议重跑/补坐标；
            //  ② 带了范围但区域名不在内置库 → 这才是"名字查不到"。
            // 2026-09-23 实测踩到：回填 bbox 的任务早就带了范围，文案却仍写
            // "后端未返回范围信息"，用户按这个提示去查后端会一无所获。
            const hasRange = (layers || []).some(
              (l) => (Array.isArray(l.bbox) && l.bbox.length === 4) || l.geojson)
            return hasRange
              ? '未能确定该图层的地理位置，已显示全局视图。请在地图上自行查找分析区域。'
              : '未能确定该图层的地理位置，已显示全局视图。该任务未记录分析区域范围'
                + '（区域名也不在内置区域库中），可重新运行该分析以获取定位信息。'
          })()}
        </div>
      )}
      {tileWarn && (
        <div className="map-warn">
          以下图层的瓦片地址无法识别，已跳过叠加：{tileWarn.join('、')}。
          本机浏览器直连 GEE 瓦片不可用，需由后端代理转发。
        </div>
      )}
      {tileErr && tileErr.count > 0 && (
        <div className="map-warn">
          栅格图层有 {tileErr.count} 张瓦片加载失败
          {tileErr.names.length ? `（${tileErr.names.join('、')}）` : ''}，
          地图上的色块会缺失。最常见的原因是 GEE 图层链接里的短期令牌已过期
          （历史任务放久了都会出现），点「重新运行」重跑一次该分析即可刷新图层。
        </div>
      )}
    </div>
  )
}
