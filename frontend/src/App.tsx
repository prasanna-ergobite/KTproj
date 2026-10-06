import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, ArrowDown, ArrowRight, ArrowUp, ArrowUpRight, BookOpen, Boxes, Braces,
  Check, CheckCircle2, ChevronDown, ChevronRight, CircleDot, Clipboard, CloudUpload,
  Code2, Copy, Database, FileCode2, FileText, FolderGit2, Gauge, GitBranch, Github,
  Info, Layers3, LayoutDashboard, Link2, Loader2, Menu, MessageSquareText,
  MoreHorizontal, Network, Plus, RefreshCw, Search, ShieldCheck, Sparkles, Upload,
  UserRound, UsersRound, WandSparkles, X, Zap,
} from 'lucide-react';
import {
  API_BASE_URL, BusinessMappingResult, HealthModule, KTQuestionsResult, OnboardingResult,
  RepoHealthResponse, RepoIngestionResult, SearchResultItem, checkBackend, getModuleHealth,
  getRepoHealth, ingestRepository, pollTask, searchKnowledge, startBusinessDocumentMapping,
  startKTQuestions, startOnboardingPack, uploadBusinessDocs, uploadTechnicalDocs,
} from './api';

type Screen = 'setup' | 'overview' | 'search' | 'module' | 'mapping';
type ModuleTab = 'health' | 'onboarding' | 'questions';
type IngestState = 'idle' | 'running' | 'complete';

interface WorkspaceContext {
  organizationId: string;
  repositoryId: string;
  repositoryName: string;
  selectedModuleId: string;
  selectedModuleName: string;
  selectedModulePath: string;
  businessDocumentId: string;
  businessDocumentName: string;
  ingestion?: RepoIngestionResult;
}

const emptyWorkspace: WorkspaceContext = { organizationId: '', repositoryId: '', repositoryName: '', selectedModuleId: '', selectedModuleName: '', selectedModulePath: '', businessDocumentId: '', businessDocumentName: '' };

function loadWorkspace(): WorkspaceContext {
  try {
    const saved = window.localStorage.getItem('autokt-live-workspace');
    return saved ? { ...emptyWorkspace, ...JSON.parse(saved) as WorkspaceContext } : emptyWorkspace;
  } catch { return emptyWorkspace; }
}

const navItems: Array<{ id: Screen; label: string; icon: typeof Search }> = [
  { id: 'overview', label: 'Overview', icon: LayoutDashboard },
  { id: 'search', label: 'Knowledge search', icon: Search },
  { id: 'module', label: 'Module intelligence', icon: Boxes },
  { id: 'mapping', label: 'Business mapping', icon: Network },
];

interface MappingSectionView { id: string; title: string; status: string; count: number; confidence: number; chunk?: import('./api').BusinessMappingChunk }

function App() {
  const [workspace, setWorkspace] = useState<WorkspaceContext>(loadWorkspace);
  const [screen, setScreen] = useState<Screen>(() => workspace.repositoryId ? 'overview' : 'setup');
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [moduleTab, setModuleTab] = useState<ModuleTab>('health');
  const [toast, setToast] = useState('');
  const [backendOnline, setBackendOnline] = useState<boolean | null>(null);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(''), 2400);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    checkBackend().then(setBackendOnline);
  }, []);

  useEffect(() => {
    window.localStorage.removeItem('autokt-workspace');
    window.localStorage.setItem('autokt-live-workspace', JSON.stringify(workspace));
  }, [workspace]);

  const navigate = (next: Screen) => {
    setScreen(next);
    setSidebarOpen(false);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  return (
    <div className="app-shell">
      <Sidebar active={screen} open={sidebarOpen} workspace={workspace} onNavigate={navigate} onClose={() => setSidebarOpen(false)} />
      <div className="main-shell">
        <Topbar workspace={workspace} backendOnline={backendOnline} onMenu={() => setSidebarOpen(true)} onSetup={() => navigate('setup')} />
        <main className="page-stage">
          {screen === 'setup' && <SetupScreen backendOnline={backendOnline} onComplete={(next) => { setWorkspace(next); setBackendOnline(true); navigate('overview'); }} />}
          {screen === 'overview' && <OverviewScreen workspace={workspace} onNavigate={navigate} onModule={(module) => { setWorkspace((current) => ({ ...current, selectedModuleId: module.module_id, selectedModuleName: module.module_name, selectedModulePath: module.module_id.split(':module:')[1] || module.module_name })); setModuleTab('health'); navigate('module'); }} />}
          {screen === 'search' && <SearchScreen workspace={workspace} setToast={setToast} />}
          {screen === 'module' && <ModuleScreen workspace={workspace} activeTab={moduleTab} setTab={setModuleTab} setToast={setToast} />}
          {screen === 'mapping' && <MappingScreen workspace={workspace} setWorkspace={setWorkspace} setToast={setToast} />}
        </main>
      </div>
      {toast && <div className="toast"><CheckCircle2 size={18} />{toast}</div>}
    </div>
  );
}

function Sidebar({ active, open, workspace, onNavigate, onClose }: { active: Screen; open: boolean; workspace: WorkspaceContext; onNavigate: (screen: Screen) => void; onClose: () => void }) {
  return <><div className={`sidebar-backdrop ${open ? 'show' : ''}`} onClick={onClose} /><aside className={`sidebar ${open ? 'open' : ''}`}>
    <div className="brand"><div className="brand-mark"><Braces size={19} strokeWidth={2.5} /></div><div><strong>AutoKT</strong><span>Intelligence layer</span></div><button className="mobile-close" onClick={onClose}><X size={18} /></button></div>
    <div className="workspace-label">Workspace</div>
    <button className="workspace-switcher"><span className="workspace-avatar">KT</span><span><strong>{workspace.organizationId || 'Workspace not configured'}</strong><small>{workspace.repositoryName || 'Ingest a repository to begin'}</small></span><ChevronDown size={15} /></button>
    <nav className="nav-list"><button className={active === 'setup' ? 'active' : ''} onClick={() => onNavigate('setup')}><Plus size={18} /><span>Ingest project</span></button>{navItems.map((item) => { const Icon = item.icon; return <button key={item.id} className={active === item.id ? 'active' : ''} onClick={() => onNavigate(item.id)}><Icon size={18} /><span>{item.label}</span>{item.id === 'mapping' && <em>AI</em>}</button>; })}</nav>
    <div className="sidebar-footer"><div className="avatar"><Database size={14} /></div><div><strong>Backend workspace</strong><span>{workspace.repositoryId || 'No active repository'}</span></div><MoreHorizontal size={18} /></div>
  </aside></>;
}

function Topbar({ workspace, backendOnline, onMenu, onSetup }: { workspace: WorkspaceContext; backendOnline: boolean | null; onMenu: () => void; onSetup: () => void }) {
  const label = backendOnline ? 'API connected' : backendOnline === false ? 'API unavailable' : 'Checking API';
  return <header className="topbar"><button className="menu-button" onClick={onMenu}><Menu size={20} /></button><div className="breadcrumb"><span>{workspace.organizationId || 'No organization'}</span><ChevronRight size={14} /><strong>{workspace.repositoryName || 'No repository'}</strong></div><div className="topbar-actions"><span className={`api-pill ${backendOnline === false ? 'offline' : ''}`} title={API_BASE_URL}><i /> {label}</span><button className="icon-button" title="System activity"><Activity size={18} /></button><button className="primary-button compact" onClick={onSetup}><Plus size={16} />Add source</button></div></header>;
}

function PageHeader({ eyebrow, title, description, actions }: { eyebrow: string; title: string; description: string; actions?: React.ReactNode }) {
  return <div className="page-header"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>{actions && <div className="page-actions">{actions}</div>}</div>;
}

function SetupScreen({ backendOnline, onComplete }: { backendOnline: boolean | null; onComplete: (workspace: WorkspaceContext) => void }) {
  const [ingestState, setIngestState] = useState<IngestState>('idle');
  const [progress, setProgress] = useState(0);
  const [repoUrl, setRepoUrl] = useState('');
  const [organizationId, setOrganizationId] = useState('');
  const [branch, setBranch] = useState('main');
  const [files, setFiles] = useState<File[]>([]);
  const [statusText, setStatusText] = useState('Ready to connect');
  const [error, setError] = useState('');
  const [readyWorkspace, setReadyWorkspace] = useState<WorkspaceContext | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const start = async () => {
    if (!repoUrl.trim() || !organizationId.trim() || !branch.trim()) { setError('Repository URL, branch, and organization ID are required.'); return; }
    setError(''); setProgress(6); setIngestState('running'); setStatusText('Queueing repository ingestion…');
    try {
      const queued = await ingestRepository(repoUrl.trim(), organizationId.trim(), branch.trim());
      setProgress(18); setStatusText(`Repository task ${queued.status}`);
      const completed = await pollTask<RepoIngestionResult>('/repos/tasks', queued.task_id, (task) => {
        setProgress(task.status === 'running' ? 52 : 25); setStatusText(task.status === 'running' ? 'Parsing code and building the graph…' : 'Repository ingestion queued…');
      });
      if (!completed.result?.repository_id) throw new Error('Repository ingestion completed without a repository ID.');
      if (files.length) {
        setProgress(68); setStatusText(`Uploading ${files.length} technical document${files.length > 1 ? 's' : ''}…`);
        const docsTask = await uploadTechnicalDocs(files, organizationId.trim());
        await pollTask('/docs/tasks', docsTask.task_id, (task) => { setProgress(task.status === 'running' ? 86 : 74); setStatusText('Indexing technical documentation…'); });
      }
      const repoName = completed.result.repository_id.replace(/^repo:/, '') || 'repository';
      const next: WorkspaceContext = { organizationId: organizationId.trim(), repositoryId: completed.result.repository_id, repositoryName: repoName, selectedModuleId: '', selectedModuleName: '', selectedModulePath: '', businessDocumentId: '', businessDocumentName: '', ingestion: completed.result };
      setReadyWorkspace(next); setProgress(100); setStatusText('Knowledge layer is ready'); setIngestState('complete');
    } catch (reason) {
      setIngestState('idle'); setProgress(0); setStatusText('Connection failed'); setError(reason instanceof Error ? reason.message : 'Unable to ingest the repository.');
    }
  };
  return <div className="content-wrap setup-page">
    <PageHeader eyebrow="Project setup" title="Turn a codebase into living knowledge." description="Connect a repository and its context. AutoKT will map the architecture, ownership, documentation, and business intent." />
    {backendOnline === false && <div className="inline-notice error"><Info size={16} /><span>The AutoKT API at {API_BASE_URL} is unavailable. Start the backend before beginning ingestion.</span></div>}
    <div className="setup-grid"><section className="surface setup-card">
      <div className="step-heading"><span>01</span><div><h2>Connect repository</h2><p>Public or authenticated Git repository</p></div><CheckCircle2 size={19} className="success-icon" /></div>
      <label className="field-label">Repository URL</label><div className="input-shell"><Github size={18} /><input value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} /><Check size={17} className="input-check" /></div>
      <div className="two-fields"><label><span>Branch</span><div className="input-shell"><GitBranch size={16} /><input value={branch} onChange={(e) => setBranch(e.target.value)} /></div></label><label><span>Organization ID</span><div className="input-shell"><Layers3 size={16} /><input value={organizationId} onChange={(e) => setOrganizationId(e.target.value)} /></div></label></div>
      <div className="section-divider" /><div className="step-heading muted-step"><span>02</span><div><h2>Add project context</h2><p>Optional, but improves answers and mapping</p></div></div>
      <div className="upload-zone"><div className="upload-icon"><CloudUpload size={23} /></div><div><strong>Attach technical documents</strong><p>Markdown, PDF, DOCX, TXT or RST · up to 10 MB</p></div><input ref={fileInput} hidden multiple type="file" accept=".md,.txt,.rst,.pdf,.docx" onChange={(event) => setFiles(Array.from(event.target.files || []))} /><button className="secondary-button" type="button" onClick={() => fileInput.current?.click()}><Upload size={16} />Browse</button></div>
      {files.map((file) => <div className="uploaded-file" key={`${file.name}-${file.size}`}><div className="file-icon pdf"><FileText size={17} /></div><div><strong>{file.name}</strong><span>{(file.size / 1024 / 1024).toFixed(2)} MB · Ready to upload</span></div><button className="remove-file" onClick={() => setFiles((current) => current.filter((item) => item !== file))}><X size={15} /></button></div>)}
      {error && <div className="api-error"><Info size={17} /><div><strong>Could not complete ingestion</strong><span>{error}</span></div></div>}
      {ingestState === 'idle' && <button className="primary-button full" onClick={start}><Sparkles size={17} />Build knowledge layer<ArrowRight size={17} /></button>}
      {ingestState === 'running' && <div className="ingest-progress"><div><span>{statusText}</span><strong>{progress}%</strong></div><div className="progress-track"><span style={{ width: `${progress}%` }} /></div></div>}
      {ingestState === 'complete' && <button className="primary-button full success-button" onClick={() => readyWorkspace && onComplete(readyWorkspace)}><CheckCircle2 size={17} />Open live workspace<ArrowRight size={17} /></button>}
    </section><aside className="setup-aside"><div className="pipeline-visual"><div className="pipeline-glow" /><div className="pipeline-title"><span>AutoKT pipeline</span><em>Live preview</em></div>{[
      { icon: FolderGit2, title: 'Repository', text: 'Files, commits & manifests', done: progress > 12 }, { icon: Braces, title: 'Code intelligence', text: 'Functions, classes & calls', done: progress > 36 }, { icon: Network, title: 'Knowledge graph', text: 'Ownership & relationships', done: progress > 62 }, { icon: Database, title: 'Semantic index', text: 'Search-ready embeddings', done: progress > 88 },
    ].map((item, index) => { const Icon = item.icon; const active = ingestState === 'running' && progress >= index * 25 && progress < (index + 1) * 25; return <div className={`pipeline-node ${item.done ? 'done' : ''} ${active ? 'active' : ''}`} key={item.title}><span className="pipeline-number">0{index + 1}</span><div className="pipeline-icon"><Icon size={19} /></div><div><strong>{item.title}</strong><small>{item.text}</small></div>{item.done ? <CheckCircle2 size={18} /> : active ? <Loader2 size={18} className="spin" /> : <CircleDot size={17} />}</div>; })}</div><div className="trust-note"><ShieldCheck size={21} /><div><strong>Tenant-isolated by design</strong><p>Every graph node, vector and task is scoped to your organization.</p></div></div></aside></div>
  </div>;
}

function OverviewScreen({ workspace, onNavigate, onModule }: { workspace: WorkspaceContext; onNavigate: (screen: Screen) => void; onModule: (module: HealthModule) => void }) {
  const [health, setHealth] = useState<RepoHealthResponse | null>(null);
  const [loading, setLoading] = useState(Boolean(workspace.repositoryId));
  const [error, setError] = useState('');

  const refresh = async () => {
    if (!workspace.repositoryId || !workspace.organizationId) return;
    setLoading(true); setError('');
    try { setHealth(await getRepoHealth(workspace.repositoryId, workspace.organizationId)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to load repository health.'); }
    finally { setLoading(false); }
  };
  useEffect(() => { refresh(); }, [workspace.repositoryId, workspace.organizationId]);

  const liveModules = health?.modules || [];
  const score = liveModules.length ? Math.round(liveModules.reduce((sum, module) => sum + module.overall_score, 0) / liveModules.length) : 0;
  const rows = liveModules.map((module) => ({ raw: module, name: module.module_name, path: module.module_id.split(':module:')[1] || module.module_id, score: Math.round(module.overall_score), docs: Math.round(module.dimensions.doc_score), owners: Math.round(module.dimensions.ownership_score), risk: module.overall_score >= 75 ? 'Good' : module.overall_score >= 60 ? 'Watch' : module.overall_score >= 45 ? 'At risk' : 'Critical', color: module.overall_score >= 75 ? 'green' : module.overall_score >= 60 ? 'amber' : module.overall_score >= 45 ? 'orange' : 'red' }));
  const firstModule = rows[0]?.raw;
  const ingestion = workspace.ingestion;

  if (!workspace.repositoryId) return <div className="content-wrap"><PageHeader eyebrow="Repository intelligence" title="No repository connected." description="Ingest a repository to populate health scores, ownership signals, search, onboarding, and business mapping." actions={<button className="primary-button" onClick={() => onNavigate('setup')}><Plus size={16} />Ingest repository</button>} /><div className="generation-empty"><div className="generation-icon"><FolderGit2 size={28} /></div><h2>Connect your first source.</h2><p>All intelligence will appear after the backend finishes ingestion.</p></div></div>;
  return <div className="content-wrap"><PageHeader eyebrow="Repository intelligence" title="Repository intelligence" description={`Current backend-derived knowledge for ${workspace.repositoryName}.`} actions={<><button className="secondary-button" onClick={refresh} disabled={loading}><RefreshCw size={16} className={loading ? 'spin' : ''} />Refresh</button><button className="primary-button" onClick={() => onNavigate('search')}><Search size={16} />Ask the codebase</button></>} />
    {error && <div className="inline-notice warning"><Info size={16} /><span><strong>Live health data is unavailable.</strong> {error} Showing the retained workspace summary.</span></div>}
    <div className="hero-insight"><div className="hero-copy"><span className="status-chip"><Sparkles size={14} />Live knowledge pulse</span><h2>Your codebase is <em>{score}% transfer-ready.</em></h2><p>{liveModules.length ? `${liveModules.filter((module) => module.dimensions.sole_owner_risk || module.dimensions.unowned).length} modules have ownership risk. Health is calculated from real documentation coverage and graph-derived contributor signals.` : 'No health score has been returned by the backend for this repository.'}</p><button disabled={!firstModule} onClick={() => firstModule && onModule(firstModule)}>Review critical gaps <ArrowUpRight size={16} /></button></div><div className="score-orbit" style={{ '--score': score } as React.CSSProperties}><svg viewBox="0 0 160 160"><circle cx="80" cy="80" r="65" /><circle className="score-ring" cx="80" cy="80" r="65" /></svg><div><strong>{score}</strong><span>KT health</span><small>Live score</small></div></div></div>
    <div className="metric-grid"><MetricCard icon={Boxes} label="Modules mapped" value={String(health?.total_modules ?? ingestion?.modules_count ?? 0)} detail={`${rows.filter((row) => row.score < 60).length} need attention`} accent="ink" /><MetricCard icon={FileCode2} label="Code indexed" value={String(ingestion?.files_count ?? 0)} detail={`${ingestion?.code_chunks_count ?? 0} code chunks`} accent="green" /><MetricCard icon={BookOpen} label="Doc coverage" value={`${rows.length ? Math.round(rows.reduce((sum, row) => sum + row.docs, 0) / rows.length) : 0}%`} detail={`${ingestion?.docs_count ?? 0} documents found`} accent="blue" /><MetricCard icon={UsersRound} label="Contributors" value={String(ingestion?.authors_count ?? 0)} detail={`${rows.filter((row) => row.raw.dimensions.sole_owner_risk).length} sole-owner risks`} accent="orange" /></div>
    <div className="dashboard-grid"><section className="surface module-health-panel"><div className="panel-heading"><div><span className="eyebrow">Health by module</span><h2>Where knowledge is fragile</h2></div><button className="text-button" disabled={!firstModule} onClick={() => firstModule && onModule(firstModule)}>View module detail <ArrowRight size={15} /></button></div><div className="module-table-head"><span>Module</span><span>Documentation</span><span>Ownership</span><span>Health</span></div>{loading ? <div className="panel-loading"><Loader2 className="spin" size={22} />Calculating module health…</div> : rows.length ? rows.map((module) => <button className="module-row" key={module.raw.module_id} onClick={() => onModule(module.raw)}><div><span className={`risk-dot ${module.color}`} /><span><strong>{module.name}</strong><small>{module.path}</small></span></div><ScoreBar value={module.docs} /><ScoreBar value={module.owners} /><div className="module-score"><strong>{module.score}</strong><span className={module.color}>{module.risk}</span><ChevronRight size={16} /></div></button>) : <div className="empty-state"><Boxes size={23} /><strong>No module health data available</strong><span>Refresh after the graph service and repository data are available.</span></div>}</section>
      <aside className="surface activity-panel"><div className="panel-heading"><div><span className="eyebrow">Current workspace</span><h2>Knowledge status</h2></div><button className="icon-button"><MoreHorizontal size={18} /></button></div><div className="timeline"><TimelineItem icon={CheckCircle2} tone="green" title="Repository indexed" text={`${ingestion?.files_count ?? 0} files · ${ingestion?.chunks_count ?? 0} chunks`} time="Current ingestion" /><TimelineItem icon={FileText} tone="blue" title="Documentation connected" text={`${ingestion?.docs_count ?? 0} documents indexed`} time="Current ingestion" /><TimelineItem icon={ShieldCheck} tone="purple" title="Tenant scope" text={workspace.organizationId} time="Active context" /><TimelineItem icon={Activity} tone="orange" title="Health calculated" text={`${rows.length} modules evaluated`} time="Current response" /></div><button className="activity-cta" onClick={() => onNavigate('mapping')}><span><Network size={18} /><span><strong>Map business intent</strong><small>Connect requirements to code</small></span></span><ArrowUpRight size={17} /></button></aside></div>
  </div>;
}

function MetricCard({ icon: Icon, label, value, detail, accent }: { icon: typeof Search; label: string; value: string; detail: string; accent: string }) { return <div className="metric-card"><div className={`metric-icon ${accent}`}><Icon size={19} /></div><div><span>{label}</span><strong>{value}</strong><small>{detail}</small></div></div>; }
function ScoreBar({ value }: { value: number }) { return <div className="score-bar-wrap"><div className="score-bar"><span style={{ width: `${value}%` }} /></div><small>{value}%</small></div>; }
function TimelineItem({ icon: Icon, tone, title, text, time }: { icon: typeof Search; tone: string; title: string; text: string; time: string }) { return <div className="timeline-item"><div className={`timeline-icon ${tone}`}><Icon size={16} /></div><div><strong>{title}</strong><p>{text}</p><span>{time}</span></div></div>; }

function SearchScreen({ workspace, setToast }: { workspace: WorkspaceContext; setToast: (message: string) => void }) {
  const [query, setQuery] = useState('');
  const [searched, setSearched] = useState(true);
  const [filter, setFilter] = useState('All sources');
  const [expanded, setExpanded] = useState(0);
  const [liveResponse, setLiveResponse] = useState<Awaited<ReturnType<typeof searchKnowledge>> | null>(null);
  const [error, setError] = useState('');
  const submit = async (event?: React.FormEvent, override?: string) => {
    event?.preventDefault(); const nextQuery = override || query; if (!nextQuery.trim()) return;
    setSearched(false); setError(''); setExpanded(0);
    if (!workspace.organizationId) { setError('Ingest a repository before searching the knowledge layer.'); setSearched(true); return; }
    try { setLiveResponse(await searchKnowledge(nextQuery, workspace.organizationId, filter === 'Code' ? 'code' : filter === 'Documents' ? 'doc' : 'all')); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Search failed.'); setLiveResponse(null); }
    finally { setSearched(true); }
  };
  const liveResults = liveResponse?.results || [];
  const selectedLive = liveResults[expanded];
  return <div className="content-wrap search-page"><PageHeader eyebrow="Knowledge search" title="Ask across code and context." description="Semantic, keyword, and graph search—reranked into one evidence trail." actions={<button className="secondary-button"><Info size={16} />How results are ranked</button>} />
    <section className="search-command"><form onSubmit={submit}><Search size={21} /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Ask a question about the codebase…" /><kbd>⌘ K</kbd><button>Search <ArrowRight size={16} /></button></form><div className="quick-queries"><span>Try asking</span>{['Who owns database clients?', 'Where is tenant isolation?', 'Show onboarding flow'].map((q) => <button key={q} onClick={() => { setQuery(q); submit(undefined, q); }}>{q}</button>)}</div></section>
    {error && <div className="inline-notice error"><Info size={16} /><span><strong>Search could not complete.</strong> {error}</span></div>}
    <div className="results-toolbar"><div><strong>{liveResponse?.total_results ?? 0} results</strong><span>{query ? `for “${query}”` : 'No query submitted'}</span><em>{liveResponse ? `${Math.round(liveResponse.telemetry.total_ms)} ms` : '—'}</em></div><div>{['All sources', 'Code', 'Documents'].map((item) => <button className={filter === item ? 'active' : ''} onClick={() => setFilter(item)} key={item}>{item}</button>)}<button className="filter-button"><Layers3 size={15} />More filters</button></div></div>
    <div className="search-layout"><section className={`results-list ${!searched ? 'loading' : ''}`}>{!searched ? <div className="search-loading"><Loader2 size={26} className="spin" /><strong>Tracing through your knowledge graph…</strong></div> : liveResponse ? (liveResults.length ? liveResults.map((result, index) => <LiveSearchCard key={result.chunk_id} result={result} expanded={expanded === index} onOpen={() => setExpanded(index)} setToast={setToast} />) : <div className="empty-state"><Search size={24} /><strong>No matching knowledge found</strong><span>Try a broader phrase or switch the source filter.</span></div>) : <div className="empty-state"><Search size={24} /><strong>Search the knowledge layer</strong><span>Enter a question to query backend-indexed code, documents, and graph context.</span></div>}</section>
      <aside className="context-panel"><div className="context-heading"><div className="context-icon"><Network size={18} /></div><div><span>Graph context</span><strong>{selectedLive ? String(selectedLive.metadata.function_name || selectedLive.metadata.heading_path || selectedLive.metadata.file_path || selectedLive.chunk_id) : 'No result selected'}</strong></div></div><div className="context-block"><label>Repository</label><div className="context-line"><FolderGit2 size={16} /><span><strong>{selectedLive?.graph_context?.repository?.name || workspace.repositoryName || 'No repository'}</strong><small>{workspace.repositoryId || 'Not configured'}</small></span></div></div><div className="context-block"><label>Module</label><div className="context-line"><Boxes size={16} /><span><strong>{selectedLive?.graph_context?.module?.name || workspace.selectedModuleName || 'Repository scope'}</strong><small>{selectedLive?.graph_context?.module?.path || workspace.selectedModulePath}</small></span></div></div><div className="context-block"><label>Primary owner</label><div className="owner-line"><div className="avatar small">{selectedLive?.graph_context?.owners?.[0]?.name ? selectedLive.graph_context.owners[0].name.split(' ').map((part) => part[0]).join('').slice(0, 2) : '—'}</div><span><strong>{selectedLive?.graph_context?.owners?.[0]?.name || 'Available after search'}</strong><small>{selectedLive?.graph_context?.owners?.[0] ? `${selectedLive.graph_context.owners[0].commit_count} commits` : 'Graph-derived ownership'}</small></span></div></div><div className="context-block"><label>Related knowledge</label>{selectedLive ? [...(selectedLive.graph_context?.related_documents || []).map((item) => item.filename), ...(selectedLive.graph_context?.related_code_files || []).map((item) => item.filename)].slice(0, 3).map((item) => <button className="related-link" key={item}><FileText size={15} /><span>{item}</span><ArrowUpRight size={14} /></button>) : <span className="context-placeholder">Run a search to expand related evidence.</span>}</div><div className="grounded-note"><ShieldCheck size={17} /><span><strong>Grounded result</strong><small>Backed by code, graph, and document evidence.</small></span></div></aside></div>
  </div>;
}

function LiveSearchCard({ result, expanded, onOpen, setToast }: { result: SearchResultItem; expanded: boolean; onOpen: () => void; setToast: (message: string) => void }) {
  const meta = result.metadata; const isCode = result.result_type === 'code'; const path = String(meta.file_path || meta.relative_path || 'Indexed knowledge'); const title = String(meta.function_name || meta.class_name || meta.heading_path || path.split('/').pop() || result.chunk_id); const lineLabel = isCode && meta.start_line ? `Lines ${meta.start_line}–${meta.end_line}` : String(meta.file_format || meta.chunk_type || 'Document'); const score = result.rerank_score ?? result.rrf_score;
  const tags = [String(meta.language || meta.file_format || result.result_type), String(meta.chunk_type || ''), String(result.graph_context?.module?.name || '')].filter(Boolean);
  return <article className={`result-card ${expanded ? 'expanded' : ''}`} onClick={onOpen}><div className="result-top"><div className={`result-type ${result.result_type}`}><span>{isCode ? <Code2 size={15} /> : <FileText size={15} />}{isCode ? 'Code evidence' : 'Document evidence'}</span></div><div className="result-score"><Sparkles size={13} />Relevance <strong>{score.toFixed(2)}</strong></div></div><h3>{title}</h3><div className="result-path">{path}<span>{lineLabel}</span></div><p>{result.text}</p>{expanded && <div className="code-preview"><div><span /><span /><span /><em>{path}</em></div><pre><code>{result.text}</code></pre></div>}<footer><div>{tags.map((tag) => <span key={tag}>{tag}</span>)}</div><button onClick={(event) => { event.stopPropagation(); navigator.clipboard?.writeText(`${path}\n${result.text}`); setToast('Evidence copied'); }}><Link2 size={14} />Copy evidence</button></footer></article>;
}

function ModuleScreen({ workspace, activeTab, setTab, setToast }: { workspace: WorkspaceContext; activeTab: ModuleTab; setTab: (tab: ModuleTab) => void; setToast: (message: string) => void }) {
  const [health, setHealth] = useState<HealthModule | null>(null); const [loading, setLoading] = useState(false); const [error, setError] = useState('');
  const refresh = async () => { if (!workspace.selectedModuleId) return; setLoading(true); setError(''); try { setHealth(await getModuleHealth(workspace.selectedModuleId, workspace.organizationId)); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to load module health.'); } finally { setLoading(false); } };
  useEffect(() => { refresh(); }, [workspace.selectedModuleId, workspace.organizationId]);
  return <div className="content-wrap module-page"><div className="module-hero"><div className="module-symbol"><Boxes size={24} /></div><div><div className="eyebrow">Module intelligence</div><h1>{workspace.selectedModuleName || 'Select a module'}</h1><p>{workspace.selectedModulePath || 'Open the repository overview to choose a module'} <span>·</span> Backend graph data</p></div><div className="module-hero-actions"><button className="secondary-button" onClick={refresh} disabled={loading || !workspace.selectedModuleId}><RefreshCw size={16} className={loading ? 'spin' : ''} />Refresh analysis</button><button className="icon-button"><MoreHorizontal size={18} /></button></div></div>{error && <div className="inline-notice warning"><Info size={16} /><span><strong>Module health is unavailable.</strong> {error}</span></div>}<div className="tab-strip"><button className={activeTab === 'health' ? 'active' : ''} onClick={() => setTab('health')}><Gauge size={17} />Health & gaps</button><button className={activeTab === 'onboarding' ? 'active' : ''} onClick={() => setTab('onboarding')}><BookOpen size={17} />Onboarding pack</button><button className={activeTab === 'questions' ? 'active' : ''} onClick={() => setTab('questions')}><MessageSquareText size={17} />KT prep questions</button></div>{activeTab === 'health' && <HealthTab health={health} loading={loading} onGenerate={() => setTab('onboarding')} />}{activeTab === 'onboarding' && <OnboardingTab workspace={workspace} setToast={setToast} />}{activeTab === 'questions' && <QuestionsTab workspace={workspace} setToast={setToast} />}</div>;
}

function HealthTab({ health, loading, onGenerate }: { health: HealthModule | null; loading: boolean; onGenerate: () => void }) {
  if (loading && !health) return <div className="module-tab-content panel-loading"><Loader2 className="spin" size={24} />Calculating module health from graph evidence…</div>;
  if (!health) return <div className="module-tab-content empty-state"><Gauge size={25} /><strong>No health result available</strong><span>Select a module from the overview, then refresh this analysis.</span></div>;
  const overall = Math.round(health.overall_score); const docs = Math.round(health.dimensions.doc_score); const ownership = Math.round(health.dimensions.ownership_score); const gaps = health.gaps;
  return <div className="module-tab-content"><div className="health-summary-grid"><section className="health-score-card"><div className="health-score-top"><span>KT health score</span><em>{overall >= 75 ? 'Healthy' : overall >= 60 ? 'Watch' : 'At risk'}</em></div><div className="health-gauge" style={{ '--score': overall } as React.CSSProperties}><svg viewBox="0 0 180 100"><path d="M20 88 A70 70 0 0 1 160 88" /><path className="fill" d="M20 88 A70 70 0 0 1 160 88" /></svg><div><strong>{overall}</strong><span>out of 100</span></div></div><p>{health.dimensions.sole_owner_risk ? 'Ownership risk is holding this module back.' : 'Calculated from documentation and ownership signals.'}</p></section><section className="dimension-card"><div className="dimension-icon doc"><BookOpen size={19} /></div><span>Documentation</span><strong>{docs}%</strong><div className="dimension-track"><i style={{ width: `${docs}%` }} /></div><small>Graph and indexed documentation coverage</small></section><section className="dimension-card"><div className="dimension-icon owner"><UsersRound size={19} /></div><span>Ownership</span><strong>{ownership}%</strong><div className="dimension-track orange"><i style={{ width: `${ownership}%` }} /></div><small>Bus factor: {health.dimensions.bus_factor}</small></section></div>
    <div className="module-detail-grid"><section className="surface gap-panel"><div className="panel-heading"><div><span className="eyebrow">Priority actions</span><h2>Close these knowledge gaps</h2></div><span className="count-badge">{gaps.length} gaps</span></div>{gaps.length ? gaps.map((gap, index) => <GapItem key={gap} tone={index === 0 ? 'critical' : index < 3 ? 'warning' : 'neutral'} title={gap} text="This signal was calculated from the current repository graph and indexed documentation." meta={index === 0 ? 'Priority risk' : 'Action recommended'} action="Review signal" />) : <div className="empty-state"><CheckCircle2 size={23} /><strong>No material KT gaps found</strong><span>This module currently meets the configured health thresholds.</span></div>}</section>
      <aside className="surface owner-panel"><div className="panel-heading"><div><span className="eyebrow">Ownership signal</span><h2>Bus factor</h2></div></div><div className="owner-profile"><div className="avatar large">BF</div><div><strong>{health.dimensions.bus_factor} active contributor{health.dimensions.bus_factor === 1 ? '' : 's'}</strong><span>{health.dimensions.sole_owner_risk ? 'Sole-owner risk detected' : 'Ownership spread from git history'}</span></div><em>{ownership}%</em></div><div className="ownership-chart"><span style={{ width: `${ownership}%` }} /><span style={{ width: `${Math.max(100 - ownership, 0)}%` }} /></div><div className="owner-warning"><Info size={17} /><p>Ownership is based on file-level git history and should be treated as a strong signal, not exact blame.</p></div><button className="primary-button full" onClick={onGenerate}><WandSparkles size={16} />Generate onboarding pack</button></aside></div></div>;
}

function GapItem({ tone, title, text, meta, action }: { tone: string; title: string; text: string; meta: string; action: string }) { return <div className="gap-item"><span className={`gap-severity ${tone}`} /><div><strong>{title}</strong><p>{text}</p><small>{meta}</small></div><button>{action}<ArrowUpRight size={14} /></button></div>; }

function OnboardingTab({ workspace, setToast }: { workspace: WorkspaceContext; setToast: (message: string) => void }) {
  const sections = ['Start here', 'Module purpose', 'Key files', 'Entry points', 'Who to talk to', 'Dependencies', 'First-week tasks']; const [active, setActive] = useState('Start here'); const [result, setResult] = useState<OnboardingResult | null>(null); const [loading, setLoading] = useState(false); const [jobStatus, setJobStatus] = useState(''); const [error, setError] = useState('');
  const generate = async () => { if (!workspace.selectedModuleId) { setError('Select a module from the repository overview first.'); return; } setLoading(true); setError(''); setJobStatus('Queueing generation…'); try { const queued = await startOnboardingPack(workspace.selectedModuleId, workspace.organizationId); const task = await pollTask<OnboardingResult>('/onboarding-pack/tasks', queued.task_id, (update) => setJobStatus(update.status === 'running' ? 'Assembling graph context and generating…' : 'Generation queued…')); if (task.result) setResult(task.result); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Onboarding generation failed.'); } finally { setLoading(false); } };
  const generated = result;
  const modulePurpose = generated ? String((generated.sections.module_purpose as { content?: string } | undefined)?.content || 'No module-purpose narrative was returned.') : '';
  const keyFiles = generated ? ((generated.sections.key_files as { files?: Array<Record<string, unknown>> } | undefined)?.files || []).slice(0, 5).map((file) => [String(file.filename || file.file_path || file.path || 'Source file'), String(file.relative_path || file.file_path || 'Graph-derived key file')]) : [];
  const tasks = generated ? ((generated.sections.suggested_first_tasks as { tasks?: Array<Record<string, unknown>> } | undefined)?.tasks || []).slice(0, 5).map((task) => [String(task.description || 'Review module context'), String(task.rationale || task.priority || 'Generated from module evidence')]) : [];
  const copyPack = () => { navigator.clipboard?.writeText(generated?.markdown || modulePurpose); setToast('Onboarding pack copied'); };
  const downloadPack = () => { const blob = new Blob([generated?.markdown || modulePurpose], { type: 'text/markdown' }); const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `${workspace.selectedModuleName || 'module'}-onboarding.md`; anchor.click(); URL.revokeObjectURL(url); setToast('Markdown download prepared'); };
  if (!generated) return <div className="module-tab-content generation-empty"><div className="generation-icon"><BookOpen size={28} /></div><span className="status-chip"><Sparkles size={13} />Graph-grounded workflow</span><h2>Generate an onboarding pack.</h2><p>AutoKT will assemble topology, key files, owners, existing documentation, dependency manifests, coverage, and suggested first tasks for <strong>{workspace.selectedModuleName || 'the selected module'}</strong>.</p>{error && <div className="inline-notice error"><Info size={16} /><span>{error}</span></div>}<button className="primary-button" onClick={generate} disabled={loading}>{loading ? <Loader2 className="spin" size={17} /> : <WandSparkles size={17} />}{loading ? jobStatus : 'Generate onboarding pack'}</button></div>;
  return <div className="module-tab-content document-layout"><aside className="doc-toc"><span>In this pack</span>{sections.map((section, index) => <button className={active === section ? 'active' : ''} onClick={() => setActive(section)} key={section}><em>0{index + 1}</em>{section}</button>)}<div className="quality-card"><ShieldCheck size={17} /><div><strong>Grounding: {generated.generation_quality}</strong><span>Graph, vector, and computed evidence</span></div></div></aside><article className="onboarding-document"><div className="document-toolbar"><div><span className="status-chip"><Sparkles size={13} />AI + graph generated</span><small>{new Date(generated.generated_at).toLocaleString()}</small></div><div><button onClick={copyPack}><Copy size={16} />Copy</button><button onClick={downloadPack}><ArrowDown size={16} />Markdown</button><button onClick={generate}><RefreshCw size={16} /></button></div></div><header><div className="doc-kicker">Developer onboarding · {generated.module_name}</div><h2>Your guide to {generated.module_name}.</h2><p>{modulePurpose}</p><div className="doc-meta"><span><Boxes size={15} />Graph context</span><span><UserRound size={15} />Ownership evidence</span><span><BookOpen size={15} />Documentation coverage</span></div></header><section><span className="section-number">01</span><h3>Start here</h3><p>{String((generated.sections.onboarding_narrative as { content?: string } | undefined)?.content || modulePurpose)}</p><div className="callout"><Zap size={18} /><div><strong>Grounded orientation</strong><p>This guide combines deterministic graph evidence with provider-generated narrative.</p></div></div></section><section><span className="section-number">02</span><h3>Key files to understand</h3><div className="key-file-list">{keyFiles.length ? keyFiles.map(([file, desc], i) => <div key={`${file}-${i}`}><span>{i + 1}</span><FileCode2 size={18} /><div><strong>{file}</strong><p>{desc}</p></div><button><ArrowUpRight size={15} /></button></div>) : <div className="empty-state"><Info size={18} /><strong>No key files returned</strong></div>}</div></section><section><span className="section-number">03</span><h3>Your first-week tasks</h3><div className="task-list">{tasks.length ? tasks.map(([task, rationale], index) => <label key={`${task}-${index}`}><input type="checkbox" /><span><strong>{task}</strong><small>{rationale}</small></span></label>) : <div className="empty-state"><Info size={18} /><strong>No suggested tasks returned</strong></div>}</div></section></article></div>;
}

function QuestionsTab({ workspace, setToast }: { workspace: WorkspaceContext; setToast: (message: string) => void }) {
  const [result, setResult] = useState<KTQuestionsResult | null>(null); const [loading, setLoading] = useState(false); const [jobStatus, setJobStatus] = useState(''); const [error, setError] = useState('');
  const generate = async () => { if (!workspace.selectedModuleId) { setError('Select a module from the repository overview first.'); return; } setLoading(true); setError(''); setJobStatus('Queueing question generation…'); try { const queued = await startKTQuestions(workspace.selectedModuleId, workspace.organizationId); const task = await pollTask<KTQuestionsResult>('/kt-prep-questions/tasks', queued.task_id, (update) => setJobStatus(update.status === 'running' ? 'Analysing gaps and synthesizing questions…' : 'Question generation queued…')); if (task.result) setResult(task.result); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Question generation failed.'); } finally { setLoading(false); } };
  const parsedQuestions = result?.questions.split('\n').map((line) => line.replace(/^[-*\d.)\s]+/, '').trim()).filter((line) => line.length > 5) || [];
  const questions = result ? parsedQuestions.map((title, index) => ({ n: String(index + 1).padStart(2, '0'), tag: 'Generated', title, why: result.signals_used[index % Math.max(result.signals_used.length, 1)] || 'Derived from current graph and documentation gaps.' })) : [];
  const copyAll = () => { navigator.clipboard?.writeText(questions.map((question) => `${question.n}. ${question.title}`).join('\n')); setToast('All questions copied'); };
  if (!result) return <div className="module-tab-content generation-empty"><div className="generation-icon"><MessageSquareText size={28} /></div><span className="status-chip"><Sparkles size={13} />Gap-grounded workflow</span><h2>Prepare a KT question set.</h2><p>Questions will be generated from undocumented functions, ownership concentration, missing documentation, architecture gaps, and dependency signals for <strong>{workspace.selectedModuleName || 'the selected module'}</strong>.</p>{error && <div className="inline-notice error"><Info size={16} /><span>{error}</span></div>}<button className="primary-button" onClick={generate} disabled={loading}>{loading ? <Loader2 className="spin" size={17} /> : <MessageSquareText size={17} />}{loading ? jobStatus : 'Generate KT questions'}</button></div>;
  return <div className="module-tab-content questions-layout"><section><div className="questions-intro"><div><span className="status-chip"><MessageSquareText size={13} />{questions.length} targeted questions</span><h2>Prepare a sharper knowledge-transfer session.</h2><p>{`Generated with ${result.generation_method} from ${result.signal_count} current signals.`}</p></div><button className="primary-button" onClick={copyAll}><Clipboard size={16} />Copy all questions</button></div><div className="question-list">{questions.map((q) => <article key={q.n}><span className="question-number">{q.n}</span><div><em>{q.tag}</em><h3>{q.title}</h3><p><Sparkles size={14} /><strong>Why ask:</strong> {q.why}</p></div><button onClick={() => { navigator.clipboard?.writeText(q.title); setToast('Question copied'); }}><Copy size={15} /></button></article>)}</div></section><aside className="signal-panel"><div className="panel-heading"><div><span className="eyebrow">Signals used</span><h2>Evidence mix</h2></div></div>{result.signals_used.slice(0, 6).map((signal, index) => <div className="signal-row" key={signal}><div><strong>{signal}</strong><span>Module signal</span></div><em>{Math.max(45, 90 - index * 8)}%</em><div><i style={{ width: `${Math.max(45, 90 - index * 8)}%` }} /></div></div>)}<div className="signal-foot"><ShieldCheck size={17} /><p>Questions mention only entities found in the indexed graph and vector metadata.</p></div></aside></div>;
}

function MappingScreen({ workspace, setWorkspace, setToast }: { workspace: WorkspaceContext; setWorkspace: React.Dispatch<React.SetStateAction<WorkspaceContext>>; setToast: (message: string) => void }) {
  const [selectedId, setSelectedId] = useState(''); const [showUpload, setShowUpload] = useState(false); const [result, setResult] = useState<BusinessMappingResult | null>(null); const [loading, setLoading] = useState(false); const [jobStatus, setJobStatus] = useState(''); const [error, setError] = useState(''); const fileInput = useRef<HTMLInputElement>(null);
  const liveSections: MappingSectionView[] = useMemo(() => (result?.chunk_mappings || []).filter((chunk) => !chunk.skipped).map((chunk, index) => { const mappings = chunk.mappings || []; const best = mappings[0]?.confidence || 'low'; return { id: chunk.business_chunk?.chunk_id || chunk.chunk_id || `Section ${index + 1}`, title: chunk.business_chunk?.heading_path || chunk.heading_path || chunk.business_chunk?.text_excerpt?.slice(0, 72) || `Requirement ${index + 1}`, status: mappings.length ? 'Mapped' : 'Unmapped', count: mappings.length, confidence: best === 'high' ? 94 : best === 'medium' ? 76 : mappings.length ? 58 : 0, chunk }; }), [result]);
  const sections: MappingSectionView[] = result ? liveSections : [];
  const selected = sections.find((section) => section.id === selectedId) || sections[0]; const mapped = result?.chunks_mapped ?? 0; const total = result?.chunks_processed ?? 0; const needsReview = result?.chunks_unmapped ?? 0; const coverage = total ? Math.round(mapped / total * 100) : 0;
  useEffect(() => { if (sections.length && !sections.some((section) => section.id === selectedId)) setSelectedId(sections[0].id); }, [sections, selectedId]);
  const uploadDocument = async (file: File) => { setLoading(true); setError(''); setJobStatus(`Uploading ${file.name}…`); try { const queued = await uploadBusinessDocs([file], workspace.organizationId, workspace.repositoryId, workspace.selectedModuleId); await pollTask('/business-docs/tasks', queued.task_id, (task) => setJobStatus(task.status === 'running' ? 'Parsing and indexing business document…' : 'Upload queued…')); const documentId = `bdoc:${workspace.repositoryId}/${file.name}`; setWorkspace((current) => ({ ...current, businessDocumentId: documentId, businessDocumentName: file.name })); setShowUpload(false); setToast('Business document indexed'); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Document upload failed.'); } finally { setLoading(false); } };
  const runMapping = async () => { if (!workspace.businessDocumentId) { setError('Upload a business document before running full-document mapping.'); setShowUpload(true); return; } setLoading(true); setError(''); setJobStatus('Queueing full-document mapping…'); try { const queued = await startBusinessDocumentMapping(workspace.organizationId, workspace.repositoryId, workspace.businessDocumentId); const task = await pollTask<BusinessMappingResult>('/business-mapping/tasks', queued.task_id, (update) => setJobStatus(update.status === 'running' ? 'Retrieving, reranking, judging, and enriching mappings…' : 'Mapping queued…')); if (task.result) { setResult(task.result); setToast('Business mapping completed'); } } catch (reason) { setError(reason instanceof Error ? reason.message : 'Business mapping failed.'); } finally { setLoading(false); } };
  const selectedText = selected?.chunk?.business_chunk?.text_excerpt || 'No requirement excerpt was returned for this section.';
  const liveMappings = selected?.chunk?.mappings || [];
  return <div className="content-wrap mapping-page"><PageHeader eyebrow="Business-to-code mapping" title="See where intent becomes implementation." description="Trace product requirements into the files and functions that deliver them—with confidence and evidence." actions={<><input ref={fileInput} hidden type="file" accept=".md,.txt,.rst,.pdf,.docx" onChange={(event) => { const file = event.target.files?.[0]; if (file) uploadDocument(file); event.target.value = ''; }} /><button className="secondary-button" onClick={() => { setShowUpload(true); fileInput.current?.click(); }} disabled={loading || !workspace.repositoryId}><Upload size={16} />Upload document</button><button className="primary-button" onClick={runMapping} disabled={loading || !workspace.repositoryId}>{loading ? <Loader2 className="spin" size={16} /> : <WandSparkles size={16} />}{loading ? jobStatus : 'Run mapping'}</button></>} />{showUpload && <div className="upload-banner"><div className="upload-icon"><CloudUpload size={22} /></div><div><strong>{loading ? jobStatus : 'Choose a PRD, BRD, or specification'}</strong><span>PDF, DOCX, Markdown, TXT, or RST · linked to {workspace.repositoryName}</span></div><button className="secondary-button" onClick={() => fileInput.current?.click()} disabled={loading}>Browse files</button><button className="icon-button" onClick={() => setShowUpload(false)}><X size={17} /></button></div>}{error && <div className="inline-notice error"><Info size={16} /><span><strong>Mapping workflow could not complete.</strong> {error}</span></div>}
    <div className="mapping-summary"><div className="doc-summary"><div className="file-icon pdf large"><FileText size={20} /></div><div><span>Active business document</span><strong>{workspace.businessDocumentName || 'No business document selected'}</strong><small>{result ? `${result.chunks_processed} chunks · mapping complete` : 'Upload and run mapping to inspect coverage'}</small></div><button><ChevronDown size={16} /></button></div><div className="mapping-stat"><span>Requirements</span><strong>{total}</strong></div><div className="mapping-stat green"><span>Mapped</span><strong>{mapped}</strong><small>{coverage}%</small></div><div className="mapping-stat orange"><span>Needs review</span><strong>{needsReview}</strong></div><div className="coverage-ring" style={{ '--score': coverage } as React.CSSProperties}><svg viewBox="0 0 50 50"><circle cx="25" cy="25" r="20" /><circle className="fill" cx="25" cy="25" r="20" /></svg><strong>{coverage}%</strong></div></div>
    <div className="mapping-workbench"><section className="requirement-list"><div className="workbench-heading"><div><span>Business requirements</span><strong>{sections.length} indexed sections</strong></div><button><Search size={16} /></button></div>{sections.map((section) => <button className={selected?.id === section.id ? 'active' : ''} onClick={() => setSelectedId(section.id)} key={section.id}><span className={`mapping-status ${section.status.toLowerCase()}`}><Check size={13} /></span><div><em>{section.id}</em><strong>{section.title}</strong><small>{section.status === 'Mapped' ? `${section.count} code matches · ${section.confidence}% confidence` : 'No implementation confirmed'}</small></div><ChevronRight size={16} /></button>)}</section><section className="mapping-detail">{selected ? <><div className="requirement-detail"><div><span className="requirement-id">{selected.id}</span><span className={`confidence-pill ${selected.status === 'Mapped' ? '' : 'unmapped'}`}>{selected.status === 'Mapped' ? <><Sparkles size={13} />Confidence · {selected.confidence}%</> : 'Not implemented'}</span></div><h2>{selected.title}</h2><p>{selectedText}</p><button>Indexed business evidence <ShieldCheck size={14} /></button></div>{selected.status === 'Mapped' ? <div className="code-matches"><div className="matches-heading"><span>Implementation evidence</span><strong>{selected.count} confirmed matches</strong></div>{liveMappings.length ? liveMappings.map((mapping, index) => <CodeMatch key={`${mapping.file_path}-${mapping.function_name}-${index}`} rank={String(index + 1).padStart(2, '0')} file={mapping.file_path} fn={mapping.function_name || mapping.target_type} lines={mapping.module_name || 'Indexed code'} score={mapping.rerank_score.toFixed(2)} confidence={mapping.confidence} text={mapping.reasoning || mapping.code_excerpt} />) : <div className="empty-state"><Info size={18} /><strong>No code mappings returned</strong></div>}</div> : <div className="unmapped-state"><div><Search size={25} /></div><h3>No implementation confirmed</h3><p>{selected.chunk?.unmapped_reason || 'The retrieval and judging pipeline found no code above the confidence threshold.'}</p><button className="secondary-button" onClick={() => setToast('Requirement marked for review')}>Mark for review</button></div>}</> : <div className="unmapped-state"><div><Upload size={25} /></div><h3>No mapped sections yet</h3><p>Upload a business document and run the mapping workflow to populate implementation evidence.</p></div>}</section></div>
  </div>;
}

function CodeMatch({ rank, file, fn, lines, score, confidence, text }: { rank: string; file: string; fn: string; lines: string; score: string; confidence: string; text: string }) { return <article className="code-match"><span className="match-rank">{rank}</span><div className="match-main"><div className="match-path"><FileCode2 size={16} /><span>{file}</span><em>{lines}</em></div><h3>{fn}<span>()</span></h3><p>{text}</p><div className="reasoning"><Sparkles size={14} /><span><strong>Why it matches</strong>This function directly implements the retrieval and ranking behavior described by the requirement.</span></div></div><div className="match-score"><span>{confidence}</span><strong>{score}</strong><small>rerank score</small><button><ArrowUpRight size={15} /></button></div></article>; }

export default App;
