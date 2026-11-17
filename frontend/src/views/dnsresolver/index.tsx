import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { request } from '@/api/client'

const ENDPOINT = '/api/dnsresolver'
const columns = [
  "配置编号",
  "域名模式",
  "DNS服务器",
  "解析超时毫秒",
  "缓存TTL秒",
  "A记录策略",
  "IPv6优先",
  "故障转移地址"
]
const editableFields = [
  "DNS服务器",
  "解析超时毫秒",
  "缓存TTL秒",
  "A记录策略",
  "IPv6优先",
  "故障转移地址"
]
const actions = [
  "提交配置测试",
  "启用配置",
  "降级配置"
]
const statuses = [
  "草稿",
  "配置测试中",
  "已启用",
  "已降级"
]

type Row = Record<string, string | number | null>
type Dataset = {
  域名模式: string
  生效版本: string | null
  生效配置版本: number
  生效地址: string[]
  故障转移地址: string[]
  缓存TTL秒: number
  结果状态: string
  刷新来源: string | null
  刷新时间: string | null
  最近失败: Record<string, unknown> | null
  版本历史: number[]
  原始请求: number[]
}
type VersionRow = {
  id: number
  版本键: string
  配置版本: number
  地址: string[]
  故障转移地址: string[]
  优先级: number
  来源: string
  状态: string
  创建时间: string
}
type RequestRow = {
  id: number
  幂等键: string | null
  来源: string
  配置版本: number
  处理结果: string
  请求时间: string
}
type FailureRow = {
  id: number
  来源: string
  配置版本: number
  失败原因: string
  失败时间: string
  生效版本: string | null
}

const emptyForm: Record<string, string> = {
  配置编号: '',
  域名模式: '',
  DNS服务器: '',
  解析超时毫秒: '3000',
  缓存TTL秒: '300',
  A记录策略: '',
  IPv6优先: '否',
  故障转移地址: ''
}

export default function DnsresolverView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [form, setForm] = useState<Record<string, string>>(emptyForm)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [activeDomain, setActiveDomain] = useState<string | null>(null)
  const [versions, setVersions] = useState<VersionRow[]>([])
  const [requests, setRequests] = useState<RequestRow[]>([])
  const [failures, setFailures] = useState<FailureRow[]>([])
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('解析配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'DNS解析列表读取失败')
    }
  }, [filters])

  const reloadDatasets = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/resolutions`)
      if (response.ok) {
        const payload = await response.json()
        setDatasets(payload.items ?? [])
      }
    } catch {
      // 数据集读取失败不阻塞配置页面，页脚会展示具体错误
    }
  }, [])

  const loadTrace = useCallback(async (domain: string) => {
    setActiveDomain(domain)
    const [vResp, rResp, fResp] = await Promise.all([
      request(`${ENDPOINT}/resolutions/${encodeURIComponent(domain)}/versions`),
      request(`${ENDPOINT}/resolutions/${encodeURIComponent(domain)}/requests`),
      request(`${ENDPOINT}/resolutions/${encodeURIComponent(domain)}/failures`)
    ])
    if (vResp.ok) setVersions((await vResp.json()).items ?? [])
    if (rResp.ok) setRequests((await rResp.json()).items ?? [])
    if (fResp.ok) setFailures((await fResp.json()).items ?? [])
  }, [])

  useEffect(() => {
    void reload()
    void reloadDatasets()
  }, [reload, reloadDatasets])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? 'DNS解析动作未生效，请稍后重试')
      }
      setInfoMessage(payload.message)
      await reload()
      await reloadDatasets()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'DNS解析操作失败')
    }
  }

  async function submitForm() {
    setErrorMessage('')
    setInfoMessage('')
    const values: Record<string, string> = {}
    for (const field of columns) {
      if (form[field]?.trim()) values[field] = form[field].trim()
    }
    try {
      const url = editingId === null ? ENDPOINT : `${ENDPOINT}/${editingId}`
      const method = editingId === null ? 'POST' : 'PUT'
      const response = await request(url, { method, body: JSON.stringify({ values }) })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '解析配置保存失败')
      }
      setInfoMessage(payload.message)
      setForm(emptyForm)
      setEditingId(null)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '解析配置保存失败')
    }
  }

  function startEdit(row: Row) {
    setEditingId(Number(row.id))
    const next: Record<string, string> = { ...emptyForm }
    for (const field of columns) next[field] = String(row[field] ?? '')
    setForm(next)
    setErrorMessage('')
    setInfoMessage('')
  }

  async function refreshResolution(row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/refresh`, {
        method: 'POST',
        body: JSON.stringify({
          values: {
            域名模式: String(row.域名模式 ?? ''),
            配置编号: String(row.配置编号 ?? ''),
            来源: 'DNS配置页面'
          }
        }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? 'DNS解析刷新失败')
      }
      setInfoMessage(payload.message)
      await reloadDatasets()
      if (activeDomain === row.域名模式) await loadTrace(String(row.域名模式))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'DNS解析刷新失败')
    }
  }

  const stats = useMemo(() => [
    { label: '解析配置总数', value: rows.length },
    { label: '已启用配置', value: rows.filter((r) => r.status === '已启用').length },
    { label: '唯一解析结果数据集', value: datasets.length },
    { label: '冲突/失败数据集', value: datasets.filter((d) => d.结果状态 === '冲突' || d.最近失败).length }
  ], [rows, datasets])

  return (
    <section className="page" data-module="dnsresolver">
      <header className="page-head">
        <div>
          <h2>DNS解析管理</h2>
          <p className="page-desc">管理采集域名的DNS解析策略，配置随版本持久化，解析结果按域名只保留一份生效版本。</p>
        </div>
        <div className="page-actions">
          <Link className="btn ghost" to="/crawljob">前往抓取作业核对入口</Link>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出DNS解析清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form
        className="filter-bar"
        onSubmit={(event) => { event.preventDefault(); void reload() }}
      >
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <div className="filter-bar" style={{ flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <label className="filter-item">
          <span>配置编号</span>
          <input
            value={form.配置编号}
            disabled={editingId !== null}
            placeholder="如 DNSR-0010"
            onChange={(e) => setForm((p) => ({ ...p, 配置编号: e.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>域名模式</span>
          <input
            value={form.域名模式}
            disabled={editingId !== null}
            placeholder="如 api.example.com 或 *.example.com"
            onChange={(e) => setForm((p) => ({ ...p, 域名模式: e.target.value }))}
          />
        </label>
        {editableFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={form[field]}
              placeholder={field}
              onChange={(e) => setForm((p) => ({ ...p, [field]: e.target.value }))}
            />
          </label>
        ))}
        <button className="btn primary" type="button" onClick={() => void submitForm()}>
          {editingId === null ? '登记解析配置' : '保存配置修改'}
        </button>
        {editingId !== null && (
          <button
            className="btn ghost"
            type="button"
            onClick={() => { setEditingId(null); setForm(emptyForm) }}
          >
            取消编辑
          </button>
        )}
      </div>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态/版本</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>
                <div>{String(row.status ?? '—')}</div>
                <small>配置版本 v{String(row.配置版本 ?? 1)}</small>
              </td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => startEdit(row)}>编辑参数</button>
                <button className="link" type="button" onClick={() => void refreshResolution(row)}>刷新解析</button>
                <button
                  className="link"
                  type="button"
                  onClick={() => void loadTrace(String(row.域名模式 ?? ''))}
                >
                  解析证据
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无DNS解析数据，可先登记解析配置</td>
            </tr>
          )}
        </tbody>
      </table>

      <h3 style={{ marginTop: 24 }}>唯一DNS解析结果数据集（每个域名模式一条）</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>域名模式</th>
            <th>结果状态</th>
            <th>生效版本</th>
            <th>生效地址</th>
            <th>故障转移地址</th>
            <th>缓存TTL秒</th>
            <th>刷新来源/时间</th>
            <th>最近失败</th>
            <th>追溯</th>
          </tr>
        </thead>
        <tbody>
          {datasets.map((ds) => (
            <tr key={ds.域名模式}>
              <td>{ds.域名模式}</td>
              <td>{ds.结果状态}</td>
              <td>{ds.生效版本 ?? '—'}</td>
              <td>{ds.生效地址.join(', ') || '—'}</td>
              <td>{ds.故障转移地址.join(', ') || '—'}</td>
              <td>{ds.缓存TTL秒}</td>
              <td>
                <div>{ds.刷新来源 ?? '—'}</div>
                <small>{ds.刷新时间 ?? ''}</small>
              </td>
              <td>{ds.最近失败 ? String((ds.最近失败 as Record<string, string>).原因 ?? '查看失败记录') : '—'}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void loadTrace(ds.域名模式)}>
                  版本/请求/失败
                </button>
              </td>
            </tr>
          ))}
          {datasets.length === 0 && (
            <tr><td className="empty-state" colSpan={9}>暂无解析结果，启用配置后点击“刷新解析”生成</td></tr>
          )}
        </tbody>
      </table>

      {activeDomain && (
        <div style={{ marginTop: 24 }}>
          <h3>解析证据：{activeDomain}</h3>
          <h4>版本快照（冲突时两份版本均保留，按优先级裁决）</h4>
          <table className="data-table">
            <thead>
              <tr><th>版本键</th><th>地址</th><th>故障转移</th><th>优先级</th><th>来源</th><th>状态</th><th>创建时间</th></tr>
            </thead>
            <tbody>
              {versions.map((v) => (
                <tr key={v.id}>
                  <td>{v.版本键}</td>
                  <td>{v.地址.join(', ')}</td>
                  <td>{v.故障转移地址.join(', ') || '—'}</td>
                  <td>{v.优先级}</td>
                  <td>{v.来源}</td>
                  <td>{v.状态}</td>
                  <td>{v.创建时间}</td>
                </tr>
              ))}
              {versions.length === 0 && <tr><td className="empty-state" colSpan={7}>暂无版本快照</td></tr>}
            </tbody>
          </table>

          <h4>原始解析请求（与唯一数据集对应）</h4>
          <table className="data-table">
            <thead>
              <tr><th>请求id</th><th>幂等键</th><th>来源</th><th>配置版本</th><th>处理结果</th><th>请求时间</th></tr>
            </thead>
            <tbody>
              {requests.map((r) => (
                <tr key={r.id}>
                  <td>{r.id}</td>
                  <td>{r.幂等键 ?? '—'}</td>
                  <td>{r.来源}</td>
                  <td>v{r.配置版本}</td>
                  <td>{r.处理结果}</td>
                  <td>{r.请求时间}</td>
                </tr>
              ))}
              {requests.length === 0 && <tr><td className="empty-state" colSpan={6}>暂无原始请求</td></tr>}
            </tbody>
          </table>

          <h4>失败来源（携带当时生效版本）</h4>
          <table className="data-table">
            <thead>
              <tr><th>失败id</th><th>来源</th><th>请求版本</th><th>生效版本</th><th>失败原因</th><th>失败时间</th></tr>
            </thead>
            <tbody>
              {failures.map((f) => (
                <tr key={f.id}>
                  <td>{f.id}</td>
                  <td>{f.来源}</td>
                  <td>v{f.配置版本}</td>
                  <td>{f.生效版本 ?? '—'}</td>
                  <td>{f.失败原因}</td>
                  <td>{f.失败时间}</td>
                </tr>
              ))}
              {failures.length === 0 && <tr><td className="empty-state" colSpan={6}>暂无失败记录</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条DNS解析记录，{datasets.length} 条唯一解析结果数据集</span>
        {infoMessage && <span className="error-text" style={{ color: 'var(--accent, #2b7cff)' }}>{infoMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
