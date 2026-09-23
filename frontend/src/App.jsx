import { Suspense, lazy, useEffect, useRef, useState } from 'react'
import {
  Button,
  Card,
  Collapse,
  Drawer,
  Empty,
  Input,
  InputNumber,
  List,
  Modal,
  Radio,
  Segmented,
  Select,
  Space,
  Spin,
  Tag,
  message,
} from 'antd'
import {
  askExpert,
  changePassword,
  getGeeStatus,
  getHealth,
  getKnowledge,
  getMe,
  getPreferences,
  getProfiles,
  getRegions,
  getTask,
  getTaskTypes,
  listTasks,
  login,
  logout,
  parseIntent,
  planTask,
  register,
  reportUrl,
  savePreferences,
  searchKnowledge,
  setUnauthorizedHandler,
  submitFeedback,
  submitTask,
} from './api'

// 地图（leaflet ~150KB）与图表（echarts ~1MB）按需加载：它们只在有任务结果时才渲染，
// 登录页与应用外壳没必要先把这 1MB+ 下载下来。拆完首屏 JS 从 2.05MB 降到约 0.9MB。
const MapPanel = lazy(() => import('./MapPanel'))
const ChartPanel = lazy(() => import('./ChartPanel'))

/** 默认分析区间：最近一个完整自然年。
 *
 * 与后端 app/daterange.py 保持同一口径（那边写了为什么不用"滚动 12 个月"：
 * 季节完整性 + 年内可复现）。原先这里写死 2024-01-01/2024-12-31，
 * 到 2026 年就过期了——而且它同时是表单初始态和提交兜底，
 * 用户不手动改时间就会拿两年前的数据去分析。
 *
 * 用函数而不是模块级常量：常量在页面加载时求值一次，标签页跨年不刷新会沿用旧值。
 */
function defaultDateRange() {
  const y = new Date().getFullYear() - 1
  return { start_date: `${y}-01-01`, end_date: `${y}-12-31` }
}

/** 任务状态标签配色。
 *
 * 【为什么不用 antd 的预设色名】
 * 原来写的是 color="success" / "error" / "processing" —— antd 会渲染成
 * 「浅底 + 浅色文字」的样式。浏览器实测对比度只有 3.37:1，**未达 WCAG AA 的 4.5:1**。
 * 状态文字（成功/失败）是用户判断任务结果的第一信息，读不清是硬伤。
 *
 * 改为显式指定「实色底 + 白字」：
 *   实色底天然满足 4.5:1（深绿 5.4:1 / 深红 6.4:1 / 深棕 5.9:1）
 * 且状态**从来不只靠颜色表达** —— 文字本身（成功/失败/执行中）就是主通道，
 * 色觉障碍用户不依赖色相也能读懂。
 */
const STATUS = {
  pending: { color: '#6b6b66', text: '排队中' },
  running: { color: '#2f6f9f', text: '执行中' },
  loading: { color: '#2f6f9f', text: '加载中' },
  succeeded: { color: '#3f7d44', text: '成功' },
  failed: { color: '#a33b2c', text: '失败' },
}

/** 取错误里最可读的那一句。
 *
 * 后端把「配额超限 / 用户名占用 / 密码太短」等写成 detail 里的中文，
 * 直接用 axios 的 e.message 只会看到 "Request failed with status code 429"。
 */
const apiErr = (e, fallback = '操作失败') =>
  e?.response?.data?.detail || e?.message || fallback

function Legend({ legend }) {
  if (!legend || !legend.length) return null
  return (
    <div className="legend">
      {legend.map((l, i) => (
        <span key={i} className="legend-item">
          <span className="legend-swatch" style={{ background: l.color }} />
          {l.label}
        </span>
      ))}
    </div>
  )
}

/** 懒加载面板的占位。
 *
 * 地图（leaflet）与图表（echarts）是分开的 chunk，首次渲染要等它们下载完。
 * 没有占位的话用户会以为卡住了 —— 演示现场第一次点开历史任务时尤其明显。
 */
function PanelLoading() {
  return (
    <div className="panel-loading">
      <Spin />
    </div>
  )
}

/** 登录 / 注册页。
 *
 * 服务经 cpolar 暴露在公网，不做访问控制的话任何人拿到 URL 都能提交任务、
 * 消耗 DeepSeek 与 GEE 额度，所以这里是进入应用的前置门。
 */
function AuthScreen({ onSuccess }) {
  const [mode, setMode] = useState('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [password2, setPassword2] = useState('')
  const [allowRegister, setAllowRegister] = useState(true)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    getMe()
      .then((m) => setAllowRegister(m.allow_registration !== false))
      .catch(() => {})
  }, [])

  const submit = async () => {
    const u = username.trim()
    if (!u || !password) {
      message.warning('请填写用户名和密码')
      return
    }
    if (mode === 'register' && password !== password2) {
      message.warning('两次输入的密码不一致')
      return
    }
    setBusy(true)
    try {
      const d = mode === 'login' ? await login(u, password) : await register(u, password)
      message.success(mode === 'login' ? '登录成功' : '注册成功')
      onSuccess(d)
    } catch (e) {
      // 后端的 detail 是可读中文（用户名占用 / 密码太短 / 失败次数过多），优先展示
      message.error(e?.response?.data?.detail || e?.message || '操作失败')
    } finally {
      setBusy(false)
    }
  }

  const options = allowRegister
    ? [
        { label: '登录', value: 'login' },
        { label: '注册', value: 'register' },
      ]
    : [{ label: '登录', value: 'login' }]

  return (
    <div className="auth-wrap">
      <Card className="auth-card" title="卫星遥感影像智能分析助手">
        <p className="auth-hint">
          本服务部署在公网，需登录后使用，以免接口被匿名消耗算力额度。
        </p>
        <Segmented
          block
          value={mode}
          onChange={setMode}
          options={options}
          style={{ marginBottom: 16 }}
        />
        <Input
          placeholder="用户名"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          onPressEnter={submit}
          style={{ marginBottom: 12 }}
        />
        <Input.Password
          placeholder="密码"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          onPressEnter={submit}
          style={{ marginBottom: 12 }}
        />
        {mode === 'register' && (
          <Input.Password
            placeholder="确认密码"
            value={password2}
            onChange={(e) => setPassword2(e.target.value)}
            onPressEnter={submit}
            style={{ marginBottom: 12 }}
          />
        )}
        <Button type="primary" block loading={busy} onClick={submit}>
          {mode === 'login' ? '登录' : '注册并进入'}
        </Button>
        <p className="auth-hint">
          {mode === 'register'
            ? '用户名 3–32 位（中英文/数字/下划线）；密码至少 8 位。'
            : '首次使用请先切换到「注册」。'}
        </p>
      </Card>
    </div>
  )
}

/** 修改密码。后端改完会清掉该用户全部会话，所以这里要顺势回到登录页。 */
function PasswordModal({ open, onClose, onDone }) {
  const [oldPwd, setOldPwd] = useState('')
  const [newPwd, setNewPwd] = useState('')
  const [newPwd2, setNewPwd2] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    if (!oldPwd || !newPwd) {
      message.warning('请填写原密码与新密码')
      return
    }
    if (newPwd.length < 8) {
      message.warning('新密码至少 8 位')
      return
    }
    if (newPwd !== newPwd2) {
      message.warning('两次输入的新密码不一致')
      return
    }
    setBusy(true)
    try {
      await changePassword(oldPwd, newPwd)
      message.success('密码已更新，请用新密码重新登录')
      setOldPwd('')
      setNewPwd('')
      setNewPwd2('')
      onDone()
    } catch (e) {
      message.error(e?.response?.data?.detail || e?.message || '修改失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title="修改密码"
      open={open}
      onCancel={onClose}
      onOk={submit}
      confirmLoading={busy}
      okText="确认修改"
      cancelText="取消"
    >
      <Input.Password
        placeholder="原密码"
        value={oldPwd}
        onChange={(e) => setOldPwd(e.target.value)}
        style={{ marginBottom: 12 }}
      />
      <Input.Password
        placeholder="新密码（至少 8 位）"
        value={newPwd}
        onChange={(e) => setNewPwd(e.target.value)}
        style={{ marginBottom: 12 }}
      />
      <Input.Password
        placeholder="确认新密码"
        value={newPwd2}
        onChange={(e) => setNewPwd2(e.target.value)}
        onPressEnter={submit}
      />
      <p className="auth-hint">修改成功后当前登录会失效，需用新密码重新登录。</p>
    </Modal>
  )
}

export default function App() {
  const [health, setHealth] = useState(null)
  const [gee, setGee] = useState(null)
  const [geeOpen, setGeeOpen] = useState(false)
  const [types, setTypes] = useState([])
  const [form, setForm] = useState({
    task_type: 'ndvi',
    region: '太湖流域',
    ...defaultDateRange(),
    cloud_threshold: 20,
  })
  const [task, setTask] = useState(null)
  // 反馈：value 是 'up' | 'down' | null，note 是可选补充。
  // 反馈值从任务详情回填，所以刷新页面后按钮仍是选中态。
  const [feedback, setFeedback] = useState(null)
  const [feedbackNote, setFeedbackNote] = useState('')
  const [feedbackSending, setFeedbackSending] = useState(false)
  const [history, setHistory] = useState([])
  const [historyTotal, setHistoryTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [convo, setConvo] = useState([])
  const [intent, setIntent] = useState(null)
  const [chatInput, setChatInput] = useState('')
  const [chatLoading, setChatLoading] = useState(false)
  const [regions, setRegions] = useState([])
  const [mode, setMode] = useState('do')
  const [prefs, setPrefs] = useState({ instructions: '', data_source: 'auto', agent_profile: 'analyst' })
  const [profiles, setProfiles] = useState([])
  const [prefsOpen, setPrefsOpen] = useState(false)
  const [prefsSaving, setPrefsSaving] = useState(false)
  const [kb, setKb] = useState([])
  const [kbQuery, setKbQuery] = useState('')
  const [me, setMe] = useState(null)
  const [booted, setBooted] = useState(false)
  const [pwdOpen, setPwdOpen] = useState(false)
  const pollRef = useRef(null)

  const refreshHistory = () => {
    listTasks()
      .then((d) => {
        setHistory(d.tasks || [])
        // 接口默认只回最近 50 条。以前它连总数都不返回，界面自然无从知道"是不是拿全了"——
        // 任务超过 50 条时列表会静默变短，用户看不出来（实测任务涨到 51 条才发现）。
        // 现在把总数记下来并显示，让截断变成看得见的事实。
        setHistoryTotal(d.total ?? (d.tasks || []).length)
      })
      .catch(() => {})
  }

  // 登录后才拉受保护的数据。不采用「先打一遍接口等 401 再重试」的写法：
  // 那会在浏览器控制台刷一屏红色报错，把真正的错误淹掉。
  const loadWorkspace = () => {
    getGeeStatus().then(setGee).catch(() => {})
    getTaskTypes().then(setTypes).catch(() => {})
    getRegions().then((d) => setRegions(d.regions || [])).catch(() => {})
    getPreferences()
      .then((d) => setPrefs(d.preferences || {}))
      .catch(() => {})
    getProfiles()
      .then((d) => setProfiles(d.profiles || []))
      .catch(() => {})
    getKnowledge()
      .then((d) => setKb(d.datasets || []))
      .catch(() => {})
    refreshHistory()
  }

  useEffect(() => {
    // 会话过期时任意请求都会 401，统一退回登录页
    setUnauthorizedHandler(() => setMe({ auth_enabled: true, authenticated: false }))
    getHealth().then(setHealth).catch(() => setHealth({ status: 'down' }))
    getMe()
      .then((m) => {
        setMe(m)
        if (m.authenticated) loadWorkspace()
        setBooted(true)
      })
      .catch(() => {
        setMe({ auth_enabled: true, authenticated: false })
        setBooted(true)
      })
    return () => clearTimeout(pollRef.current)
  }, [])

  const doLogout = async () => {
    try {
      await logout()
    } catch (e) {
      /* 即使请求失败也要退回登录页，避免用户卡在无权限的界面 */
    }
    clearTimeout(pollRef.current)
    setTask(null)
    setHistory([])
    setMe({ auth_enabled: true, authenticated: false })
  }

  const doKbSearch = async () => {
    try {
      const d = await searchKnowledge(kbQuery, mode === 'do' ? form.task_type : '')
      setKb(d.datasets || [])
    } catch (e) {
      message.error('检索失败：' + (e?.message || e))
    }
  }

  const startPoll = (id) => {
    clearTimeout(pollRef.current)
    getTask(id).then((t) => {
      setTask(t)
      // 已提交过的反馈回填（刷新/切历史任务后按钮仍显示选中态）
      setFeedback(t.feedback || null)
      setFeedbackNote(t.feedback_note || '')
      if (t.status === 'running' || t.status === 'pending') {
        pollRef.current = setTimeout(() => startPoll(id), 1200)
      } else {
        refreshHistory()
        if (t.status === 'succeeded') message.success('分析完成')
        else message.error('分析失败，请查看日志')
      }
    })
  }

  const sendFeedback = async (value) => {
    if (!task?.task_id) return
    // 再次点击同一个按钮 = 取消反馈。不提供取消的话，误点一次就永久留下错误信号，
    // 而反馈数据本身就是要给人看的，宁可让人改主意。
    const next = feedback === value ? null : value
    setFeedbackSending(true)
    try {
      if (next === null) {
        // 后端只接受 up/down，没有"取消"语义 —— 这里保持原值而不是假装成功，
        // 避免界面显示"已取消"而后端仍记着上一次的评分。
        setFeedback(feedback)
        message.info('反馈已记录，暂不支持撤销')
        return
      }
      await submitFeedback(task.task_id, next, feedbackNote)
      setFeedback(next)
      message.success(next === 'up' ? '感谢反馈 👍' : '已记录，我们会继续改进')
    } catch (e) {
      // axios 给的是 "Request failed with status code 4xx"，要显示后端原文才有用
      message.error('反馈提交失败：' + (e?.response?.data?.detail || e?.message || e))
    } finally {
      setFeedbackSending(false)
    }
  }

  // 纯栅格图层（如 NDVI 分布）不含 geojson，算不出 bounds，用区域中心兜底定位
  const regionCenter = (name) => {
    const hit = (regions || []).find((r) => r.name === name)
    return hit ? [hit.lat, hit.lon] : null
  }

  const doSubmit = async (fields) => {
    setLoading(true)
    setTask(null)
    try {
      const { task_id } = await submitTask(fields)
      setTask({ task_id, status: 'pending', logs: [], code: '', result: null, region: fields.region })
      startPoll(task_id)
    } catch (e) {
      // 配额超限（429）也走这里，detail 会带上"已有 N 个任务在执行"之类的具体原因
      message.error('提交失败：' + apiErr(e, '请查看后端日志'))
    } finally {
      setLoading(false)
    }
  }

  const onSubmit = () => {
    if (!form.region.trim()) {
      message.warning('请填写分析区域')
      return
    }
    doSubmit(form)
  }

  const TASK_LABELS = {
    ndvi: '植被指数 NDVI',
    water: '水体提取',
    classification: '地表分类',
    change_detection: '时序变化检测',
  }

  // 提交一个子任务并轮询到完成，返回最终任务对象。
  // 提交失败也**必须** resolve：原来只写了 .then，一旦 submitTask 被拒（例如配额
  // 429），这个 Promise 永远不落地，runMulti 里的 await 就卡死，用户看到的是
  // 一直停在"执行中…"、既不报错也不结束。这里退回一个合成失败对象，让多步流程
  // 继续走完并把原因写进日志。
  const runOne = (fields) =>
    new Promise((resolve) => {
      submitTask(fields)
        .then(({ task_id }) => {
          const poll = () => {
            getTask(task_id)
              .then((t) => {
                if (t.status === 'running' || t.status === 'pending') {
                  setTimeout(poll, 1200)
                } else {
                  resolve(t)
                }
              })
              .catch(() => setTimeout(poll, 1200))
          }
          poll()
        })
        .catch((e) => {
          resolve({
            task_id: 'submit-failed',
            status: 'failed',
            attempts: 0,
            code: '',
            result: null,
            logs: [`提交被拒绝：${apiErr(e)}`],
          })
        })
    })

  const runMulti = async (tasks) => {
    const results = []
    const fallbackRange = defaultDateRange()
    for (let i = 0; i < tasks.length; i++) {
      const t = tasks[i]
      const label = TASK_LABELS[t.task_type] || t.task_type
      setConvo((c) => [...c, { role: 'assistant', text: `【第 ${i + 1}/${tasks.length} 步】${label} · ${t.region} 执行中…` }])
      const r = await runOne({
        task_type: t.task_type,
        region: t.region || '太湖流域',
        start_date: t.start_date || fallbackRange.start_date,
        end_date: t.end_date || fallbackRange.end_date,
        cloud_threshold: t.cloud_threshold ?? 20,
      })
      results.push(r)
      setConvo((c) => [
        ...c,
        {
          role: 'assistant',
          text: `【第 ${i + 1}/${tasks.length} 步】${label} ${r.status === 'succeeded' ? '✅ 完成' : '❌ 失败'}`,
        },
      ])
    }
    // 合并多步结果，复用现有渲染
    const layers = results.flatMap((r) => r.result?.layers || [])
    const charts = results.flatMap((r) => r.result?.charts || [])
    const code = results
      .map((r, i) => `# ===== 子任务 ${i + 1}: ${TASK_LABELS[tasks[i].task_type] || tasks[i].task_type} =====\n${r.code || ''}`)
      .join('\n\n')
    const logs = results.flatMap((r) => r.logs || [])
    const allOk = results.every((r) => r.status === 'succeeded')
    setTask({
      task_id: results.map((r) => r.task_id).join('+'),
      status: allOk ? 'succeeded' : 'failed',
      attempts: results.reduce((s, r) => s + (r.attempts || 0), 0),
      code,
      logs,
      result: { ok: allOk, layers, charts },
      is_multi: true,
      region: tasks[0]?.region || '',
    })
    refreshHistory()
    if (allOk) message.success('多步分析全部完成')
    else message.warning('部分子任务失败，请查看结果')
  }

  const sendText = async (raw) => {
    const text = (raw || '').trim()
    if (!text) return
    setChatInput('')
    setConvo((c) => [...c, { role: 'user', text }])
    setChatLoading(true)
    try {
      if (mode === 'ask') {
        const res = await askExpert(text, convo)
        setConvo((c) => [...c, { role: 'assistant', text: res.answer }])
        return
      }
      // 执行模式：先检测是否多意图
      const planRes = await planTask(text)
      if (planRes.multi && planRes.tasks?.length) {
        const tasks = planRes.tasks
        setConvo((c) => [
          ...c,
          {
            role: 'assistant',
            text: `检测到 ${tasks.length} 个独立分析目标，将依次执行：${tasks
              .map((t) => TASK_LABELS[t.task_type] || t.task_type)
              .join('、')}`,
          },
        ])
        await runMulti(tasks)
        return
      }
      const res = await parseIntent(text, intent?.fields)
      setIntent(res)
      setConvo((c) => [
        ...c,
        { role: 'assistant', text: res.complete ? res.summary : res.question },
      ])
      if (res.complete) {
        setForm((f) => ({
          ...f,
          task_type: res.fields.task_type,
          region: res.fields.region,
          start_date: res.fields.start_date,
          end_date: res.fields.end_date,
          cloud_threshold: res.fields.cloud_threshold,
        }))
        doSubmit(res.fields)
      }
    } catch (e) {
      setConvo((c) => [...c, { role: 'assistant', text: '出错了：' + (e?.message || e) }])
      message.error('请求失败：' + (e?.message || e))
    } finally {
      setChatLoading(false)
    }
  }

  const savePrefs = async () => {
    setPrefsSaving(true)
    try {
      const d = await savePreferences(prefs)
      setPrefs(d.preferences || prefs)
      message.success('分析偏好已保存，将应用到后续代码生成')
      setPrefsOpen(false)
    } catch (e) {
      message.error('保存失败：' + (e?.message || e))
    } finally {
      setPrefsSaving(false)
    }
  }

  const sendChat = () => sendText(chatInput)

  const resetChat = () => {
    setConvo([])
    setIntent(null)
    setChatInput('')
  }

  const loadHistoryItem = (id) => {
    setTask({ task_id: id, status: 'loading', logs: [], code: '', result: null })
    // 切换任务时必须清空上一条的反馈，否则会短暂显示出"别人的"选中态
    setFeedback(null)
    setFeedbackNote('')
    getTask(id).then((t) => {
      setTask(t)
      setFeedback(t.feedback || null)
      setFeedbackNote(t.feedback_note || '')
    })
  }

  const labelOf = (v) => types.find((t) => t.value === v)?.label || v

  const result = task?.result

  if (!booted) {
    return (
      <div className="auth-wrap">
        <Spin tip="加载中" />
      </div>
    )
  }

  if (me && me.auth_enabled !== false && !me.authenticated) {
    return (
      <AuthScreen
        onSuccess={(d) => {
          setMe({ auth_enabled: true, authenticated: true, username: d.username, is_admin: d.is_admin })
          loadWorkspace()
        }}
      />
    )
  }

  return (
    <div className="app">
      <div className="app-header">
        <span className="app-title">卫星遥感影像智能分析助手</span>
        <Space>
          {/* 顶栏状态标签统一用「实色底 + 白字」。
              原写法用 antd 预设色名（green/red/blue/orange），渲染成浅底浅字，
              实测对比度仅 3.37:1，未达 AA 门槛 —— 而这几个标签是用户判断
              「系统是否可用」的第一信息。改为显式实色后均 >=4.5:1。 */}
          <Tag color={health?.status === 'ok' ? '#3f7d44' : '#a33b2c'}>
            后端 {health?.status === 'ok' ? '在线' : '离线'}
          </Tag>
          {health?.status === 'ok' && (
            <Tag style={{ color: 'var(--ink-700)', background: 'var(--canvas)', borderColor: 'var(--line)' }}>
              执行后端：{health.backend}
            </Tag>
          )}
          {health?.status === 'ok' && (
            <Tag color={health.has_key ? '#2f6f9f' : '#b7791f'}>
              LLM {health.has_key ? '已配置' : '未配置(兜底)'}
            </Tag>
          )}
          {gee && (
            <Tag
              color={gee.initialized ? '#3f7d44' : gee.credentials_found ? '#b7791f' : '#6b6b66'}
              style={{ cursor: 'pointer' }}
              onClick={() => setGeeOpen(true)}
            >
              GEE {gee.initialized ? '已就绪' : gee.credentials_found ? '凭据异常' : '凭据未配置'} · 查看
            </Tag>
          )}
          {me && me.auth_enabled !== false && me.authenticated && (
            <>
              <Tag color="#2d6a4f">
                {me.username}
                {me.is_admin ? '（管理员）' : ''}
              </Tag>
              <Button size="small" type="link" onClick={() => setPwdOpen(true)}>
                修改密码
              </Button>
              <Button size="small" type="link" onClick={doLogout}>
                退出
              </Button>
            </>
          )}
        </Space>
      </div>

      <PasswordModal
        open={pwdOpen}
        onClose={() => setPwdOpen(false)}
        onDone={() => {
          setPwdOpen(false)
          doLogout()
        }}
      />

      <Modal
        title="Google Earth Engine 接入状态"
        open={geeOpen}
        onCancel={() => setGeeOpen(false)}
        footer={null}
        width={640}
      >
        {gee && (
          <div style={{ fontSize: 13, lineHeight: 1.9 }}>
            <p>
              <b>依赖包：</b>
              {gee.package_installed ? 'earthengine-api 已安装' : '未安装 earthengine-api'}
              <br />
              <b>凭据来源：</b>
              {gee.source === 'none' ? '未找到凭据' : gee.source}
              <br />
              <b>账号：</b>
              {gee.account}
              <br />
              <b>项目 ID：</b>
              {gee.project || '（未设置 GEE_PROJECT）'}
              <br />
              <b>出网代理：</b>
              {gee.proxy || '（未配置 GEE_PROXY）'}
              <br />
              <b>鉴权结果：</b>
              {gee.initialized ? 'ee.Initialize 成功' : '未通过'}
            </p>
            <p style={{ whiteSpace: 'pre-wrap', color: 'var(--ink-500)' }}>{gee.message}</p>
            {!gee.credentials_found && (
              <div style={{ background: 'var(--moss-50)', padding: 12, borderRadius: 6 }}>
                <b>怎么补上凭据：</b>
                <div style={{ marginTop: 6 }}>
                  凭据需要自行创建，不会自动生成。在 <code>backend</code> 目录执行自检脚本查看逐步指引：
                </div>
                <pre style={{ margin: '6px 0', whiteSpace: 'pre-wrap' }}>
{`.venv\\Scripts\\python.exe tools\\gee_doctor.py
.venv\\Scripts\\python.exe tools\\gee_doctor.py --import-sa "C:\\路径\\key.json"`}
                </pre>
                <div>
                  当前网络已通过 <code>GEE_PROXY</code> 打通（GEE 计算端点实测可达），
                  只差凭据这一步。换网络工具或改端口时用{' '}
                  <code>--detect-proxy</code> 自动发现。
                </div>
                <div style={{ marginTop: 6 }}>
                  完整图文步骤见项目文档 <code>docs/GEE凭据配置指南.md</code>。在凭据就绪前，系统使用离线样例结果兜底，
                  全部功能仍可正常演示。
                </div>
              </div>
            )}
            {gee.credentials_found && !gee.initialized && (
              <div style={{ background: '#fdf6e3', padding: 12, borderRadius: 6 }}>
                凭据已找到但鉴权未通过，优先核对两项：① <code>GEE_PROJECT</code> 是否填对；
                ② 云项目是否已在 code.earthengine.google.com/register 注册，且服务账号已被授予
                Earth Engine Resource Viewer 角色。
              </div>
            )}
          </div>
        )}
      </Modal>

      <div className="app-body">
        <div className="panel-left">
          <Card title="智能对话" size="small">
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                marginBottom: 8,
              }}
            >
              <Segmented
                size="small"
                value={mode}
                onChange={setMode}
                options={[
                  { label: '咨询 Ask', value: 'ask' },
                  { label: '执行 Do', value: 'do' },
                ]}
              />
              <Button size="small" type="link" onClick={() => setPrefsOpen(true)}>
                分析偏好
              </Button>
            </div>
            <div className="chat-box">
              {convo.length === 0 && (
                <div className="chat-hint">
                  {mode === 'ask'
                    ? '咨询模式：问任何遥感 / GEE 相关问题，我以专家身份解答，不会生成或执行代码。例如：「NDVI 和 EVI 有什么区别？」'
                    : '执行模式：用一句话描述你的分析需求，例如：「帮我分析太湖流域 6-8 月的植被状况」'}
                </div>
              )}
              {convo.length === 0 && (
                <div className="example-prompts">
                  {(mode === 'ask'
                    ? ['NDVI 和 EVI 有什么区别？', '怎么选择水体提取的指数？', '云量阈值一般设多少合适？']
                    : ['分析太湖流域 6-8 月植被状况', '提取洞庭湖的水体范围', '对比鄱阳湖近两年的变化']
                  ).map((ex) => (
                    <Tag
                      key={ex}
                      className="example-tag"
                      onClick={() => sendText(ex)}
                    >
                      {ex}
                    </Tag>
                  ))}
                </div>
              )}
              {convo.map((m, i) => (
                <div key={i} className={`chat-msg ${m.role}`}>
                  <div className="chat-bubble">{m.text}</div>
                </div>
              ))}
              {chatLoading && (
                <div className="chat-msg assistant">
                  <div className="chat-bubble">正在理解…</div>
                </div>
              )}
            </div>
            <Space.Compact style={{ width: '100%', marginTop: 8 }}>
              <Input
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                onPressEnter={sendChat}
                placeholder={mode === 'ask' ? '咨询遥感 / GEE 相关问题…' : '描述你的分析需求…'}
              />
              <Button type="primary" loading={chatLoading} onClick={sendChat}>
                发送
              </Button>
            </Space.Compact>
            {mode === 'do' && regions.length > 0 && (
              <div className="region-tags">
                <span className="region-tags-label">常用区域：</span>
                {regions.slice(0, 9).map((r) => (
                  <Tag
                    key={r.name}
                    color="blue"
                    className="region-tag"
                    onClick={() => sendText('分析' + r.name)}
                  >
                    {r.name}
                  </Tag>
                ))}
              </div>
            )}
            <div
              style={{
                marginTop: 8,
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
              }}
            >
              {mode === 'do' && intent?.complete ? (
                <Tag color="green">参数已就绪，自动开始分析</Tag>
              ) : mode === 'do' && intent ? (
                <Tag color="orange">还需补充参数</Tag>
              ) : (
                <span />
              )}
              <Button size="small" type="link" onClick={resetChat} disabled={convo.length === 0}>
                重置对话
              </Button>
            </div>
          </Card>

          <Collapse
            size="small"
            style={{ marginTop: 12 }}
            items={[
              {
                key: 'manual',
                label: '手动调整参数（高级）',
                children: (
            <Space direction="vertical" style={{ width: '100%' }} size={12}>
              <div>
                <div style={{ marginBottom: 4, color: 'var(--ink-700)' }}>任务类型</div>
                <Select
                  style={{ width: '100%' }}
                  value={form.task_type}
                  onChange={(v) => setForm({ ...form, task_type: v })}
                  options={types.map((t) => ({ value: t.value, label: t.label }))}
                />
              </div>
              <div>
                <div style={{ marginBottom: 4, color: 'var(--ink-700)' }}>分析区域</div>
                <Input
                  value={form.region}
                  onChange={(e) => setForm({ ...form, region: e.target.value })}
                  placeholder="如：太湖流域 / 洞庭湖"
                />
              </div>
              <div>
                <div style={{ marginBottom: 4, color: 'var(--ink-700)' }}>起止日期</div>
                <Space.Compact style={{ width: '100%' }}>
                  <Input
                    value={form.start_date}
                    onChange={(e) => setForm({ ...form, start_date: e.target.value })}
                    placeholder="起始 YYYY-MM-DD"
                  />
                  <Input
                    value={form.end_date}
                    onChange={(e) => setForm({ ...form, end_date: e.target.value })}
                    placeholder="结束 YYYY-MM-DD"
                  />
                </Space.Compact>
              </div>
              <div>
                <div style={{ marginBottom: 4, color: 'var(--ink-700)' }}>云量阈值 (%)</div>
                <InputNumber
                  style={{ width: '100%' }}
                  min={0}
                  max={100}
                  value={form.cloud_threshold}
                  onChange={(v) => setForm({ ...form, cloud_threshold: v })}
                />
              </div>
              <Button type="primary" block loading={loading} onClick={onSubmit}>
                开始分析
              </Button>
            </Space>
                ),
              },
            ]}
          />

          <Collapse
            size="small"
            style={{ marginTop: 12 }}
            items={[
              {
                key: 'kb',
                label: `数据集知识库（${kb.length}）`,
                children: (
                  <Space direction="vertical" style={{ width: '100%' }} size={8}>
                    <Space.Compact style={{ width: '100%' }}>
                      <Input
                        size="small"
                        value={kbQuery}
                        onChange={(e) => setKbQuery(e.target.value)}
                        onPressEnter={doKbSearch}
                        placeholder="搜索数据集，如：水体 / 植被 / 高程"
                      />
                      <Button size="small" onClick={doKbSearch}>
                        检索
                      </Button>
                    </Space.Compact>
                    <div style={{ maxHeight: 240, overflowY: 'auto' }}>
                      {kb.map((d) => (
                        <div key={d.id} className="kb-item">
                          <div className="kb-item-head">
                            <span className="kb-item-name">{d.name}</span>
                            {d.tasks?.includes(form.task_type) && (
                              <Tag color="green" style={{ fontSize: 10 }}>推荐</Tag>
                            )}
                          </div>
                          <div className="kb-item-eeid">{d.ee_id}</div>
                          <div className="kb-item-desc">{d.desc}</div>
                        </div>
                      ))}
                    </div>
                  </Space>
                ),
              },
            ]}
          />

          {task && (
            <Card title="任务状态" size="small" style={{ marginTop: 12 }}>
              <Space direction="vertical" style={{ width: '100%' }} size={8}>
                <Space>
                  <Tag color={STATUS[task.status]?.color}>
                    {STATUS[task.status]?.text || task.status}
                  </Tag>
                  <span style={{ fontSize: 12, color: 'var(--ink-500)' }}>ID: {task.task_id}</span>
                  {task.attempts > 0 && (
                    <span style={{ fontSize: 12, color: 'var(--ink-500)' }}>尝试 {task.attempts} 次</span>
                  )}
                </Space>
                {(task.status === 'running' || task.status === 'pending') && (
                  <Spin size="small" />
                )}
                {/* 参数回显：让用户能核对自己提交时填了什么。
                    此前 task.cloud_threshold 未从后端投影出来，
                    这里只能显示空值；2026-09-20 已补齐。 */}
                <div style={{ fontSize: 12, color: 'var(--ink-700)' }}>
                  {[
                    task.region,
                    task.start_date && task.end_date
                      ? `${task.start_date} ~ ${task.end_date}`
                      : '',
                    // 该值在库里是 REAL，直接用会显示成「云量 ≤ 37.0%」；
                    // 统一取整，与提交表单的整数百分比保持一致。
                    task.cloud_threshold != null
                      ? `云量 ≤ ${Math.round(task.cloud_threshold)}%`
                      : '',
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                </div>
                {task.logs?.length > 0 && (
                  <div>
                    {task.logs.map((l, i) => (
                      <div key={i} className="log-line">
                        {l}
                      </div>
                    ))}
                  </div>
                )}
              </Space>
            </Card>
          )}

          <Card
            title={
              historyTotal > history.length
                ? `历史任务（显示最近 ${history.length} / 共 ${historyTotal} 条）`
                : '历史任务'
            }
            size="small"
            style={{ marginTop: 12 }}
          >
            {history.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无历史任务" />
            ) : (
              <List
                size="small"
                dataSource={history}
                renderItem={(it) => (
                  <List.Item
                    style={{ cursor: 'pointer' }}
                    onClick={() => loadHistoryItem(it.task_id)}
                  >
                    <Space direction="vertical" size={0}>
                      <span style={{ fontSize: 13 }}>
                        {labelOf(it.task_type)} · {it.region}
                      </span>
                      <span style={{ fontSize: 11, color: 'var(--ink-500)' }}>
                        {STATUS[it.status]?.text || it.status} ·{' '}
                        {new Date(it.created_at * 1000).toLocaleString()}
                      </span>
                    </Space>
                  </List.Item>
                )}
              />
            )}
          </Card>
        </div>

        <div className="panel-right">
          {task && !task.is_multi && (
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
              <Button
                size="small"
                href={reportUrl(task.task_id)}
                target="_blank"
                rel="noreferrer"
                disabled={!result}
              >
                导出报告
              </Button>
            </div>
          )}
          {!result && (
            <Card>
              {/* 空状态：原来只有一行灰字居中，看着像"页面坏了"。
                  改成"图示 + 说明 + 预期"，让用户知道这里是做什么的、
                  以及需要等多久 —— 遥感任务要跑 30~90s，不说清楚会被当成卡死。 */}
              <div className="empty-state">
                <div className="empty-state-mark" aria-hidden="true" />
                <div className="empty-state-title">结果将显示在这里</div>
                <div className="empty-state-desc">
                  在左侧用一句话描述你的分析需求，例如「分析太湖流域 6-8 月植被状况」。
                  分析会调用卫星影像实时计算，通常需要 30~90 秒。
                </div>
              </div>
            </Card>
          )}

          {/* 结论摘要放在最前面 —— 非专业用户看完图仍然不知道"所以我该做什么"，
              这是"零代码"产品最容易在最后一公里失败的地方。数据本来就在手，
              只是差一次把统计量翻译成业务语言的调用。 */}
          {result?.ok !== false && result?.conclusion && (
            <Card className="result-card conclusion-card" title="结论摘要">
              <div className="conclusion-text">{result.conclusion}</div>
              <div className="conclusion-note">
                本结论由 AI 依据本次统计结果自动生成，仅供研究与分析参考，不构成权威监测结论。
              </div>
            </Card>
          )}

          {result?.layers?.map((layer, i) => (
            <Card key={i} className="result-card" title={layer.name}>
              <Suspense fallback={<PanelLoading />}>
                <MapPanel layers={[layer]} center={regionCenter(task?.region)} />
                <Legend legend={layer.legend} />
              </Suspense>
            </Card>
          ))}

          {result?.charts?.map((chart, i) => (
            <Card key={i} className="result-card" title={chart.title}>
              <Suspense fallback={<PanelLoading />}>
                <ChartPanel chart={chart} />
              </Suspense>
            </Card>
          ))}

          {result?.ok === false && result?.error && (
            <Card className="result-card" title="执行失败">
              <Tag color="error">{result.error.category}</Tag>
              <div style={{ marginTop: 8 }}>{result.error.message}</div>
            </Card>
          )}

          {/* 反馈行：把"用户验证"从一件要安排的事变成默认会发生的事。
              项目至今 0 真实用户，而产品价值这一条线只有用户能证明；
              访谈成本高且要等人，所以走这种最低成本的回收通道。 */}
          {task?.task_id && result?.ok !== false && !task?.is_multi && (
            <Card className="result-card" title="这次结果对你有用吗？" size="small">
              <Space direction="vertical" style={{ width: '100%' }} size={8}>
                <Space>
                  <Button
                    size="small"
                    type={feedback === 'up' ? 'primary' : 'default'}
                    loading={feedbackSending && feedback !== 'up'}
                    onClick={() => sendFeedback('up')}
                  >
                    有用
                  </Button>
                  <Button
                    size="small"
                    danger={feedback === 'down'}
                    type={feedback === 'down' ? 'primary' : 'default'}
                    loading={feedbackSending && feedback !== 'down'}
                    onClick={() => sendFeedback('down')}
                  >
                    没用
                  </Button>
                  {feedback && (
                    <span style={{ fontSize: 12, color: 'var(--ink-500)' }}>
                      已记录{feedback === 'up' ? '👍' : '👎'}
                    </span>
                  )}
                </Space>
                <Input
                  size="small"
                  placeholder="可选：一句话说明问题（例如结果偏大 / 图表看不懂）"
                  value={feedbackNote}
                  onChange={(e) => setFeedbackNote(e.target.value)}
                  onBlur={() => {
                    // 已经评过但改了备注 → 自动重新提交（否则用户会以为改动没保存）
                    if (feedback && feedbackNote.trim()) {
                      submitFeedback(task.task_id, feedback, feedbackNote).catch(() => {})
                    }
                  }}
                  maxLength={200}
                />
              </Space>
            </Card>
          )}

          {task?.code && (
            <Collapse
              items={[
                {
                  key: 'code',
                  label: '查看生成的 GEE 代码',
                  children: <pre className="code-block">{task.code}</pre>,
                },
              ]}
            />
          )}
        </div>
      </div>

      <Drawer
        title="分析偏好（Custom Instructions）"
        placement="right"
        width={380}
        open={prefsOpen}
        onClose={() => setPrefsOpen(false)}
        extra={
          <Button type="primary" size="small" loading={prefsSaving} onClick={savePrefs}>
            保存
          </Button>
        }
      >
        <Space direction="vertical" style={{ width: '100%' }} size={16}>
          <div>
            <div style={{ marginBottom: 6, fontWeight: 500 }}>助手画像</div>
            <Radio.Group
              value={prefs.agent_profile}
              onChange={(e) => setPrefs({ ...prefs, agent_profile: e.target.value })}
            >
              {profiles.map((p) => (
                <Radio.Button key={p.value} value={p.value}>
                  {p.label}
                </Radio.Button>
              ))}
            </Radio.Group>
            {profiles.length > 0 && (
              <div style={{ fontSize: 12, color: 'var(--ink-500)', marginTop: 6 }}>
                {profiles.find((p) => p.value === prefs.agent_profile)?.desc}
              </div>
            )}
          </div>
          <div>
            <div style={{ marginBottom: 6, fontWeight: 500 }}>数据源偏好</div>
            <Radio.Group
              value={prefs.data_source}
              onChange={(e) => setPrefs({ ...prefs, data_source: e.target.value })}
            >
              <Radio.Button value="auto">自动</Radio.Button>
              <Radio.Button value="sentinel2">Sentinel-2</Radio.Button>
              <Radio.Button value="landsat">Landsat</Radio.Button>
            </Radio.Group>
          </div>
          <div>
            <div style={{ marginBottom: 6, fontWeight: 500 }}>自定义分析指令</div>
            <Input.TextArea
              rows={8}
              value={prefs.instructions}
              onChange={(e) => setPrefs({ ...prefs, instructions: e.target.value })}
              placeholder={
                '写下你希望 AI 在生成代码时遵循的偏好，例如：\n' +
                '- 云掩膜优先用 Cloud Score+\n' +
                '- NDVI 用 Sentinel-2 10m 波段\n' +
                '- 统计结果按县级行政区汇总'
              }
            />
            <div style={{ fontSize: 12, color: 'var(--ink-500)', marginTop: 6 }}>
              这些指令会注入到代码生成的提示词中，影响后续生成的 GEE 代码风格与参数选择。
            </div>
          </div>
        </Space>
      </Drawer>
    </div>
  )
}
