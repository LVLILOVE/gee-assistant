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
    const gj = l.geojson
    if (!gj) return
    const feats = gj.type === 'FeatureCollection' ? gj.features : [gj]
    feats.forEach((f) => f.geometry && walk(f.geometry.coordinates))
  })
  return bounds.isValid() ? bounds : null
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

export default function MapPanel({ layers, center }) {
  const ref = useRef(null)
  const mapRef = useRef(null)
  const groupRef = useRef(null)
  const [tileWarn, setTileWarn] = useState(null)
  const c0 = center ? center[0] : null
  const c1 = center ? center[1] : null

  useEffect(() => {
    if (!ref.current) return
    if (!mapRef.current) {
      mapRef.current = L.map(ref.current).setView(c0 && c1 ? [c0, c1] : [31.2, 120.1], 9)
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
    ;(layers || []).forEach((layer) => {
      // 栅格图层（真实 GEE 返回的是瓦片模板）
      if (layer.tile_url) {
        const proxied = proxiedTileUrl(layer.tile_url)
        if (proxied) {
          L.tileLayer(proxied, {
            opacity: 0.85,
            attribution: 'Google Earth Engine',
          }).addTo(group)
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

    const bounds = computeBounds(layers)
    if (bounds) map.fitBounds(bounds, { padding: [20, 20] })
    else if (c0 && c1) map.setView([c0, c1], 9)
    setTimeout(() => map.invalidateSize(), 50)
  }, [layers, c0, c1])

  return (
    <div className="map-wrap">
      <div className="map-container" ref={ref} />
      {tileWarn && (
        <div className="map-warn">
          以下图层的瓦片地址无法识别，已跳过叠加：{tileWarn.join('、')}。
          本机浏览器直连 GEE 瓦片不可用，需由后端代理转发。
        </div>
      )}
    </div>
  )
}
