const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";

// Agent runs (analyze/approve) can legitimately take several minutes.
const TIMEOUT_MS = 900_000;

export class ApiError extends Error {}

async function request(method, path, { json, params } = {}) {
  const url = new URL(`${API_URL}${path}`);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, value);
      }
    }
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  let response;
  try {
    response = await fetch(url, {
      method,
      headers: json ? { "Content-Type": "application/json" } : undefined,
      body: json ? JSON.stringify(json) : undefined,
      signal: controller.signal,
    });
  } catch (err) {
    throw new ApiError(`Cannot reach the SonarFix API at ${API_URL}: ${err.message}`);
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      // ignore, body wasn't json
    }
    throw new ApiError(`${method} ${path} failed (${response.status}): ${detail}`);
  }

  if (response.status === 204) return null;
  return response.json();
}

export const api = {
  health: () => request("GET", "/health"),

  listProjects: () => request("GET", "/projects"),
  syncProjects: () => request("POST", "/projects/sync"),
  onboard: (sonarUrl, repoUrl, branch, repoPath) =>
    request("POST", "/onboard", {
      json: {
        sonar_url: sonarUrl,
        repo_url: repoUrl,
        branch: branch || null,
        repo_path: repoPath || null,
      },
    }),
  setRepoPath: (key, repoPath) =>
    request("PUT", `/projects/${key}/repo`, { json: { repo_path: repoPath } }),
  autoClone: (key) => request("POST", `/projects/${key}/auto-clone`),
  syncIssues: (key) => request("POST", `/projects/${key}/sync-issues`),
  reportUrl: (key) => `${API_URL}/projects/${key}/report`,

  listIssues: (key, { severity, type } = {}) =>
    request("GET", `/projects/${key}/issues`, { params: { severity, type } }),
  facets: (key) => request("GET", `/projects/${key}/facets`),

  cloneRepo: (key, url, branch) =>
    request("POST", `/projects/${key}/clone`, { json: { url, branch: branch || null } }),
  plan: (key) => request("GET", `/projects/${key}/plan`),
  scmStatus: (key) => request("GET", `/projects/${key}/scm`),
  createBatch: (key, body) => request("POST", `/projects/${key}/batches`, { json: body }),
  listBatches: (key) => request("GET", `/projects/${key}/batches`),
  getBatch: (id) => request("GET", `/batches/${id}`),
  buildBatch: (id) => request("POST", `/batches/${id}/build`),
  retryBatch: (id, feedback) =>
    request("POST", `/batches/${id}/retry`, { json: { feedback: feedback || null } }),
  abandonBatch: (id) => request("POST", `/batches/${id}/abandon`),
  createPullRequest: (id) => request("POST", `/batches/${id}/pull-request`),

  getIssue: (id) => request("GET", `/issues/${id}`),
  analyzeIssue: (id) => request("POST", `/issues/${id}/analyze`),
  workflowState: (id) => request("GET", `/issues/${id}/state`),
  approve: (id, feedback) =>
    request("POST", `/issues/${id}/approve`, { json: { feedback: feedback || null } }),
  reject: (id, feedback) =>
    request("POST", `/issues/${id}/reject`, { json: { feedback: feedback || null } }),
  requestBuild: (id) => request("POST", `/issues/${id}/build`),
  retryBuild: (id, feedback) =>
    request("POST", `/issues/${id}/build/retry`, { json: { feedback: feedback || null } }),
  abandonBuild: (id) => request("POST", `/issues/${id}/build/abandon`),
  createIssuePullRequest: (id) => request("POST", `/issues/${id}/pull-request`),
};
