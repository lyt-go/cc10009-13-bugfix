import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { request } from '@/api/client'

const ENDPOINT = '/api/crawljob'
const columns = [
  "作业编号",
  "目标站点",
  "起始URL",
  "爬取深度",
  "URL去重策略",
  "页面解析器",
  "下载超时秒",
  "最大页面数"
]
const dnsColumns = [
  "DNS核对状态",
  "DNS生效版本",
  "DNS生效地址",
  "DNS失败原因"
]
const actions = [
  "提交作业审批",
  "启动作业",
  "停止作业"
]
const statuses = [
  "草稿",
  "作业审批中",
  "执行中",
  "已完成",
  "已停止"
]
const stats = [
  {
    "label": "作业定义总数",
    "value": 0
  },
  {
    "label": "执行中作业",
    "value": 0
  },
  {
    "label": "已完成作业",
    "value": 0
  }
]

type Row = Record<string, string | number | string[] | null>

export default function CrawljobView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('作业定义列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '抓取作业列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

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
        throw new Error(payload.message ?? '抓取作业动作未生效，请稍后重试')
      }
      setInfoMessage(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '抓取作业操作失败')
    }
  }

  async function checkDns(row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/dns-check`, {
        method: 'POST',
        body: JSON.stringify({ values: { 失败原因: '连接失败' } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? 'DNS核对失败')
      }
      setInfoMessage(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'DNS核对失败')
    }
  }

  return (
    <section className="page" data-module="crawljob">
      <header className="page-head">
        <div>
          <h2>抓取作业管理</h2>
          <p className="page-desc">定义单次抓取作业的范围和参数，包括深度、去重和资源上限。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('作业定义登记入口尚未接入审批流')}>
            登记作业定义
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出抓取作业清单
          </button>
          <Link className="btn ghost" to="/dnsresolver">前往DNS解析配置</Link>
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

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
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

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            {dnsColumns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              {dnsColumns.map((column) => {
                const value = row[column]
                return (
                  <td key={column}>
                    {Array.isArray(value) ? value.join(', ') || '—' : (value ?? '—')}
                  </td>
                )
              })}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void checkDns(row)}>
                  DNS核对
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + dnsColumns.length + 1}>暂无抓取作业数据，可先登记作业定义</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条抓取作业记录</span>
        {infoMessage && <span className="error-text" style={{ color: 'var(--accent, #2b7cff)' }}>{infoMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
