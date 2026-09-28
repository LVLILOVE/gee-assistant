/** Leaflet 的测试替身。
 *
 * 【为什么不直接跑真 leaflet】
 * 真 Leaflet 需要一个**有布局尺寸**的容器：它要读 offsetWidth/offsetHeight 才能
 * 建投影、算 fitBounds 的缩放级别。jsdom 不做布局，所有尺寸都是 0，于是
 * fitBounds 的结果永远是"无效/退化"的 —— 拿它来断言"地图定位对不对"只会得到
 * 一堆恒真的废断言。所以这里替身的作用不是"假装渲染"，而是**记录调用**：
 * 把 fitBounds / setView / remove / tileerror 这些**行为契约**暴露出来给测试断言。
 *
 * 只要替身与真 leaflet 的**接口语义**一致（同名方法、同样的入参形状），
 * 那么"MapPanel 调了哪些方法、传了什么"就是可信的证据。
 *
 * ⚠️ 这里刻意**不复刻**投影与缩放计算。任何"地图 zoom 应该是几"的断言都必须在
 * 真实浏览器里做（见 backend/tools/test_region_map_fallback.py 与
 * skills/headless-browser-verify-cn）。本文件只回答"调没调、传什么"。
 */

/** 所有替身实例的登记表；测试用 reset() 清干净，避免用例之间互相污染。 */
export const registry = {
  mapCalls: [], // L.map(el, opts) 的 (el, opts)
  maps: [], // 建出来的地图实例
  tileLayers: [], // L.tileLayer 实例（含 url）
  geoJsons: [], // L.geoJSON 实例（含入参）
  layerGroups: [],
  reset() {
    this.mapCalls = []
    this.maps = []
    this.tileLayers = []
    this.geoJsons = []
    this.layerGroups = []
  },
}

/** 把一条 bounds 上收集到的点换算成地理包围盒，供断言用。
 *
 * 约定与 MapPanel 的用法一致：extend 收到的是 **[lat, lng]**（Leaflet 的顺序）。
 * 返回 null 表示这条 bounds 是空的（等价于 Leaflet 的 `isValid() === false`）。
 */
export function boxOf(bounds) {
  const pts = bounds?.points || []
  if (!pts.length) return null
  const lats = pts.map((p) => p[0])
  const lngs = pts.map((p) => p[1])
  return {
    minLat: Math.min(...lats),
    maxLat: Math.max(...lats),
    minLng: Math.min(...lngs),
    maxLng: Math.max(...lngs),
  }
}

class FakeBounds {
  constructor(a, b) {
    this.points = []
    if (a !== undefined) this.extend(a)
    if (b !== undefined) this.extend(b)
  }

  // 语义对齐 Leaflet：空数组 = 什么都没加（Leaflet 的 _extend 会跳过空数组）。
  // 这一点很关键 —— computeBounds 里的 `L.latLngBounds([])` 必须被判成"空"，
  // 否则"没有任何图层带范围"这一支永远不会执行，定位兜底就测不出来了。
  extend(latlng) {
    if (!latlng || !latlng.length) return this
    if (typeof latlng[0] === 'number') {
      this.points.push([latlng[0], latlng[1]])
      return this
    }
    latlng.forEach((x) => this.extend(x))
    return this
  }

  isValid() {
    return this.points.length > 0
  }
}

class FakeLayer {
  constructor(kind, arg) {
    this.kind = kind
    this.arg = arg
    this.handlers = {}
    this.addedTo = []
    this.cleared = 0
  }

  addTo(target) {
    this.addedTo.push(target)
    return this
  }

  on(evt, fn) {
    ;(this.handlers[evt] = this.handlers[evt] || []).push(fn)
    return this
  }

  /** 测试用：手动触发某个事件（例如让瓦片"加载失败"）。 */
  fire(evt, payload) {
    ;(this.handlers[evt] || []).forEach((fn) => fn(payload))
  }

  clearLayers() {
    this.cleared += 1
  }
}

class FakeMap {
  constructor(el, opts) {
    this.el = el
    this.opts = opts
    this.views = []
    this.fits = []
    this.removed = 0
    this.invalidated = 0
    this.layers = []
  }

  setView(center, zoom) {
    this.views.push({ center, zoom })
    return this
  }

  fitBounds(bounds, opts) {
    this.fits.push({ bounds, box: boxOf(bounds), opts })
    return this
  }

  remove() {
    this.removed += 1
  }

  invalidateSize() {
    this.invalidated += 1
  }
}

const L = {
  map(el, opts) {
    const m = new FakeMap(el, opts)
    registry.mapCalls.push({ el, opts })
    registry.maps.push(m)
    return m
  },
  tileLayer(url, opts) {
    const l = new FakeLayer('tileLayer', { url, opts })
    registry.tileLayers.push(l)
    return l
  },
  layerGroup() {
    const l = new FakeLayer('layerGroup')
    registry.layerGroups.push(l)
    return l
  },
  geoJSON(gj, opts) {
    const l = new FakeLayer('geoJSON', { gj, opts })
    registry.geoJsons.push(l)
    return l
  },
  latLngBounds(a, b) {
    return new FakeBounds(a, b)
  },
}

export default L
