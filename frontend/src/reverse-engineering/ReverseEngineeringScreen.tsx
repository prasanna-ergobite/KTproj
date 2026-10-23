import { FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle, CheckCircle2, Code2, Database, Download, Eye, FileArchive, FileCode2, FileText, FolderSearch,
  GitBranch, Github, History, Loader2, Network, Play, RefreshCw, ShieldCheck, X,
} from 'lucide-react';
import {
  ApiError, ReverseEngineeringDocumentPreview, ReverseEngineeringRecentRun, ReverseEngineeringResult, TaskStatus,
  getReverseEngineeringBundleDownloadUrl, getReverseEngineeringDocument, getReverseEngineeringDocumentDownloadUrl,
  getReverseEngineeringTask, listReverseEngineeringRuns, pollReverseEngineeringTask, startReverseEngineering,
} from '../api';
import './reverse-engineering.css';
import { MarkdownPreview } from './MarkdownPreview';

const STORAGE_KEY = 'autokt-reverse-engineering-run';
const outputs = [
  ['system_overview', 'System Overview & Architecture', Network],
  ['business_logic', 'Functional & Business Logic', Code2],
  ['database_api_reference', 'Database & API Reference', FileCode2],
  ['developer_operations', 'Developer & Operations Guide', FileText],
  ['kt_handover', 'KT Plan & Handover Guide', FileArchive],
] as const;

interface StoredRun {
  taskId: string;
  organizationId: string;
  task?: TaskStatus<ReverseEngineeringResult>;
}

function readStoredRun(): StoredRun | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) as StoredRun : null;
  } catch {
    return null;
  }
}

function stageLabel(task?: TaskStatus<ReverseEngineeringResult> | null) {
  if (!task) return 'Ready';
  if (task.status === 'pending') return 'Queued';
  if (task.status === 'failed') return 'Failed';
  if (task.status === 'completed') {
    if (task.result?.documents?.some((item) => item.generation_status === 'failed')) return 'Documents generated with failures';
    if (task.result?.verification?.status === 'verification_incomplete') return 'Verification incomplete';
    if (task.result?.verification?.status === 'needs_review') return 'Verification needs review';
    return task.result?.verification ? 'Documentation verified' : task.result?.documents?.length ? 'Documents generated' : task.result?.analysis ? 'Structural analysis complete' : 'Inventory complete';
  }
  if (task.result?.stage === 'verification') return 'Verifying generated documents';
  if (task.result?.stage === 'document_generation') return 'Generating documents';
  if (task.result?.stage === 'shared_analysis') return 'Synthesizing repository analysis';
  if (task.result?.stage === 'structural_analysis') return 'Extracting source evidence';
  return task.result?.stage === 'inventory' ? 'Reading source tree' : 'Cloning repository';
}

function formatBytes(value = 0) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDuration(milliseconds = 0) {
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1000).toFixed(1)} s`;
  return `${Math.floor(milliseconds / 60_000)}m ${Math.round((milliseconds % 60_000) / 1000)}s`;
}

export function ReverseEngineeringScreen({
  defaultOrganizationId,
  setToast,
}: {
  defaultOrganizationId: string;
  setToast: (message: string) => void;
}) {
  const stored = useMemo(readStoredRun, []);
  const [repoUrl, setRepoUrl] = useState('');
  const [organizationId, setOrganizationId] = useState(stored?.organizationId || defaultOrganizationId || 'demo');
  const [ref, setRef] = useState('');
  const [task, setTask] = useState<TaskStatus<ReverseEngineeringResult> | null>(stored?.task || null);
  const [running, setRunning] = useState(Boolean(stored && stored.task?.status !== 'completed' && stored.task?.status !== 'failed'));
  const [error, setError] = useState('');
  const [preview, setPreview] = useState<ReverseEngineeringDocumentPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState('');
  const [recentRuns, setRecentRuns] = useState<ReverseEngineeringRecentRun[]>([]);
  const [recentLoading, setRecentLoading] = useState(false);
  const controller = useRef<AbortController | null>(null);

  const remember = (taskId: string, orgId: string, nextTask: TaskStatus<ReverseEngineeringResult>) => {
    setTask(nextTask);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ taskId, organizationId: orgId, task: nextTask }));
  };

  const refreshRecentRuns = async (orgId = organizationId.trim()) => {
    if (!orgId) return;
    setRecentLoading(true);
    try {
      setRecentRuns((await listReverseEngineeringRuns(orgId)).runs);
    } catch {
      setRecentRuns([]);
    } finally {
      setRecentLoading(false);
    }
  };

  const watch = async (taskId: string, orgId: string) => {
    controller.current?.abort();
    controller.current = new AbortController();
    setRunning(true);
    try {
      const completed = await pollReverseEngineeringTask(taskId, orgId, (next) => remember(taskId, orgId, next), controller.current.signal);
      remember(taskId, orgId, completed);
      setToast('Repository documentation generated');
      await refreshRecentRuns(orgId);
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === 'AbortError') return;
      setError(caught instanceof ApiError ? caught.message : 'Repository analysis failed.');
    } finally {
      setRunning(false);
    }
  };

  useEffect(() => {
    const initialOrganizationId = stored?.organizationId || defaultOrganizationId || 'demo';
    refreshRecentRuns(initialOrganizationId);
    if (stored?.taskId && stored.task?.status !== 'completed' && stored.task?.status !== 'failed') {
      watch(stored.taskId, stored.organizationId);
    }
    return () => controller.current?.abort();
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    setPreview(null);
    try {
      const created = await startReverseEngineering(repoUrl.trim(), organizationId.trim(), ref.trim() || undefined);
      remember(created.task_id, organizationId.trim(), created);
      await watch(created.task_id, organizationId.trim());
    } catch (caught) {
      setRunning(false);
      setError(caught instanceof ApiError ? caught.message : 'Could not start repository analysis.');
    }
  };

  const openRecentRun = async (run: ReverseEngineeringRecentRun) => {
    const orgId = organizationId.trim();
    setError('');
    setPreview(null);
    try {
      const selected = await getReverseEngineeringTask(run.task_id, orgId);
      remember(run.task_id, orgId, selected);
      setRepoUrl(run.repo_url || '');
      setRef(run.requested_ref || '');
      if (selected.status !== 'completed' && selected.status !== 'failed') await watch(run.task_id, orgId);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not open the selected analysis run.');
    }
  };

  const result = task?.result;
  const inventory = result?.inventory;
  const analysis = result?.analysis;
  const documents = result?.documents || [];
  const documentsComplete = documents.length === outputs.length && documents.every((item) => item.generation_status === 'completed');
  const verification = result?.verification;
  const acceptance = result?.acceptance;
  const verificationComplete = Boolean(verification);
  const progress = task?.status === 'completed' ? 100 : result?.stage === 'verification' ? 97 : result?.stage === 'document_generation' ? 90 : result?.stage === 'shared_analysis' ? 82 : result?.stage === 'structural_analysis' ? 68 : result?.stage === 'inventory' ? 45 : task ? 18 : 0;
  const cloneDone = Boolean(result?.commit_sha);
  const taskOrganizationId = typeof task?.payload?.organization_id === 'string' ? task.payload.organization_id : organizationId;

  const openPreview = async (documentId: string) => {
    if (!task?.task_id) return;
    setPreviewLoading(documentId);
    setError('');
    try {
      setPreview(await getReverseEngineeringDocument(task.task_id, documentId, taskOrganizationId));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not load the document preview.');
    } finally {
      setPreviewLoading('');
    }
  };

  return (
    <div className="content-wrap re-page">
      <header className="page-header re-heading">
        <div>
          <div className="eyebrow">Source-only reverse engineering</div>
          <h1>Turn a repository into trusted documentation.</h1>
          <p>Start with a pinned source snapshot and a transparent inventory. Every generated claim will later link back to this evidence.</p>
        </div>
        <div className="re-safety"><ShieldCheck size={17} /><span><strong>No code execution</strong><small>Read-only source analysis</small></span></div>
      </header>

      <section className="re-intake-grid">
        <form className="surface re-form" onSubmit={submit}>
          <div className="re-section-title"><span>01</span><div><h2>Repository intake</h2><p>Public GitHub, GitLab, or Bitbucket repository</p></div></div>
          <label><span>Repository URL</span><div className="input-shell"><Github size={16} /><input type="url" required placeholder="https://github.com/organization/project.git" value={repoUrl} onChange={(event) => setRepoUrl(event.target.value)} /></div></label>
          <div className="two-fields">
            <label><span>Organization ID</span><div className="input-shell"><FolderSearch size={15} /><input required value={organizationId} onChange={(event) => setOrganizationId(event.target.value)} /></div></label>
            <label><span>Branch, tag, or commit (optional)</span><div className="input-shell"><GitBranch size={15} /><input placeholder="Default branch" value={ref} onChange={(event) => setRef(event.target.value)} /></div></label>
          </div>
          {error && <div className="api-error"><AlertCircle size={17} /><div><strong>Analysis could not continue</strong><span>{error}</span></div></div>}
          <button className="primary-button full" disabled={running} type="submit">
            {running ? <Loader2 className="spin" size={16} /> : <Play size={15} />}{running ? 'Analyzing repository…' : 'Analyze repository'}
          </button>
        </form>

        <aside className="re-status-card">
          <div className="re-status-top"><span>Current run</span><em className={task?.status || 'idle'}>{stageLabel(task)}</em></div>
          <h2>{result?.repository_name || 'Waiting for a repository'}</h2>
          <p>{result?.repo_url || 'The pinned commit and inventory summary will appear here.'}</p>
          <div className="re-progress"><span style={{ width: `${progress}%` }} /></div>
          <div className="re-stage-list">
            <div className={cloneDone ? 'done' : running ? 'active' : ''}><span>{cloneDone ? <CheckCircle2 size={15} /> : '1'}</span><div><strong>Validate and clone</strong><small>Remote URL, requested ref, immutable commit</small></div></div>
            <div className={inventory ? 'done' : running ? 'active' : ''}><span>{inventory ? <CheckCircle2 size={15} /> : '2'}</span><div><strong>Build source inventory</strong><small>Files, languages, manifests, exclusions</small></div></div>
            <div className={analysis ? 'done' : result?.stage === 'structural_analysis' ? 'active' : ''}><span>{analysis ? <CheckCircle2 size={15} /> : '3'}</span><div><strong>Extract structure and evidence</strong><small>Symbols, routes, models and relationships</small></div></div>
            <div className={documentsComplete ? 'done' : result?.stage === 'document_generation' || result?.stage === 'shared_analysis' ? 'active' : ''}><span>{documentsComplete ? <CheckCircle2 size={15} /> : '4'}</span><div><strong>Generate documentation</strong><small>Five evidence-grounded Markdown drafts</small></div></div>
            <div className={verificationComplete ? 'done' : result?.stage === 'verification' ? 'active' : ''}><span>{verificationComplete ? <CheckCircle2 size={15} /> : '5'}</span><div><strong>Verify and correct</strong><small>Claims, source hashes, completeness and consistency</small></div></div>
          </div>
          {result?.commit_sha && <div className="re-commit"><GitBranch size={14} /><span><small>{result.resolved_ref}</small><code>{result.commit_sha.slice(0, 12)}</code></span></div>}
        </aside>
      </section>

      <section className="surface re-recent-runs">
        <div className="re-recent-heading"><div><span>Saved analysis tasks</span><h2>Recent runs</h2></div><button type="button" onClick={() => refreshRecentRuns()} disabled={recentLoading}>{recentLoading ? <Loader2 className="spin" size={13} /> : <RefreshCw size={13} />}Refresh</button></div>
        <div className="re-recent-list">
          {recentRuns.slice(0, 6).map((run) => (
            <button type="button" key={run.task_id} className={task?.task_id === run.task_id ? 'selected' : ''} onClick={() => openRecentRun(run)}>
              <History size={15} />
              <span><strong>{run.repository_name || run.repo_url || 'Repository analysis'}</strong><small>{run.commit_sha ? run.commit_sha.slice(0, 10) : run.requested_ref || 'Commit pending'} · {run.documents_completed}/{run.documents_total || 5} documents</small></span>
              <em className={run.acceptance_status || run.verification_status || run.status}>{run.acceptance_status || run.verification_status || run.status}</em>
              <time>{run.updated_at ? new Date(run.updated_at).toLocaleString() : ''}</time>
            </button>
          ))}
          {!recentLoading && recentRuns.length === 0 && <p>No reverse-engineering runs found for this organization.</p>}
        </div>
      </section>

      {inventory && (
        <>
          <section className="re-metrics">
            <article><span>Included source</span><strong>{inventory.included_files.toLocaleString()}</strong><small>{formatBytes(inventory.included_bytes)} read safely</small></article>
            <article><span>Excluded items</span><strong>{inventory.excluded_files.toLocaleString()}</strong><small>Secrets, binaries, vendor and generated paths</small></article>
            <article><span>Languages</span><strong>{Object.keys(inventory.languages).length}</strong><small>{Object.keys(inventory.languages).slice(0, 3).join(' · ') || 'No source language detected'}</small></article>
            <article><span>Framework hints</span><strong>{inventory.frameworks.length}</strong><small>{inventory.frameworks.join(' · ') || 'None found in manifests'}</small></article>
          </section>

          <section className="re-results-grid">
            <div className="surface re-files">
              <div className="re-panel-title"><div><span>Evidence preview</span><h2>Included files</h2></div><em>{result?.artifact_refs?.inventory}</em></div>
              {(result?.files_preview || []).slice(0, 12).map((file) => (
                <div className="re-file-row" key={file.path}><FileCode2 size={15} /><span><strong>{file.path}</strong><small>{file.category}{file.language ? ` · ${file.language}` : ''}</small></span><em>{formatBytes(file.size_bytes)}</em></div>
              ))}
            </div>
            <div className="surface re-output-list">
              <div className="re-panel-title"><div><span>Output set</span><h2>Documentation drafts</h2></div></div>
              {outputs.map(([id, name, Icon], index) => {
                const document = documents.find((item) => item.id === id);
                return <div key={id} className="re-output-row"><span><Icon size={15} /></span><div><strong>{index + 1}. {name}</strong><small>{document ? `${document.word_count.toLocaleString()} words · verification ${document.verification_status}` : analysis ? 'Awaiting document generation' : 'Awaiting structural analysis'}</small></div>{document?.generation_status === 'completed' ? <div className="re-document-actions"><button type="button" onClick={() => openPreview(id)} disabled={previewLoading === id}>{previewLoading === id ? <Loader2 className="spin" size={12} /> : <Eye size={12} />}Preview</button><a href={getReverseEngineeringDocumentDownloadUrl(task!.task_id, id, taskOrganizationId)}><Download size={12} />MD</a></div> : <em>{document?.generation_status === 'failed' ? 'Failed' : 'Pending'}</em>}</div>;
              })}
              {result?.bundle?.artifact_ref && task?.task_id ? <a className="re-bundle-download" href={getReverseEngineeringBundleDownloadUrl(task.task_id, taskOrganizationId)}><Download size={14} /><span><strong>Download complete package</strong><small>{formatBytes(result.bundle.size_bytes)} · documents, evidence and verification report</small></span><em>ZIP</em></a> : null}
              {result?.generation?.warnings?.length ? <div className="re-generation-warning"><AlertCircle size={14} /><div><strong>Generation note</strong><small>{result.generation.warnings[0]}</small></div></div> : null}
            </div>
          </section>
        </>
      )}

      {verification && (
        <section className={`surface re-verification ${verification.status}`}>
          <div className="re-verification-heading">
            <div className="re-verification-icon"><ShieldCheck size={20} /></div>
            <div><span>Independent deterministic verification</span><h2>{verification.status === 'checks_passed' ? 'Generated documentation passed the implemented checks' : verification.status === 'verification_incomplete' ? 'Verification did not complete' : 'Generated documentation needs review'}</h2><p>{verification.scope}</p></div>
            <em>{verification.open_findings} open findings</em>
          </div>
          <div className="re-verification-metrics">
            <article><strong>{verification.claim_verdicts.supported || 0}</strong><span>Supported claims</span><small>{result?.claims_count || 0} checked against structured facts and source hashes</small></article>
            <article><strong>{Object.values(verification.contract_checks || {}).filter((metric) => metric.status === 'complete').length}/{Object.keys(verification.contract_checks || {}).length}</strong><span>Exact document contracts</span><small>Endpoints, models, errors, workflows, behavior rows and diagram edges</small></article>
            {Object.entries(verification.coverage).slice(0, 6).map(([name, metric]) => (
              <article key={name}><strong>{metric.detected ? `${metric.documented}/${metric.detected}` : 'N/A'}</strong><span>{name.replace(/_/g, ' ')}</span><small>{metric.status}</small></article>
            ))}
            <article><strong>{verification.correction_rounds}</strong><span>Correction rounds</span><small>{verification.corrected_documents.length ? verification.corrected_documents.join(', ') : 'No regeneration required'}</small></article>
          </div>
          {(result?.findings_preview || []).length > 0 && (
            <div className="re-findings">
              {(result?.findings_preview || []).slice(0, 5).map((finding) => <div key={finding.id}><em className={finding.severity}>{finding.severity}</em><span><strong>{finding.code.replace(/_/g, ' ')}</strong><small>{finding.message}</small></span></div>)}
            </div>
          )}
          <div className="re-verification-limit"><AlertCircle size={14} /><span>{verification.limitations[0]}</span></div>
        </section>
      )}

      {acceptance && (
        <section className={`surface re-acceptance ${acceptance.status}`}>
          <div className="re-acceptance-heading">
            <div className="re-acceptance-icon"><CheckCircle2 size={20} /></div>
            <div>
              <span>Demo rehearsal report</span>
              <h2>{acceptance.status === 'ready' ? 'This run meets the implemented acceptance checks' : acceptance.status === 'needs_review' ? 'This run needs scope review before the demo' : 'This run is not ready for the demo'}</h2>
              <p>Measured from this run; the score covers the checks listed below.</p>
            </div>
            <em>{acceptance.summary.score_percent}% · {acceptance.status.replace(/_/g, ' ')}</em>
          </div>
          <div className="re-acceptance-summary">
            <article><strong>{acceptance.summary.passed}/{acceptance.summary.total_checks}</strong><span>Checks passed</span></article>
            <article><strong>{acceptance.summary.review}</strong><span>Need review</span></article>
            <article><strong>{acceptance.summary.failed}</strong><span>Failed</span></article>
            <article><strong>{formatDuration(Object.values(acceptance.measured_stage_durations_ms).reduce((total, value) => total + value, 0))}</strong><span>Measured analysis time</span></article>
          </div>
          <div className="re-acceptance-checks">
            {acceptance.checks.map((check) => (
              <div key={check.id}>
                <em className={check.status}>{check.status}</em>
                <span><strong>{check.title}</strong><small>{check.message}</small></span>
              </div>
            ))}
          </div>
          {acceptance.recommendations.length > 0 && <div className="re-acceptance-action"><AlertCircle size={14} /><span>{acceptance.recommendations[0]}</span></div>}
        </section>
      )}

      {analysis && (
        <section className="re-analysis-section">
          <div className="re-analysis-heading">
            <div><span>Deterministic source analysis</span><h2>Repository structure</h2></div>
            <em className={analysis.coverage.status}>{analysis.coverage.coverage_percent}% structural coverage · {analysis.coverage.status}</em>
          </div>
          <div className="re-analysis-metrics">
            <article><strong>{analysis.summary.components}</strong><span>Components</span></article>
            <article><strong>{analysis.summary.symbols}</strong><span>Qualified symbols</span></article>
            <article><strong>{analysis.summary.routes}</strong><span>Route candidates</span></article>
            <article><strong>{analysis.summary.models}</strong><span>Data models</span></article>
            <article><strong>{analysis.summary.dependencies}</strong><span>Dependencies</span></article>
            <article><strong>{analysis.summary.config_keys}</strong><span>Configuration keys</span></article>
          </div>
          <div className="re-analysis-grid">
            <div className="surface re-components">
              <div className="re-panel-title"><div><span>Architecture style</span><h2>{analysis.architecture.style}</h2></div><em>{result?.artifact_refs?.repository_analysis}</em></div>
              {analysis.architecture.components.slice(0, 12).map((component) => (
                <div className="re-component-row" key={component.id}>
                  <span><Network size={15} /></span>
                  <div><strong>{component.name}</strong><small>{Object.keys(component.languages).join(' · ') || 'Mixed source'} · {component.file_count} files</small></div>
                  <div><em>{component.symbol_count}</em><small>symbols</small></div>
                  <div><em>{component.route_count}</em><small>routes</small></div>
                  <div><em>{component.model_count}</em><small>models</small></div>
                </div>
              ))}
            </div>
            <div className="surface re-facts">
              <div className="re-panel-title"><div><span>Evidence-linked facts</span><h2>Routes and models</h2></div><em>{result?.artifact_refs?.extracted_facts}</em></div>
              {(result?.routes_preview || []).slice(0, 6).map((route) => (
                <div className="re-fact-row" key={route.id}><code>{route.method}</code><div><strong>{route.path || 'Dynamic path'}</strong><small>{route.file}:{route.line}</small></div></div>
              ))}
              {(result?.models_preview || []).slice(0, 5).map((model) => (
                <div className="re-fact-row model" key={model.id}><Database size={14} /><div><strong>{model.name}</strong><small>{model.model_kind} · {model.fields.length} fields · {model.file}:{model.line}</small></div></div>
              ))}
              {!result?.routes_preview?.length && !result?.models_preview?.length && <p className="re-empty-facts">No route or model facts were found in the supported source files.</p>}
            </div>
          </div>
          {analysis.uncertainties.length > 0 && <div className="re-limitations"><AlertCircle size={16} /><div><strong>Known analysis gaps</strong><span>{analysis.uncertainties.slice(0, 3).join(' · ')}</span></div></div>}
        </section>
      )}

      {preview && (
        <section className="re-preview surface">
          <header><div><span>Document preview · version {preview.document.version}</span><h2>{preview.document.title}</h2><small>{preview.document.generation_method} generation · verification {preview.document.verification_status}</small></div><button type="button" onClick={() => setPreview(null)} aria-label="Close preview"><X size={17} /></button></header>
          <MarkdownPreview markdown={preview.markdown} />
        </section>
      )}
    </div>
  );
}

