/**
 * api.js — RAS Agent API client
 *
 * Thin fetch-based client for the FastAPI backend.
 * Base URL is read from VITE_API_URL env var, falling back to localhost.
 */

const BASE_URL = import.meta.env.VITE_API_URL || "http://localhost:8000"

async function _request(method, path, body = null) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json" },
  }
  if (body !== null) {
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(`${BASE_URL}${path}`, opts)
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `HTTP ${res.status}`)
  }
  // 204 No Content — no body to parse
  if (res.status === 204) return null
  return res.json()
}

export async function fetchJobs(status = null) {
  const qs = status ? `?status=${encodeURIComponent(status)}` : ""
  return _request("GET", `/api/jobs${qs}`)
}

export async function fetchJob(jobId) {
  return _request("GET", `/api/jobs/${encodeURIComponent(jobId)}`)
}

export async function submitJob(jobData) {
  return _request("POST", "/api/jobs", jobData)
}

export async function deleteJob(jobId) {
  return _request("DELETE", `/api/jobs/${encodeURIComponent(jobId)}`)
}

export async function fetchStats() {
  return _request("GET", "/api/stats")
}

export async function fetchHealth() {
  return _request("GET", "/api/health")
}

export async function fetchJobResults(jobId) {
  return _request("GET", `/api/jobs/${encodeURIComponent(jobId)}/results`)
}

export async function fetchFloodExtent(jobId, returnPeriod = null) {
  const qs = returnPeriod != null ? `?return_period=${returnPeriod}` : ""
  return _request("GET", `/api/jobs/${encodeURIComponent(jobId)}/results/flood-extent${qs}`)
}

export async function fetchDepthStats(jobId) {
  return _request("GET", `/api/jobs/${encodeURIComponent(jobId)}/results/depth-stats`)
}
