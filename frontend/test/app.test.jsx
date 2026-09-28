/** App 层的端到端行为测试（jsdom + 真 antd 渲染，只把 api 换掉）。
 *
 * 【为什么这块最值得补】
 * 第一巡 12 处前端缺陷里，**7 处（F1–F7）都在 App 的异步流程里**，而且几乎都不是
 * "界面没渲染出来"，而是"渲染出来了但行为不对"：
 *   · 轮询链一次瞬时失败就永久断掉（F1）—— 界面停在"执行中"转圈，既不报错也不结束
 *   · 正在跑的任务把用户刚点开的历史任务覆盖掉（F2）
 *   · 按钮只锁 1 秒，30~90 秒的分析期内可以重复提交（F3）
 *   · 两条轮询链共用一个定时器 ref 互相误杀（F4，与 F2 同一处归属机制）
 *   · 多步流程的重试永不放弃 → 用户再也发不出任何指令（F5）
 *   · 详情加载失败后右栏永久停在"加载中"（F6）
 *   · 登出不清对话 → 换账号后新用户看到上一位用户的全部问答（F7）
 * 这一整类只有把组件真渲染一遍、再驱动定时器看 DOM 与调用，才钉得住。
 *
 * 【api 是唯一的替身】
 * 只 mock `../src/api`，其余（antd、React）都是真的 —— 这样"点了按钮到底发生了什么"
 * 才是真链路，而不是对着一堆桩断言。axios 那层不会真发请求，因为导出函数全被替换了。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, act, waitFor } from '@testing-library/react'
import { message } from 'antd'

const api = vi.hoisted(() => ({
  getHealth: vi.fn(),
  getMe: vi.fn(),
  getGeeStatus: vi.fn(),
  getTaskTypes: vi.fn(),
  getRegions: vi.fn(),
  getPreferences: vi.fn(),
  getProfiles: vi.fn(),
  getKnowledge: vi.fn(),
  listTasks: vi.fn(),
  submitTask: vi.fn(),
  getTask: vi.fn(),
  submitFeedback: vi.fn(),
  logout: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  changePassword: vi.fn(),
  savePreferences: vi.fn(),
  searchKnowledge: vi.fn(),
  getFeedbackSummary: vi.fn(),
  planTask: vi.fn(),
  parseIntent: vi.fn(),
  askExpert: vi.fn(),
  setUnauthorizedHandler: vi.fn(),
  reportUrl: vi.fn((id) => `/api/tasks/${id}/report`),
}))

vi.mock('../src/api', () => api)
// 结果区的地图/图表替身：这两个是 React.lazy 的独立分包，本体另有专门测试
// （mappanel.test.jsx / chartpanel.test.jsx）。这里只关心 App 的接线是否正确。
vi.mock('../src/MapPanel', () => ({ default: () => <div data-testid="map-stub" /> }))
vi.mock('../src/ChartPanel', () => ({ default: () => <div data-testid="chart-stub" /> }))

import App from '../src/App.jsx'

const EXAMPLE = '分析太湖流域 6-8 月植被状况'

/** 冲微任务。不用 waitFor 是为了让同一个 helper 在假定时器下也能用。 */
const flush = async (n = 10) => {
  for (let i = 0; i < n; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => {
      await Promise.resolve()
    })
  }
}

const renderApp = async () => {
  const r = render(<App />)
  // ⚠️ 这里**不能用 waitFor**：RTL 判断"是否启用了假定时器"的写法是
  //    `typeof jest !== 'undefined' && ...`，而本项目用 vitest（全局里没有 jest），
  //    于是启用假定时器的用例会被 RTL 当成"真定时器"，它内部的 setInterval 被
  //    sinon 接管后永不触发 → 一律 20s 超时。实测就是这么红的。
  //    改用纯微任务冲 + 直接查 DOM：登录后的那串都是 promise 链，不需要真定时器。
  for (let i = 0; i < 40; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => {
      await Promise.resolve()
    })
    if (document.querySelector('.app')) break
  }
  expect(document.querySelector('.app')).toBeTruthy()
  return r
}

/** 整页正文。
 *
 * 不用 `getByText` 的单节点匹配：任务参数那张卡是把 region/日期/云量
 * `.join(' · ')` 出来的，再加个 <strong> 就匹配不到了（实测踩过）。
 * 也不用"只看右栏"：`任务状态`卡（含日志）在**左栏**，而"换账号后残留""任务被覆盖"
 * 这两类断言恰恰要同时看左右两栏。
 */
const bodyText = () => document.body.textContent || ''

/** 按可访问名找按钮，容忍 antd 的"两个汉字之间插空格"。
 *  antd 的 Button 对**恰好两个汉字**的文案会渲染成「登 录」「退 出」「发 送」，
 *  精确字符串匹配会找不到元素（一开始就栽在这里）。 */
const btn = (re) => screen.getByRole('button', { name: re })

const historyRow = (text) => screen.getByText(text)

/** 通过"示例标签"提交一个任务。
 *
 * 刻意不走「手动调整参数」那个 Collapse：那条路要先展开折叠面板，
 * 而折叠动画依赖 rAF/定时器，在假定时器下会引入与缺陷无关的不确定性。
 * 示例标签走的是产品主路径（sendText → planTask → parseIntent → doSubmit），
 * 覆盖面反而更完整。
 */
const submitViaChat = async () => {
  fireEvent.click(screen.getByText(EXAMPLE))
  await flush()
}

const HISTORY_B = {
  task_id: 'B',
  task_type: 'ndvi',
  region: '区域B',
  status: 'succeeded',
  created_at: 1700000000,
}

beforeEach(() => {
  vi.clearAllMocks()
  api.getHealth.mockResolvedValue({ status: 'ok', backend: 'gee', has_key: true })
  api.getMe.mockResolvedValue({
    auth_enabled: true,
    authenticated: true,
    username: 'admin',
    is_admin: true,
  })
  api.getGeeStatus.mockResolvedValue({ initialized: true, credentials_found: true })
  // ⚠️ 形状必须与后端一致，否则测的就不是真链路了。
  //    `/api/tasks/types` 是**裸数组**（backend/app/main.py 里 `return [{...}]`），
  //    与 /api/regions、/api/tasks 那些带包装层的接口不同 —— 一开始我按
  //    `{types: [...]}` 造 mock，12 条用例全崩在 `types.map is not a function`。
  api.getTaskTypes.mockResolvedValue([
    { value: 'ndvi', label: '植被指数 NDVI' },
    { value: 'water', label: '水体提取' },
  ])
  api.getRegions.mockResolvedValue({ regions: [{ name: '太湖流域', lat: 31.2, lon: 120.1 }] })
  api.getPreferences.mockResolvedValue({ preferences: {} })
  api.getProfiles.mockResolvedValue({ profiles: [] })
  api.getKnowledge.mockResolvedValue({ datasets: [] })
  api.listTasks.mockResolvedValue({ tasks: [], total: 0 })
  api.logout.mockResolvedValue({ ok: true })
  api.planTask.mockResolvedValue({ multi: false, tasks: [] })
  api.parseIntent.mockResolvedValue({
    complete: true,
    fields: {
      task_type: 'ndvi',
      region: '太湖流域',
      start_date: '2025-06-01',
      end_date: '2025-08-31',
      cloud_threshold: 20,
    },
    summary: '参数已就绪',
  })
  // antd 的 message 是模块级单例，会把提示渲染进 Portal。这里换成 spy：
  // 既免掉 Portal 噪音，又把"到底有没有告诉用户"变成可断言的事实 ——
  // 本轮多处修复的要害正是"原先静默失败"。
  vi.spyOn(message, 'success').mockImplementation(() => {})
  vi.spyOn(message, 'error').mockImplementation(() => {})
  vi.spyOn(message, 'warning').mockImplementation(() => {})
  vi.spyOn(message, 'info').mockImplementation(() => {})
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('App · F3 分析期间不许重复提交', () => {
  beforeEach(() => {
    api.submitTask.mockResolvedValue({ task_id: 'A' })
    api.getTask.mockResolvedValue({
      task_id: 'A',
      status: 'running',
      region: '区域A',
      result: null,
    })
  })

  it('任务还在跑时再提交一次，不会产生第二个任务', async () => {
    await renderApp()
    await submitViaChat()
    await waitFor(() => expect(api.submitTask).toHaveBeenCalledTimes(1))

    // 再走一遍提交路径（模拟用户以为没反应又发了一次）。注意示例标签在
    // 有对话后就不再渲染，所以这里用输入框回车 —— 同样会走到 doSubmit。
    const box = screen.getByPlaceholderText('描述你的分析需求…')
    fireEvent.change(box, { target: { value: '再分析一次太湖流域' } })
    fireEvent.keyDown(box, { key: 'Enter', keyCode: 13, which: 13 })
    await flush()

    // 修复前 `loading` 只覆盖 POST 那一秒，这时按钮早已解锁，
    // 于是会真的提交第二个任务（后端单用户并发上限是 2，会接受）。
    expect(api.submitTask).toHaveBeenCalledTimes(1)
  })

  it('「开始分析」按钮的加载态要覆盖**整个任务周期**，而不是只覆盖提交那一下', async () => {
    await renderApp()
    fireEvent.click(screen.getByText('手动调整参数（高级）'))
    const btn = await screen.findByRole('button', { name: /开始分析/ })
    expect(btn.className).not.toContain('ant-btn-loading')

    await submitViaChat()
    await waitFor(() => expect(api.submitTask).toHaveBeenCalledTimes(1))
    // 任务已提交并进入 running，按钮必须仍在加载态（这是防重闸门的前提）
    await waitFor(() => expect(btn.className).toContain('ant-btn-loading'))
  })

  it('提交失败（配额 429）要恢复可提交状态，并把后端原文说出来', async () => {
    api.submitTask.mockRejectedValue({
      response: { data: { detail: '已有 2 个任务在执行，请稍后' } },
    })
    await renderApp()
    await submitViaChat()

    await waitFor(() =>
      expect(message.error).toHaveBeenCalledWith(expect.stringContaining('已有 2 个任务在执行')),
    )
    expect(api.getTask).not.toHaveBeenCalled()
  })
})

describe('App · F1 轮询瞬时失败不许把任务卡死', () => {
  it('状态查询连续失败时，有限次重试后明确告知用户（而不是永远转圈）', async () => {
    vi.useFakeTimers()
    api.submitTask.mockResolvedValue({ task_id: 'A' })
    api.getTask.mockRejectedValue(new Error('502 Bad Gateway'))

    await renderApp()
    await submitViaChat()

    // 21 = 首次 tick + 20 次重试；此后必须放弃
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500 * 25)
    })

    expect(api.getTask).toHaveBeenCalledTimes(21)
    expect(message.warning).toHaveBeenCalledWith(expect.stringContaining('任务状态多次刷新失败'))

    // 再推进更久也不该继续打接口 —— "永不放弃"正是修复前的写法
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500 * 10)
    })
    expect(api.getTask).toHaveBeenCalledTimes(21)
  })
})

describe('App · F2 正在跑的任务不许覆盖用户已打开的历史任务', () => {
  it('任务 A 在跑 → 点开历史任务 B → A 的后续 tick 必须安静退场', async () => {
    vi.useFakeTimers()
    api.submitTask.mockResolvedValue({ task_id: 'A' })
    api.getTask.mockImplementation((id) =>
      id === 'A'
        ? Promise.resolve({ task_id: 'A', status: 'running', region: '区域A', result: null })
        : Promise.resolve({
            task_id: 'B',
            status: 'succeeded',
            region: '区域B',
            start_date: '2025-01-01',
            end_date: '2025-03-31',
            result: { ok: true, conclusion: 'B 的结论' },
            logs: [],
          }),
    )
    api.listTasks.mockResolvedValue({ tasks: [HISTORY_B], total: 1 })

    await renderApp()
    await submitViaChat()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10)
    })

    fireEvent.click(historyRow('植被指数 NDVI · 区域B'))
    await flush()
    expect(bodyText()).toContain('B 的结论')

    // A 的轮询定时器此刻已经排好，让它再跑几轮
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200 * 3)
    })

    // 右栏必须仍是 B。修复前 A 的 tick 会把 A 的结果整块写进来，
    // 用户会把 A 的结论当成 B 的（历史复看时最容易出错的地方）。
    expect(bodyText()).toContain('B 的结论')
    expect(bodyText()).toContain('区域B')
    expect(bodyText()).not.toContain('区域A')
  })
})

describe('App · F6 详情加载失败不许停在"加载中"', () => {
  it('历史任务详情返回非 2xx 时，右栏给出明确失败而不是永久转圈', async () => {
    api.listTasks.mockResolvedValue({ tasks: [HISTORY_B], total: 1 })
    api.getTask.mockRejectedValue(new Error('404 Not Found'))

    await renderApp()
    fireEvent.click(historyRow('植被指数 NDVI · 区域B'))
    await flush()

    expect(bodyText()).toContain('任务详情加载失败')
    expect(bodyText()).toContain('失败')
  })
})

describe('App · F5 多步流程的等待必须能结束', () => {
  it('子任务状态查询一直失败时，流程最终放弃并放开输入（用户还能继续发指令）', async () => {
    vi.useFakeTimers()
    api.planTask.mockResolvedValue({
      multi: true,
      tasks: [{ task_type: 'water', region: '洞庭湖' }],
    })
    api.submitTask.mockResolvedValue({ task_id: 'm1' })
    api.getTask.mockRejectedValue(new Error('boom'))

    await renderApp()
    fireEvent.click(screen.getByText(EXAMPLE))
    await flush()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500 * 25)
    })

    // 修复前的重试永不放弃 → runOne 永不 resolve → chatLoading 永远为 true，
    // 用户**再也发不出任何指令**，只能刷新页面。所以这里判两件事：
    // ① 转圈结束了；② 多步流程如实报了失败。
    expect(screen.queryByText('正在理解…')).toBeNull()
    expect(btn(/发\s*送/).className).not.toContain('ant-btn-loading')
    expect(screen.getByText(/❌ 失败/)).toBeTruthy()
    expect(bodyText()).toContain('已放弃等待')
  })
})

describe('App · F7 登出必须清空上一位用户的对话', () => {
  it('退出后重新登录，看不到上一位用户的提问与回答', async () => {
    api.login.mockResolvedValue({ username: 'other', is_admin: false })

    await renderApp()
    await submitViaChat()
    expect(document.querySelectorAll('.chat-msg.user').length).toBe(1) // 用户气泡在
    expect(screen.getByText('参数已就绪')).toBeTruthy() // 助手回答在

    fireEvent.click(btn(/退\s*出/))
    await flush()

    // 回到登录页并以另一个账号登录
    fireEvent.change(screen.getByPlaceholderText('用户名'), { target: { value: 'other' } })
    fireEvent.change(screen.getByPlaceholderText('密码'), { target: { value: 'pw12345678' } })
    fireEvent.click(btn(/登\s*录/))
    await flush()

    await waitFor(() => expect(screen.getByText('卫星遥感影像智能分析助手')).toBeTruthy())
    // 换账号后绝不能看到上一位用户的任何对话内容（含分析结论与意图标签）。
    // ⚠️ 判据不能用 `queryByText(EXAMPLE)`：**示例标签**里有一个字符串与它逐字相同，
    //    而对话清空后示例标签会重新出现 —— 那样会把"已清空"判成"没清空"（实测踩到）。
    //    所以按"对话气泡"这类元素来判。
    const userBubbles = [...document.querySelectorAll('.chat-msg.user')].map((n) => n.textContent)
    expect(userBubbles).toEqual([])
    expect(screen.queryByText('参数已就绪')).toBeNull()
    expect(screen.queryByText('参数已就绪，自动开始分析')).toBeNull()
    // 反面确认：对话区确实回到了空态（示例标签重新出现）。
    // 没有这一条的话，上面那句也可能因为"整页没渲染出来"而假通过。
    expect(document.querySelectorAll('.example-tag').length).toBe(3)
  })
})

describe('App · F12 反馈备注只在内容真变化时才提交', () => {
  beforeEach(() => {
    api.listTasks.mockResolvedValue({ tasks: [HISTORY_B], total: 1 })
    api.getTask.mockResolvedValue({
      task_id: 'B',
      status: 'succeeded',
      region: '区域B',
      feedback: 'down',
      feedback_note: '偏大',
      result: { ok: true, conclusion: '结论' },
      logs: [],
    })
  })

  const openB = async () => {
    await renderApp()
    fireEvent.click(historyRow('植被指数 NDVI · 区域B'))
    await flush()
    return screen.getByPlaceholderText(/可选：一句话说明问题/)
  }

  it('纯浏览（点进点出输入框、内容没变）不产生多余的写请求', async () => {
    const box = await openB()
    fireEvent.focus(box)
    fireEvent.blur(box)
    fireEvent.focus(box)
    fireEvent.blur(box)
    await flush()
    expect(api.submitFeedback).not.toHaveBeenCalled()
  })

  it('内容真变了才提交；失败要告诉用户（原先静默吞掉）', async () => {
    api.submitFeedback.mockRejectedValue({ response: { data: { detail: '任务不存在' } } })
    const box = await openB()

    fireEvent.change(box, { target: { value: '偏大且边缘破碎' } })
    fireEvent.blur(box)
    await flush()

    expect(api.submitFeedback).toHaveBeenCalledTimes(1)
    expect(api.submitFeedback).toHaveBeenCalledWith('B', 'down', '偏大且边缘破碎')
    await waitFor(() =>
      expect(message.error).toHaveBeenCalledWith(expect.stringContaining('备注保存失败')),
    )
  })
})

describe('App · T6「重新运行」必须用任务自己的参数', () => {
  const detail = {
    task_id: 'B',
    status: 'succeeded',
    task_type: 'water',
    region: '洞庭湖',
    start_date: '2025-03-01',
    end_date: '2025-05-31',
    cloud_threshold: 37,
    created_at: 1700000000,
    result: { ok: true, conclusion: '结论' },
    logs: [],
  }

  const openB = async (override) => {
    api.listTasks.mockResolvedValue({
      tasks: [
        {
          task_id: 'B',
          task_type: 'water',
          region: '洞庭湖',
          status: 'succeeded',
          created_at: 1700000000,
        },
      ],
      total: 1,
    })
    api.getTask.mockResolvedValue({ ...detail, ...(override || {}) })
    await renderApp()
    fireEvent.click(historyRow('水体提取 · 洞庭湖'))
    await flush()
  }

  it('原样带上该任务当时的区域/时间/云量（而不是表单里的默认值）', async () => {
    api.submitTask.mockResolvedValue({ task_id: 'C' })
    await openB()

    fireEvent.click(screen.getByRole('button', { name: '重新运行' }))
    await flush()

    expect(api.submitTask).toHaveBeenCalledWith({
      task_type: 'water',
      region: '洞庭湖',
      start_date: '2025-03-01',
      end_date: '2025-05-31',
      cloud_threshold: 37,
    })
  })

  it('任务没记录 task_type 时明确告知，不拿默认参数乱跑', async () => {
    await openB({ task_type: undefined })

    fireEvent.click(screen.getByRole('button', { name: '重新运行' }))
    await flush()

    expect(api.submitTask).not.toHaveBeenCalled()
    expect(message.warning).toHaveBeenCalledWith(expect.stringContaining('没有记录任务类型'))
  })
})
