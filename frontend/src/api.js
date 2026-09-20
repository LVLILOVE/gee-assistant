import axios from 'axios'

const http = axios.create({ baseURL: '' })

// 会话过期或未登录时统一回调，避免每个请求各写一遍弹登录逻辑。
let onUnauthorized = null
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn
}

http.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err?.response?.status === 401 && onUnauthorized) onUnauthorized()
    return Promise.reject(err)
  },
)

export async function getHealth() {
  return (await http.get('/health')).data
}

export async function getMe() {
  return (await http.get('/api/auth/me')).data
}

export async function login(username, password) {
  return (await http.post('/api/auth/login', { username, password })).data
}

export async function register(username, password) {
  return (await http.post('/api/auth/register', { username, password })).data
}

export async function logout() {
  return (await http.post('/api/auth/logout')).data
}

export async function changePassword(oldPassword, newPassword) {
  return (await http.post('/api/auth/password', { old_password: oldPassword, new_password: newPassword })).data
}

export async function getGeeStatus() {
  return (await http.get('/api/gee/status')).data
}

export async function getTaskTypes() {
  return (await http.get('/api/tasks/types')).data
}

export async function getRegions() {
  return (await http.get('/api/regions')).data
}

export async function getKnowledge() {
  return (await http.get('/api/knowledge')).data
}

export async function searchKnowledge(q, taskType) {
  return (await http.get('/api/knowledge/search', { params: { q: q || '', task_type: taskType || '' } })).data
}

export async function parseIntent(text, partial) {
  return (await http.post('/api/parse', { text, partial: partial || {} })).data
}

export async function askExpert(question, history) {
  return (await http.post('/api/ask', { question, history: history || [] })).data
}

export async function planTask(text) {
  return (await http.post('/api/plan', { text })).data
}

export async function getPreferences() {
  return (await http.get('/api/preferences')).data
}

export async function getProfiles() {
  return (await http.get('/api/profiles')).data
}

export async function savePreferences(prefs) {
  return (await http.put('/api/preferences', prefs)).data
}

export async function submitTask(payload) {
  return (await http.post('/api/tasks', payload)).data
}

export async function getTask(id) {
  return (await http.get(`/api/tasks/${id}`)).data
}

export async function submitFeedback(id, value, note) {
  return (await http.post(`/api/tasks/${id}/feedback`, { value, note: note || '' })).data
}

export async function getFeedbackSummary() {
  return (await http.get('/api/feedback/summary')).data
}

export async function listTasks() {
  return (await http.get('/api/tasks')).data
}

export function reportUrl(id) {
  return `/api/tasks/${id}/report`
}
