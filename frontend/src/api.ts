export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '');

export type JsonRecord = Record<string, unknown>;

export interface TaskStatus<T = JsonRecord> {
  task_id: string;
  status: string;
  payload?: JsonRecord | null;
  result?: T | null;
  created_at?: string;
  updated_at?: string;
  message?: string;
}

export interface RepoIngestionResult extends JsonRecord {
  repository_id: string;
  repository_url: string;
  organization_id: string;
  modules_count: number;
  files_count: number;
  docs_count: number;
  authors_count: number;
  chunks_count: number;
  code_chunks_count: number;
  doc_chunks_count: number;
  error?: string | null;
}

export interface RepositorySummary {
  id: string;
  name: string;
  organization_id: string;
  url: string;
  default_branch: string;
  modules_count: number;
  files_count: number;
  docs_count: number;
}

export interface RepositoryListResponse {
  repositories: RepositorySummary[];
  total: number;
}

export function listRepositories(organizationId?: string): Promise<RepositoryListResponse> {
  const query = organizationId ? `?organization_id=${encodeURIComponent(organizationId)}` : '';
  return request<RepositoryListResponse>(`/repos${query}`);
}

export interface HealthModule {
  module_id: string;
  module_name: string;
  overall_score: number;
  dimensions: {
    doc_score: number;
    ownership_score: number;
    bus_factor: number;
    sole_owner_risk: boolean;
    unowned: boolean;
    staleness_note?: string;
  };
  gaps: string[];
  computed_at: string;
}

export interface RepoHealthResponse {
  repository_id: string;
  organization_id: string;
  total_modules: number;
  max_modules_per_request: number;
  modules: HealthModule[];
}

export interface SearchResultItem {
  chunk_id: string;
  result_type: 'code' | 'doc';
  similarity_score: number;
  rrf_score: number;
  rerank_score?: number | null;
  text: string;
  metadata: Record<string, string | number | boolean | string[]>;
  graph_context?: {
    repository?: { id: string; name: string; url?: string } | null;
    module?: { id: string; name: string; path?: string } | null;
    owners?: Array<{ name: string; email: string; commit_count: number }>;
    related_documents?: Array<{ doc_id: string; filename: string; relative_path: string }>;
    related_code_files?: Array<{ file_id: string; filename: string; relative_path: string }>;
  } | null;
}

export interface SearchResponse {
  query: string;
  organization_id: string;
  total_results: number;
  limit: number;
  offset: number;
  telemetry: { total_ms: number; [key: string]: number };
  results: SearchResultItem[];
}

export interface OnboardingResult extends JsonRecord {
  module_id: string;
  module_name: string;
  module_path: string;
  organization_id: string;
  generated_at: string;
  generation_quality: string;
  sections: Record<string, unknown>;
  markdown?: string;
}

export interface KTQuestionsResult extends JsonRecord {
  module_id: string;
  module_name: string;
  generated_at: string;
  generation_quality: string;
  generation_method: string;
  signal_count: number;
  signals_used: string[];
  questions: string;
}

export interface BusinessMappingItem {
  target_type: string;
  file_path: string;
  function_name: string;
  module_name: string;
  code_excerpt: string;
  confidence: string;
  rerank_score: number;
  evidence_type: string;
  reasoning: string;
  callers: string[];
  callers_scope: string;
  low_confidence: boolean;
}

export interface BusinessMappingChunk {
  business_chunk?: { chunk_id: string; heading_path: string; text_excerpt: string };
  mappings?: BusinessMappingItem[];
  unmapped_reason?: string | null;
  generation_quality?: string;
  skipped?: boolean;
  reason?: string;
  chunk_id?: string;
  heading_path?: string;
}

export interface BusinessMappingResult extends JsonRecord {
  document_id: string;
  organization_id: string;
  repository_id: string;
  chunks_processed: number;
  chunks_mapped: number;
  chunks_unmapped: number;
  overall_generation_quality: string;
  chunk_mappings: BusinessMappingChunk[];
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers: init?.body instanceof FormData ? init.headers : { 'Content-Type': 'application/json', ...init?.headers },
    });
  } catch {
    throw new ApiError(`Cannot reach the AutoKT API at ${API_BASE_URL}. Start the backend and try again.`);
  }
  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    try {
      const body = await response.json() as { detail?: string };
      if (body.detail) message = body.detail;
    } catch { /* preserve status message */ }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}

export async function checkBackend(): Promise<boolean> {
  try {
    const response = await request<{ status: string }>('/health');
    return response.status === 'ok';
  } catch {
    return false;
  }
}

export function ingestRepository(repoUrl: string, organizationId: string, branch: string) {
  return request<TaskStatus>('/repos', { method: 'POST', body: JSON.stringify({ repo_url: repoUrl, organization_id: organizationId, branch }) });
}

export function uploadTechnicalDocs(files: File[], organizationId: string) {
  const form = new FormData();
  files.forEach((file) => {
    form.append('files', file);
    form.append('relative_paths', file.webkitRelativePath || file.name);
  });
  form.append('organization_id', organizationId);
  form.append('source_type', 'doc');
  return request<TaskStatus>('/docs/upload', { method: 'POST', body: form });
}

export function uploadBusinessDocs(files: File[], organizationId: string, repositoryId: string, moduleId = '') {
  const form = new FormData();
  files.forEach((file) => form.append('files', file));
  form.append('organization_id', organizationId);
  form.append('repository_id', repositoryId);
  form.append('doc_type', 'business');
  form.append('module_id', moduleId);
  return request<TaskStatus>('/business-docs/upload', { method: 'POST', body: form });
}

export async function pollTask<T>(path: string, taskId: string, onUpdate?: (task: TaskStatus<T>) => void, signal?: AbortSignal): Promise<TaskStatus<T>> {
  const terminal = new Set(['completed', 'completed_without_embeddings', 'failed']);
  for (let attempt = 0; attempt < 240; attempt += 1) {
    if (signal?.aborted) throw new DOMException('Polling cancelled', 'AbortError');
    const task = await request<TaskStatus<T>>(`${path}/${encodeURIComponent(taskId)}`, { signal });
    onUpdate?.(task);
    if (terminal.has(task.status)) {
      if (task.status === 'failed') {
        const resultError = task.result && typeof task.result === 'object' && 'error' in task.result ? String(task.result.error) : '';
        throw new ApiError(resultError || 'The background task failed.');
      }
      return task;
    }
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(resolve, 1000);
      signal?.addEventListener('abort', () => { window.clearTimeout(timer); reject(new DOMException('Polling cancelled', 'AbortError')); }, { once: true });
    });
  }
  throw new ApiError('The task is still running. Check the backend task status and try again.');
}

export function getRepoHealth(repositoryId: string, organizationId: string) {
  return request<RepoHealthResponse>(`/health-score/repo/${encodeURIComponent(repositoryId)}?organization_id=${encodeURIComponent(organizationId)}`);
}

export function getModuleHealth(moduleId: string, organizationId: string) {
  const encodedModule = moduleId.split('/').map(encodeURIComponent).join('/');
  return request<HealthModule>(`/health-score/module/${encodedModule}?organization_id=${encodeURIComponent(organizationId)}`);
}

export function searchKnowledge(query: string, organizationId: string, sourceType = 'all') {
  const params = new URLSearchParams({ q: query, organization_id: organizationId, source_type: sourceType, limit: '12' });
  return request<SearchResponse>(`/search?${params.toString()}`);
}

export function startOnboardingPack(moduleId: string, organizationId: string) {
  return request<TaskStatus>('/onboarding-pack', { method: 'POST', body: JSON.stringify({ module_id: moduleId, organization_id: organizationId, include_markdown: true }) });
}

export function startKTQuestions(moduleId: string, organizationId: string) {
  return request<TaskStatus>('/kt-prep-questions', { method: 'POST', body: JSON.stringify({ module_id: moduleId, organization_id: organizationId }) });
}

export function startBusinessDocumentMapping(organizationId: string, repositoryId: string, documentId: string) {
  return request<TaskStatus>('/business-mapping/document', { method: 'POST', body: JSON.stringify({ organization_id: organizationId, repository_id: repositoryId, document_id: documentId, use_llm_judge: true, max_mappings_per_chunk: 3 }) });
}
