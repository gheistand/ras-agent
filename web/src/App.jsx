import { useState, useEffect, useCallback } from 'react'
import { fetchJobs, fetchStats, submitJob, deleteJob, fetchHealth } from './api.js'

const STATUS_COLORS = {
  queued:    'bg-gray-100 text-gray-600',
  running:   'bg-blue-100 text-blue-700',
  complete:  'bg-green-100 text-green-700',
  error:     'bg-red-100 text-red-700',
}

const PHASE_LABELS = {
  terrain:     '🗺️  Terrain',
  watershed:   '🌊  Watershed',
  streamstats: '📊  StreamStats',
  hydrograph:  '📈  Hydrograph',
  mesh:        '🔲  Mesh (RAS2025)',
  model_build: '🏗️   Model Build',
  run:         '⚙️   HEC-RAS Run',
  results:     '📦  Results Export',
}

function StatusBadge({ status }) {
  return (
    <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${STATUS_COLORS[status] || STATUS_COLORS.queued}`}>
      {status}
    </span>
  )
}

function JobCard({ job, onDelete }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="font-semibold text-navy">{job.name}</h3>
          <p className="text-sm text-gray-500 mt-0.5">
            {job.project_dir} · return period: {job.return_period_yr ?? '—'}yr
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <StatusBadge status={job.status} />
          {job.status === 'queued' && (
            <button
              onClick={() => onDelete(job.id)}
              title="Delete queued job"
              className="text-xs text-red-400 hover:text-red-600 transition-colors"
            >
              🗑️
            </button>
          )}
          <button
            onClick={() => setExpanded(e => !e)}
            className="text-xs text-teal hover:underline"
          >
            {expanded ? 'hide' : 'details'}
          </button>
        </div>
      </div>

      {/* Phase progress bar */}
      <div className="mt-3 flex gap-1">
        {Object.entries(PHASE_LABELS).map(([key, label]) => {
          const phaseStatus = job.phases?.[key] || 'queued'
          const color = phaseStatus === 'complete' ? 'bg-teal' :
                        phaseStatus === 'running'  ? 'bg-blue-400 animate-pulse' :
                        phaseStatus === 'error'    ? 'bg-red-400' :
                        'bg-gray-200'
          return (
            <div key={key} className="flex-1 group relative">
              <div className={`h-2 rounded-sm ${color}`} />
              <div className="absolute -top-7 left-1/2 -translate-x-1/2 bg-navy text-white text-xs px-2 py-0.5 rounded opacity-0 group-hover:opacity-100 whitespace-nowrap pointer-events-none z-10">
                {label}: {phaseStatus}
              </div>
            </div>
          )
        })}
      </div>

      {expanded && (
        <div className="mt-3 border-t pt-3">
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm">
            {Object.entries(PHASE_LABELS).map(([key, label]) => (
              <div key={key} className="flex justify-between">
                <span className="text-gray-500">{label}</span>
                <StatusBadge status={job.phases?.[key] || 'queued'} />
              </div>
            ))}
          </div>
          <div className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-gray-500">
            <div>Created: {job.created_at ? new Date(job.created_at).toLocaleString() : '—'}</div>
            {job.started_at && <div>Started: {new Date(job.started_at).toLocaleString()}</div>}
            {job.completed_at && <div>Completed: {new Date(job.completed_at).toLocaleString()}</div>}
            {job.error_msg && <div className="col-span-2 text-red-500">Error: {job.error_msg}</div>}
          </div>
          {job.results && (
            <div className="mt-3 flex gap-2 flex-wrap">
              {job.results.shapefile && (
                <a href={job.results.shapefile} className="text-xs bg-teal-light text-teal px-3 py-1 rounded-lg hover:bg-teal hover:text-white transition-colors">
                  ⬇ Shapefile
                </a>
              )}
              {job.results.geopackage && (
                <a href={job.results.geopackage} className="text-xs bg-teal-light text-teal px-3 py-1 rounded-lg hover:bg-teal hover:text-white transition-colors">
                  ⬇ GeoPackage
                </a>
              )}
              {job.results.depth_grid && (
                <a href={job.results.depth_grid} className="text-xs bg-teal-light text-teal px-3 py-1 rounded-lg hover:bg-teal hover:text-white transition-colors">
                  ⬇ Depth Grid (COG)
                </a>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function SubmitJobForm({ onSubmit }) {
  const [form, setForm] = useState({
    name: '',
    project_dir: '',
    plan_hdf: '',
    geom_ext: 'g01',
    return_period_yr: 100,
  })
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setLoading(true)
    try {
      await onSubmit(form)
      setForm({ name: '', project_dir: '', plan_hdf: '', geom_ext: 'g01', return_period_yr: 100 })
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
      <h2 className="font-semibold text-navy mb-4">Submit New Model Job</h2>
      <div className="grid grid-cols-2 gap-4">
        <div className="col-span-2">
          <label className="block text-sm font-medium text-gray-700 mb-1">Job Name</label>
          <input
            className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-teal"
            placeholder="e.g. Sangamon River at Monticello — 100yr"
            value={form.name}
            onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
            required
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Project Directory</label>
          <input
            className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-teal"
            placeholder="/path/to/hecras/project"
            value={form.project_dir}
            onChange={e => setForm(f => ({ ...f, project_dir: e.target.value }))}
            required
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Plan HDF File</label>
          <input
            className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-teal"
            placeholder="project.p01.hdf"
            value={form.plan_hdf}
            onChange={e => setForm(f => ({ ...f, plan_hdf: e.target.value }))}
            required
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Geometry Extension</label>
          <input
            className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-teal"
            placeholder="g01"
            value={form.geom_ext}
            onChange={e => setForm(f => ({ ...f, geom_ext: e.target.value }))}
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Return Period (yr)</label>
          <select
            className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-teal"
            value={form.return_period_yr}
            onChange={e => setForm(f => ({ ...f, return_period_yr: Number(e.target.value) }))}
          >
            {[2, 5, 10, 25, 50, 100, 500].map(rp => (
              <option key={rp} value={rp}>{rp}-yr</option>
            ))}
          </select>
        </div>
      </div>
      <div className="mt-4 flex justify-end">
        <button
          type="submit"
          disabled={loading}
          className="bg-teal text-white px-6 py-2 rounded-lg text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
        >
          {loading ? 'Submitting...' : '🚀 Submit Job'}
        </button>
      </div>
    </form>
  )
}

// ── Main App ─────────────────────────────────────────────────────────────────

export default function App() {
  const [jobs, setJobs] = useState([])
  const [stats, setStats] = useState({ total: 0, queued: 0, running: 0, complete: 0, error: 0 })
  const [apiOnline, setApiOnline] = useState(null)   // null = unknown, true/false
  const [fetchError, setFetchError] = useState(false)

  const loadJobs = useCallback(async () => {
    try {
      const data = await fetchJobs()
      setJobs(data)
      setFetchError(false)
    } catch {
      setFetchError(true)
    }
  }, [])

  const loadStats = useCallback(async () => {
    try {
      const data = await fetchStats()
      setStats(data)
    } catch {
      // stats failure is non-critical; keep last known values
    }
  }, [])

  const checkHealth = useCallback(async () => {
    try {
      await fetchHealth()
      setApiOnline(true)
    } catch {
      setApiOnline(false)
    }
  }, [])

  // Initial load + polling every 10s
  useEffect(() => {
    loadJobs()
    loadStats()
    const interval = setInterval(() => {
      loadJobs()
      loadStats()
    }, 10_000)
    return () => clearInterval(interval)
  }, [loadJobs, loadStats])

  // Health check every 30s
  useEffect(() => {
    checkHealth()
    const interval = setInterval(checkHealth, 30_000)
    return () => clearInterval(interval)
  }, [checkHealth])

  const handleSubmit = async (form) => {
    await submitJob(form)
    await Promise.all([loadJobs(), loadStats()])
  }

  const handleDelete = async (jobId) => {
    try {
      await deleteJob(jobId)
      setJobs(j => j.filter(job => job.id !== jobId))
      loadStats()
    } catch (err) {
      alert(`Could not delete job: ${err.message}`)
    }
  }

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-navy text-white px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="text-2xl">🌊</span>
          <div>
            <h1 className="text-lg font-bold leading-none">RAS Agent</h1>
            <p className="text-xs text-blue-200 mt-0.5">Automated 2D HEC-RAS Modeling Pipeline</p>
          </div>
        </div>
        <div className="flex items-center gap-4">
          {/* Connection status */}
          <div className="flex items-center gap-1.5 text-xs">
            {apiOnline === null ? (
              <span className="w-2 h-2 rounded-full bg-gray-400 inline-block" />
            ) : apiOnline ? (
              <span className="w-2 h-2 rounded-full bg-green-400 inline-block" />
            ) : (
              <>
                <span className="w-2 h-2 rounded-full bg-red-400 inline-block" />
                <span className="text-red-300">API offline</span>
              </>
            )}
          </div>
          <div className="text-xs text-blue-200">
            CHAMP · Illinois State Water Survey
          </div>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 py-6 space-y-6">
        {/* API error banner */}
        {fetchError && (
          <div className="bg-yellow-50 border border-yellow-200 text-yellow-800 text-sm px-4 py-3 rounded-lg">
            Could not reach API — showing cached data
          </div>
        )}

        {/* Stats row */}
        <div className="grid grid-cols-4 gap-4">
          {[
            { label: 'Total Jobs',  value: stats.total,    color: 'text-navy' },
            { label: 'Running',     value: stats.running,  color: 'text-blue-600' },
            { label: 'Complete',    value: stats.complete, color: 'text-teal' },
            { label: 'Queued',      value: stats.queued,   color: 'text-gray-500' },
          ].map(({ label, value, color }) => (
            <div key={label} className="bg-white rounded-xl border border-gray-200 shadow-sm p-4 text-center">
              <div className={`text-3xl font-bold ${color}`}>{value}</div>
              <div className="text-xs text-gray-500 mt-1">{label}</div>
            </div>
          ))}
        </div>

        {/* Submit form */}
        <SubmitJobForm onSubmit={handleSubmit} />

        {/* Job list */}
        <div>
          <h2 className="font-semibold text-navy mb-3">Model Jobs</h2>
          <div className="space-y-3">
            {jobs.length === 0 ? (
              <p className="text-sm text-gray-400 text-center py-8">No jobs yet. Submit one above.</p>
            ) : (
              jobs.map(job => (
                <JobCard key={job.id} job={job} onDelete={handleDelete} />
              ))
            )}
          </div>
        </div>
      </main>

      <footer className="text-center text-xs text-gray-400 py-6">
        RAS Agent · Apache 2.0 · Built at CHAMP, Illinois State Water Survey ·{' '}
        <a href="https://github.com/gheistand/ras-agent" className="hover:text-teal">GitHub</a>
      </footer>
    </div>
  )
}
