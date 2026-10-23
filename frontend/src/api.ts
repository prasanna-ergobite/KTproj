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

export interface ReverseEngineeringFile {
  path: string;
  size_bytes: number;
  extension: string;
  category: string;
  language?: string | null;
  sha256: string;
}

export interface ReverseEngineeringInventory {
  repository_root: string;
  total_files: number;
  included_files: number;
  excluded_files: number;
  total_bytes: number;
  included_bytes: number;
  languages: Record<string, number>;
  categories: Record<string, number>;
  manifests: string[];
  frameworks: string[];
  limitations: string[];
}

export interface ReverseEngineeringCoverage {
  status: 'complete' | 'partial';
  eligible_files: number;
  parsed_files: number;
  unsupported_files: number;
  failed_files: number;
  coverage_percent: number;
  unsupported: Array<{ path: string; reason: string }>;
  failures: Array<{ path: string; reason: string }>;
  inventory_limitations: string[];
  methods: Record<string, string>;
}

export interface ReverseEngineeringComponent {
  id: string;
  name: string;
  file_count: number;
  languages: Record<string, number>;
  symbol_count: number;
  route_count: number;
  model_count: number;
}

export interface ReverseEngineeringAnalysis {
  summary: {
    components: number;
    symbols: number;
    routes: number;
    models: number;
    dependencies: number;
    config_keys: number;
    behavior_records?: number;
  };
  coverage: ReverseEngineeringCoverage;
  architecture: {
    style: string;
    components: ReverseEngineeringComponent[];
    interactions: Array<{ source: string; target: string; type: string; evidence_count: number }>;
    entry_points: Array<Record<string, unknown>>;
  };
  uncertainties: string[];
}

export interface ReverseEngineeringDocument {
  id: string;
  title: string;
  generation_status: 'completed' | 'failed';
  verification_status: 'pending' | 'checks_passed' | 'needs_review' | 'verification_incomplete';
  version: number;
  generation_method: 'deterministic' | 'hybrid';
  artifact_ref?: string | null;
  word_count: number;
  error?: string | null;
}

export interface ReverseEngineeringDocumentPreview {
  task_id: string;
  document: ReverseEngineeringDocument;
  markdown: string;
}

export interface ReverseEngineeringCoverageMetric {
  detected: number;
  documented: number;
  ratio: number | null;
  status: 'complete' | 'incomplete' | 'not_applicable';
}

export interface ReverseEngineeringContractMetric {
  expected: number;
  matched: number;
  status: 'complete' | 'mismatch';
  unexpected?: number;
}

export interface ReverseEngineeringFinding {
  id: string;
  code: string;
  severity: 'critical' | 'high' | 'medium' | 'low';
  document_id?: string | null;
  message: string;
  evidence_ids: string[];
  fixable: boolean;
  status: 'open' | 'resolved';
}

export interface ReverseEngineeringVerification {
  status: 'checks_passed' | 'needs_review' | 'verification_incomplete';
  scope: string;
  completed_at: string;
  correction_rounds: number;
  correction_history: Array<{
    round: number;
    documents: string[];
    triggering_findings: Array<{ code: string; severity: string; document_id?: string | null; message: string }>;
  }>;
  corrected_documents: string[];
  claim_verdicts: Record<string, number>;
  coverage: Record<string, ReverseEngineeringCoverageMetric>;
  contract_checks: Record<string, ReverseEngineeringContractMetric>;
  findings_by_severity: Record<string, number>;
  open_findings: number;
  limitations: string[];
}

export interface ReverseEngineeringVerificationResponse {
  task_id: string;
  report: ReverseEngineeringVerification & { documents: ReverseEngineeringDocument[] };
  findings: ReverseEngineeringFinding[];
}

export interface ReverseEngineeringAcceptanceCheck {
  id: string;
  title: string;
  status: 'pass' | 'review' | 'fail';
  required: boolean;
  message: string;
  evidence: Record<string, unknown>;
}

export interface ReverseEngineeringAcceptance {
  status: 'ready' | 'needs_review' | 'not_ready';
  generated_at: string;
  commit_sha: string;
  summary: {
    total_checks: number;
    passed: number;
    review: number;
    failed: number;
    score_percent: number;
  };
  checks: ReverseEngineeringAcceptanceCheck[];
  measured_stage_durations_ms: Record<string, number>;
  recommendations: string[];
  limitations: string[];
}

export interface ReverseEngineeringAcceptanceResponse {
  task_id: string;
  report: ReverseEngineeringAcceptance;
}

export interface ReverseEngineeringBundle {
  artifact_ref?: string | null;
  filename?: string;
  size_bytes?: number;
  sha256?: string;
  included_files?: string[];
  error?: string;
}

export interface ReverseEngineeringRecentRun {
  task_id: string;
  status: string;
  repo_url?: string | null;
  repository_name?: string | null;
  requested_ref?: string | null;
  commit_sha?: string | null;
  verification_status?: string | null;
  acceptance_status?: string | null;
  documents_completed: number;
  documents_total: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ReverseEngineeringRecentRunsResponse {
  organization_id: string;
  runs: ReverseEngineeringRecentRun[];
}

export interface ReverseEngineeringRoute {
  id: string;
  framework: string;
  confidence: string;
  method: string;
  path?: string | null;
  handler?: string | null;
  file: string;
  line: number;
  evidence_id: string;
}

export interface ReverseEngineeringModel {
  id: string;
  name: string;
  qualified_name: string;
  model_kind: string;
  table_name?: string | null;
  fields: Array<{ name: string; type?: string | null; required?: boolean | null; default?: string | null }>;
  file: string;
  line: number;
  evidence_id: string;
}

export interface ReverseEngineeringResult extends JsonRecord {
  run_id: string;
  organization_id: string;
  repo_url: string;
  repository_name?: string;
  requested_ref?: string | null;
  resolved_ref?: string;
  commit_sha?: string;
  stage: string;
  inventory?: ReverseEngineeringInventory;
  analysis?: ReverseEngineeringAnalysis;
  files_preview?: ReverseEngineeringFile[];
  exclusions_preview?: Array<{ path: string; reason: string; size_bytes?: number }>;
  routes_preview?: ReverseEngineeringRoute[];
  models_preview?: ReverseEngineeringModel[];
  dependencies_preview?: Array<{ name: string; version?: string | null; scope: string; source: string }>;
  documents?: ReverseEngineeringDocument[];
  generation?: { method: string; provider: string; model?: string | null; warnings: string[] };
  claims_count?: number;
  verification?: ReverseEngineeringVerification;
  acceptance?: ReverseEngineeringAcceptance;
  findings_preview?: ReverseEngineeringFinding[];
  bundle?: ReverseEngineeringBundle;
  artifact_refs?: {
    inventory: string;
    extracted_facts?: string;
    structural_analysis?: string;
    repository_analysis?: string;
    evidence?: string;
    claims?: string;
    findings?: string;
    verification_report?: string;
    acceptance_report?: string;
    bundle?: string;
    manifest?: string;
  };
  completed_at?: string;
  error?: string;
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

export function startReverseEngineering(repoUrl: string, organizationId: string, ref?: string) {
  return request<TaskStatus<ReverseEngineeringResult>>('/reverse-engineering', {
    method: 'POST',
    body: JSON.stringify({ repo_url: repoUrl, organization_id: organizationId, ref: ref || null }),
  });
}

export function getReverseEngineeringTask(taskId: string, organizationId: string, signal?: AbortSignal) {
  const params = new URLSearchParams({ organization_id: organizationId });
  return request<TaskStatus<ReverseEngineeringResult>>(
    `/reverse-engineering/tasks/${encodeURIComponent(taskId)}?${params.toString()}`,
    { signal },
  );
}

export function getReverseEngineeringDocument(taskId: string, documentId: string, organizationId: string) {
  const params = new URLSearchParams({ organization_id: organizationId });
  return request<ReverseEngineeringDocumentPreview>(
    `/reverse-engineering/tasks/${encodeURIComponent(taskId)}/documents/${encodeURIComponent(documentId)}?${params.toString()}`,
  );
}

export function getReverseEngineeringDocumentDownloadUrl(taskId: string, documentId: string, organizationId: string) {
  const params = new URLSearchParams({ organization_id: organizationId });
  return `${API_BASE_URL}/reverse-engineering/tasks/${encodeURIComponent(taskId)}/documents/${encodeURIComponent(documentId)}/download?${params.toString()}`;
}

export function getReverseEngineeringBundleDownloadUrl(taskId: string, organizationId: string) {
  const params = new URLSearchParams({ organization_id: organizationId });
  return `${API_BASE_URL}/reverse-engineering/tasks/${encodeURIComponent(taskId)}/download?${params.toString()}`;
}

export function listReverseEngineeringRuns(organizationId: string, limit = 10) {
  const params = new URLSearchParams({ organization_id: organizationId, limit: String(limit) });
  return request<ReverseEngineeringRecentRunsResponse>(`/reverse-engineering/tasks?${params.toString()}`);
}

export function getReverseEngineeringVerification(taskId: string, organizationId: string) {
  const params = new URLSearchParams({ organization_id: organizationId });
  return request<ReverseEngineeringVerificationResponse>(
    `/reverse-engineering/tasks/${encodeURIComponent(taskId)}/verification?${params.toString()}`,
  );
}

export function getReverseEngineeringAcceptance(taskId: string, organizationId: string) {
  const params = new URLSearchParams({ organization_id: organizationId });
  return request<ReverseEngineeringAcceptanceResponse>(
    `/reverse-engineering/tasks/${encodeURIComponent(taskId)}/acceptance?${params.toString()}`,
  );
}

export async function pollReverseEngineeringTask(
  taskId: string,
  organizationId: string,
  onUpdate?: (task: TaskStatus<ReverseEngineeringResult>) => void,
  signal?: AbortSignal,
): Promise<TaskStatus<ReverseEngineeringResult>> {
  const terminal = new Set(['completed', 'failed']);
  for (let attempt = 0; attempt < 600; attempt += 1) {
    if (signal?.aborted) throw new DOMException('Polling cancelled', 'AbortError');
    const task = await getReverseEngineeringTask(taskId, organizationId, signal);
    onUpdate?.(task);
    if (terminal.has(task.status)) {
      if (task.status === 'failed') throw new ApiError(task.result?.error || 'Repository analysis failed.');
      return task;
    }
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(resolve, 1000);
      signal?.addEventListener('abort', () => {
        window.clearTimeout(timer);
        reject(new DOMException('Polling cancelled', 'AbortError'));
      }, { once: true });
    });
  }
  throw new ApiError('Repository analysis is still running. Reopen this page to continue polling.');
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

// ---------------------------------------------------------------------------
// Answer Generation (RAG)
// ---------------------------------------------------------------------------

export interface CitedSource {
  chunk_id: string;
  result_type: 'code' | 'doc';
  file_path: string;
  snippet: string;
  rerank_score?: number | null;
  module_name?: string | null;
  owners: string[];
}

export interface AnswerResponse {
  query: string;
  organization_id: string;
  answer: string;
  sources: CitedSource[];
  context_items_used: number;
  generation_ms: number;
}

export function answerQuestion(
  query: string,
  organizationId: string,
  contextItems: SearchResultItem[],
  topK = 5,
): Promise<AnswerResponse> {
  return request<AnswerResponse>('/search/answer', {
    method: 'POST',
    body: JSON.stringify({
      query,
      organization_id: organizationId,
      context_items: contextItems,
      top_k: topK,
    }),
  });
}

// ---------------------------------------------------------------------------
// Knowledge Graph Explorer API
// ---------------------------------------------------------------------------

export interface GraphNode {
  id: string;
  label: 'Repository' | 'Module' | 'File' | 'Function' | 'Class' | 'Person' | 'Document' | 'Package' | string;
  name: string;
  path?: string;
  val: number;
  color: string;
  badge?: string;
  properties?: Record<string, any>;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  fx?: number;
  fy?: number;
}

export interface GraphLink {
  source: string | GraphNode;
  target: string | GraphNode;
  type: string;
  label: string;
  properties?: Record<string, any>;
}

export interface GraphTopologyResponse {
  nodes: GraphNode[];
  links: GraphLink[];
  node_count: number;
  link_count: number;
  repository_id?: string;
  module_id?: string;
}

export interface GraphSearchItem {
  id: string;
  label: string;
  name: string;
  path?: string;
  badge?: string;
  color: string;
}

export interface GraphSearchResponse {
  results: GraphSearchItem[];
  total: number;
}

export function fetchGraphTopology(params: {
  repositoryId?: string;
  organizationId?: string;
  moduleId?: string;
  includeFunctions?: boolean;
  limit?: number;
}): Promise<GraphTopologyResponse> {
  const searchParams = new URLSearchParams();
  if (params.repositoryId) searchParams.set('repository_id', params.repositoryId);
  if (params.organizationId) searchParams.set('organization_id', params.organizationId);
  if (params.moduleId) searchParams.set('module_id', params.moduleId);
  if (params.includeFunctions !== undefined) searchParams.set('include_functions', String(params.includeFunctions));
  if (params.limit) searchParams.set('limit', String(params.limit));

  const qs = searchParams.toString();
  return request<GraphTopologyResponse>(`/graph/topology${qs ? `?${qs}` : ''}`);
}

export function expandGraphNode(nodeId: string, organizationId?: string, limit = 35): Promise<GraphTopologyResponse> {
  const searchParams = new URLSearchParams({ node_id: nodeId });
  if (organizationId) searchParams.set('organization_id', organizationId);
  if (limit) searchParams.set('limit', String(limit));

  return request<GraphTopologyResponse>(`/graph/node/neighbors?${searchParams.toString()}`);
}

export function searchGraphNodes(query: string, organizationId?: string, limit = 15): Promise<GraphSearchResponse> {
  const searchParams = new URLSearchParams({ query });
  if (organizationId) searchParams.set('organization_id', organizationId);
  if (limit) searchParams.set('limit', String(limit));

  return request<GraphSearchResponse>(`/graph/search?${searchParams.toString()}`);
}

