import { useState, useEffect, useCallback } from 'react'
import ScanWarning from './ScanWarning'

// What each category means for an operator. Nothing here means "safe to delete".
const CATEGORIES = {
  candidate: {
    label: 'Candidate', className: 'badge-red',
    desc: 'Not claimed by any configured cluster and not known to CNS. Needs investigation.',
  },
  orphan_known_to_cns: {
    label: 'Known to CNS', className: 'badge-yellow',
    desc: 'Not claimed, but CNS records it for a configured cluster. Needs investigation.',
  },
  foreign: {
    label: 'Other cluster', className: 'badge-blue',
    desc: 'CNS records a cluster that is not configured here; probably another cluster\'s disk.',
  },
  inconsistent: {
    label: 'Inconsistent', className: 'badge-yellow',
    desc: 'Claimed by a configured cluster, but CNS disagrees. Report only.',
  },
  unknown: {
    label: 'Unknown', className: 'badge-gray',
    desc: 'Cannot be decided: CNS state unknown or the CNS cluster-id cannot be placed.',
  },
}

const FILTERS = [
  { val: 'actionable', label: 'Needs investigation' },
  { val: 'all',        label: 'All' },
  ...Object.entries(CATEGORIES).map(([val, c]) => ({ val, label: c.label })),
]

function formatDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

function formatAge(hours) {
  if (hours == null) return 'unknown'
  if (hours < 48) return `${Math.round(hours)} h`
  return `${Math.round(hours / 24)} d`
}

function AlertPanel({ status, testResult }) {
  if (!status) return null
  const { configured, alert_to, check_interval_hours, orphans = {} } = status
  const { last_check, last_alert, last_count, last_error } = orphans

  return (
    <div style={{
      background: configured ? 'rgba(60,180,100,0.06)' : 'var(--surface)',
      border: `1px solid ${configured ? 'rgba(60,180,100,0.3)' : 'var(--border)'}`,
      borderRadius: 8,
      padding: '12px 16px',
      marginBottom: 16,
      display: 'flex',
      alignItems: 'center',
      gap: 12,
      flexWrap: 'wrap',
    }}>
      <span style={{ fontSize: 15 }}>{configured ? '🔔' : '🔕'}</span>
      <strong style={{ fontSize: 13, whiteSpace: 'nowrap' }}>Unclaimed Disk Alerts</strong>
      <span className={`badge ${configured ? 'badge-green' : 'badge-gray'}`} style={{ fontSize: 11 }}>
        {configured ? 'Configured' : 'Not configured'}
      </span>

      <div style={{ flex: 1, fontSize: 12, color: 'var(--text-secondary)' }}>
        {configured ? (
          <>
            <span>Every <strong style={{ color: 'var(--text-primary)' }}>{check_interval_hours}h</strong></span>
            <span style={{ margin: '0 6px' }}>·</span>
            <span>To: <strong style={{ color: 'var(--text-primary)' }}>{(alert_to || []).join(', ')}</strong></span>
            {last_check && (
              <>
                <span style={{ margin: '0 6px' }}>·</span>
                <span>Last check: {formatDate(last_check)}</span>
                {last_count != null && <span> ({last_count} need investigation)</span>}
              </>
            )}
            {last_alert && (
              <>
                <span style={{ margin: '0 6px' }}>·</span>
                <span>Last alert: {formatDate(last_alert)}</span>
              </>
            )}
            {last_error && (
              <span style={{ color: 'var(--danger, #e05252)', marginLeft: 8 }}>⚠ {last_error}</span>
            )}
          </>
        ) : (
          <span>
            Set <code>SMTP_HOST</code>, <code>SMTP_FROM</code>, and <code>ALERT_EMAIL_TO</code> to enable.
          </span>
        )}
      </div>

      {testResult && (
        <span style={{
          fontSize: 12, fontWeight: 500, whiteSpace: 'nowrap',
          color: testResult.ok ? 'var(--success, #3cb464)' : 'var(--danger, #e05252)',
        }}>
          {testResult.ok ? '✓' : '✗'} {testResult.msg}
        </span>
      )}
    </div>
  )
}

function AttachedCell({ item }) {
  if (item.attached === 'yes') {
    return <span className="badge badge-yellow" title={`Power: ${item.vm_power_state}`}>{item.vm_name}</span>
  }
  if (item.attached === 'unknown') {
    return <span className="badge badge-gray" title="The VM scan was incomplete">unknown</span>
  }
  return <span style={{ color: 'var(--text-secondary)' }}>no</span>
}

function CnsCell({ item }) {
  if (item.cns_known == null) return <span className="badge badge-gray">unknown</span>
  if (!item.cns_known) return <span style={{ color: 'var(--text-secondary)' }}>not known</span>
  return (
    <span className="td-mono" title={item.cns_cluster_id || ''}>
      {item.cns_cluster || item.cns_cluster_id || 'no cluster-id'}
    </span>
  )
}

export default function OrphanedFCDs({ onCount }) {
  const [scan, setScan]           = useState(null)
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState(null)
  const [spinning, setSpinning]   = useState(false)
  const [search, setSearch]       = useState('')
  const [filter, setFilter]       = useState('actionable')
  const [alertStatus, setAlertStatus] = useState(null)
  const [testing, setTesting]     = useState(false)
  const [testResult, setTestResult]   = useState(null)

  const fetchData = useCallback(async () => {
    setSpinning(true)
    setError(null)
    try {
      const res = await fetch('/api/orphaned-fcds')
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || res.statusText)
      }
      const json = await res.json()
      setScan(json)
      onCount?.(json.counts.actionable)
    } catch (e) {
      setScan(null)
      setError(e.message)
    } finally {
      setLoading(false)
      setSpinning(false)
    }
  }, [])

  const fetchAlertStatus = useCallback(async () => {
    try {
      const res = await fetch('/api/alerts/status')
      setAlertStatus(res.ok ? await res.json() : null)
    } catch {
      setAlertStatus(null)
    }
  }, [])

  useEffect(() => {
    fetchData()
    fetchAlertStatus()
  }, [fetchData, fetchAlertStatus])

  const sendTestAlert = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      const res = await fetch('/api/alerts/orphan-test', { method: 'POST' })
      const data = await res.json()
      if (!res.ok) {
        setTestResult({ ok: false, msg: data.detail || 'Failed' })
      } else if (data.sent) {
        setTestResult({
          ok: true,
          msg: `Sent to ${(data.recipients || []).join(', ')} · ${data.count} need investigation`
            + (data.complete === false ? ' · scan incomplete' : ''),
        })
      } else {
        setTestResult({ ok: false, msg: data.error || data.reason || 'Not sent' })
      }
    } catch (e) {
      setTestResult({ ok: false, msg: e.message })
    } finally {
      setTesting(false)
      fetchAlertStatus()
    }
  }

  const items  = scan?.disks || []
  const counts = scan?.counts || {}

  const afterFilter = items.filter(i =>
    filter === 'all' ? true : filter === 'actionable' ? i.actionable : i.category === filter
  )
  const q = search.toLowerCase()
  const visible = q
    ? afterFilter.filter(i =>
        [i.fcd_id, i.fcd_name, i.datastore, i.vm_name, i.file_path, i.cns_cluster, i.cns_cluster_id, ...(i.claimed_by || [])]
          .some(v => (v || '').toLowerCase().includes(q))
      )
    : afterFilter

  const actionableGB = items.filter(i => i.actionable).reduce((s, i) => s + (i.capacity_gb || 0), 0)

  return (
    <section>
      <div className="section-header">
        <div>
          <div className="section-title">Unclaimed Disks</div>
          <div className="section-subtitle">
            First Class Disks on the scanned datastores that are not claimed by any configured Kubernetes cluster,
            or that CNS and Kubernetes disagree about. Every entry needs investigation; nothing here establishes
            that a disk can be removed.
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button
            className="btn"
            onClick={sendTestAlert}
            disabled={testing}
            title="Send a test unclaimed-disk alert email now"
          >
            {testing ? '…' : '✉ Test Alert'}
          </button>
          <button className="btn" onClick={fetchData} disabled={spinning}>
            {spinning ? <span className="spin-icon">↻</span> : '↻'} Refresh
          </button>
        </div>
      </div>

      <AlertPanel status={alertStatus} testResult={testResult} />

      {loading && (
        <div className="loading-state">
          <div className="spinner" />
          Scanning datastores…
        </div>
      )}

      {error && (
        <div className="error-state">
          <span>⚠</span>
          <span>No result: {error}</span>
        </div>
      )}

      {scan && (
        <>
          {!scan.complete && (
            <ScanWarning
              title="Incomplete scan"
              text="Parts of vCenter could not be read. Disks may be missing from this list, and a disk that is not listed may still be unclaimed."
              problems={scan.problems}
            />
          )}
          {scan.cns !== 'ok' && (
            <ScanWarning
              title={scan.cns === 'disabled' ? 'CNS query disabled' : 'CNS state unknown'}
              text="Without Cloud Native Storage data no disk can be placed in a category, so every unclaimed disk is Unknown and none is listed as needing investigation."
            />
          )}

          <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 12 }}>
            Clusters consulted:{' '}
            {scan.clusters.map((c, i) => (
              <span key={c.name}>
                {i > 0 && ', '}
                <strong style={{ color: 'var(--text-primary)' }}>{c.name}</strong>
                {c.csi_cluster_id ? <span className="td-mono"> ({c.csi_cluster_id})</span> : ' (no csi_cluster_id)'}
              </span>
            ))}
            <span style={{ margin: '0 6px' }}>·</span>
            Scanned {formatDate(scan.scanned_at)}
            <span style={{ margin: '0 6px' }}>·</span>
            Minimum age {scan.min_age_hours} h
          </div>

          <div className="stats-bar">
            <div className="stat-card" style={counts.actionable > 0 ? { borderColor: 'rgba(248,81,73,.5)' } : undefined}>
              <div className="stat-value" style={counts.actionable > 0 ? { color: 'var(--danger)' } : undefined}>
                {counts.actionable}
              </div>
              <div className="stat-label">Need investigation</div>
            </div>
            <div className="stat-card">
              <div className="stat-value">{actionableGB.toFixed(1)} GB</div>
              <div className="stat-label">Capacity of those disks</div>
            </div>
            <div className="stat-card">
              <div className="stat-value">{counts.in_use}</div>
              <div className="stat-label">Claimed (in use)</div>
            </div>
          </div>

          <div style={{ display: 'flex', gap: 6, marginBottom: 12, flexWrap: 'wrap' }}>
            {FILTERS.map(({ val, label }) => {
              const count = val === 'all' ? items.length : counts[val] ?? 0
              const isActive = filter === val
              return (
                <button
                  key={val}
                  onClick={() => setFilter(val)}
                  title={CATEGORIES[val]?.desc}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    padding: '4px 12px',
                    border: `1px solid ${isActive ? 'var(--accent)' : 'var(--border)'}`,
                    borderRadius: 20,
                    background: isActive ? 'color-mix(in srgb, var(--accent) 12%, transparent)' : 'transparent',
                    color: isActive ? 'var(--text-primary)' : 'var(--text-secondary)',
                    fontSize: 12, fontWeight: isActive ? 600 : 400,
                    cursor: 'pointer', transition: 'all .15s',
                  }}
                >
                  {label}
                  <span style={{
                    background: 'var(--bg-primary)', padding: '0 5px', borderRadius: 8,
                    fontSize: 11, color: 'var(--text-secondary)', border: '1px solid var(--border)',
                  }}>
                    {count}
                  </span>
                </button>
              )
            })}
          </div>

          <div className="filter-row">
            <input
              type="search"
              className="search-input"
              placeholder="Search FCD ID, name, datastore, VM, cluster…"
              value={search}
              onChange={e => setSearch(e.target.value)}
              style={{ width: 340 }}
            />
            {search && (
              <span className="filter-count">
                {visible.length} result{visible.length !== 1 ? 's' : ''}
              </span>
            )}
          </div>

          {visible.length === 0 ? (
            <div className="empty-state">
              <div>
                {scan.complete && scan.cns === 'ok'
                  ? 'No disk in this view.'
                  : 'No disk in this view, but the scan above is incomplete or CNS is unknown.'}
              </div>
            </div>
          ) : (
            <div className="table-container">
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Category</th>
                      <th>FCD ID</th>
                      <th>Name</th>
                      <th>Datastore</th>
                      <th>Size (GB)</th>
                      <th>Claimed by</th>
                      <th>CNS cluster</th>
                      <th>Attached</th>
                      <th>Age</th>
                      <th>File Path</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map(item => {
                      const cat = CATEGORIES[item.category] || CATEGORIES.unknown
                      return (
                        <tr key={item.fcd_id}>
                          <td>
                            <span className={`badge ${cat.className}`} title={item.reason}>{cat.label}</span>
                            {item.actionable && (
                              <div style={{ fontSize: 11, color: 'var(--danger)', marginTop: 2 }}>needs investigation</div>
                            )}
                          </td>
                          <td className="td-mono" style={{ fontSize: 11 }}>{item.fcd_id}</td>
                          <td>
                            {item.fcd_name}
                            {item.looks_like_csi && (
                              <span className="badge badge-gray" style={{ marginLeft: 6, fontSize: 10 }}
                                    title="Name follows the vSphere CSI pattern (informational)">CSI name</span>
                            )}
                          </td>
                          <td>{item.datastore}</td>
                          <td>{item.capacity_gb}</td>
                          <td>{item.claimed_by.length ? item.claimed_by.join(', ') : '—'}</td>
                          <td><CnsCell item={item} /></td>
                          <td><AttachedCell item={item} /></td>
                          <td title={formatDate(item.created)}>
                            {formatAge(item.age_hours)}
                            {item.too_young && <span className="badge badge-blue" style={{ marginLeft: 6, fontSize: 10 }}>too young</span>}
                          </td>
                          <td
                            className="td-mono"
                            style={{ fontSize: 11, maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                            title={item.file_path}
                          >
                            {item.file_path}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 12 }}>
            {Object.values(CATEGORIES).map(c => (
              <div key={c.label} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text-secondary)' }}>
                <span className={`badge ${c.className}`} style={{ fontSize: 11 }}>{c.label}</span>
                {c.desc}
              </div>
            ))}
            <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
              "Needs investigation" = Candidate or Known to CNS, not attached to a VM and at least {scan.min_age_hours} hours old.
            </div>
          </div>
        </>
      )}
    </section>
  )
}
