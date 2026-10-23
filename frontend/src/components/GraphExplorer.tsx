import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ForceGraph2D from 'react-force-graph-2d';
import {
  Boxes, Check, ChevronRight, Copy, ExternalLink, Eye, EyeOff, FileCode2,
  FolderGit2, Info, Layers, Loader2, Maximize2, Minimize2, Minus, Moon,
  Network, Pause, Play, Plus, RefreshCw, RotateCcw, Search, Sparkles, Sun,
  UserRound, WandSparkles, X, Zap
} from 'lucide-react';
import {
  GraphLink, GraphNode, GraphTopologyResponse, expandGraphNode,
  fetchGraphTopology, searchGraphNodes
} from '../api';

interface GraphExplorerProps {
  repositoryId?: string;
  organizationId?: string;
  selectedModuleId?: string;
  onSelectModule?: (moduleId: string, moduleName: string) => void;
  onNavigateToSearch?: (query: string) => void;
}

const LABEL_COLORS: Record<string, { bg: string; border: string; text: string; icon: string }> = {
  Repository: { bg: '#17201d', border: '#b9f56a', text: '#f6f6ee', icon: '🏛️' },
  Module: { bg: '#5579e8', border: '#3b5bc4', text: '#ffffff', icon: '📦' },
  File: { bg: '#44833f', border: '#2f632b', text: '#ffffff', icon: '📄' },
  Function: { bg: '#ed7c4a', border: '#c75e2e', text: '#ffffff', icon: '⚡' },
  Class: { bg: '#8d6aca', border: '#6c4aa6', text: '#ffffff', icon: '🔷' },
  Person: { bg: '#c85b52', border: '#a13e36', text: '#ffffff', icon: '👤' },
  Document: { bg: '#0d9488', border: '#0a6c63', text: '#ffffff', icon: '📝' },
  Package: { bg: '#eab308', border: '#ca8a04', text: '#ffffff', icon: '📦' },
};

export const GraphExplorer: React.FC<GraphExplorerProps> = ({
  repositoryId,
  organizationId,
  selectedModuleId,
  onSelectModule,
  onNavigateToSearch,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const fgRef = useRef<any>(null);

  const [dimensions, setDimensions] = useState({ width: 900, height: 600 });
  const [loading, setLoading] = useState(false);
  const [expandingNodeId, setExpandingNodeId] = useState<string | null>(null);
  const [graphData, setGraphData] = useState<{ nodes: GraphNode[]; links: GraphLink[] }>({ nodes: [], links: [] });

  // Filters & Settings
  const [includeFunctions, setIncludeFunctions] = useState(false);
  const [activeFilters, setActiveFilters] = useState<Record<string, boolean>>({
    Repository: true,
    Module: true,
    File: true,
    Function: true,
    Class: true,
    Person: true,
    Document: true,
  });
  const [isDarkMode, setIsDarkMode] = useState(false);
  const [isPhysicsActive, setIsPhysicsActive] = useState(true);

  // Search & Selection
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [hoveredNode, setHoveredNode] = useState<GraphNode | null>(null);
  const [toastMessage, setToastMessage] = useState<string | null>(null);

  // Resize Observer for auto responsive sizing
  useEffect(() => {
    if (!containerRef.current) return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        if (width > 0 && height > 0) {
          setDimensions({ width, height });
        }
      }
    });
    observer.observe(containerRef.current);
    return () => observer.disconnect();
  }, []);

  const showToast = (msg: string) => {
    setToastMessage(msg);
    setTimeout(() => setToastMessage(null), 2500);
  };

  // Load Graph Topology
  const loadTopology = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchGraphTopology({
        repositoryId,
        organizationId,
        moduleId: selectedModuleId,
        includeFunctions,
        limit: 220,
      });

      setGraphData({
        nodes: res.nodes || [],
        links: res.links || [],
      });

      // Fit to view after nodes settle
      setTimeout(() => {
        if (fgRef.current) {
          fgRef.current.zoomToFit(400, 50);
        }
      }, 500);
    } catch (err: any) {
      showToast(err.message || 'Failed to load graph topology');
    } finally {
      setLoading(false);
    }
  }, [repositoryId, organizationId, selectedModuleId, includeFunctions]);

  useEffect(() => {
    loadTopology();
  }, [loadTopology]);

  // Node Expansion (1-hop neighbor fetching)
  const handleExpandNode = async (node: GraphNode) => {
    if (expandingNodeId) return;
    setExpandingNodeId(node.id);
    try {
      const res = await expandGraphNode(node.id, organizationId, 40);
      if (!res.nodes || res.nodes.length === 0) {
        showToast('No additional connected neighbors found');
        return;
      }

      setGraphData((prev) => {
        const existingNodeIds = new Set(prev.nodes.map((n) => n.id));
        const newNodes = res.nodes.filter((n) => !existingNodeIds.has(n.id));

        const existingLinkKeys = new Set(
          prev.links.map((l) => {
            const s = typeof l.source === 'object' ? (l.source as any).id : l.source;
            const t = typeof l.target === 'object' ? (l.target as any).id : l.target;
            return `${s}->${l.type}->${t}`;
          })
        );

        const newLinks = res.links.filter((l) => {
          const s = typeof l.source === 'object' ? (l.source as any).id : l.source;
          const t = typeof l.target === 'object' ? (l.target as any).id : l.target;
          return !existingLinkKeys.has(`${s}->${l.type}->${t}`);
        });

        showToast(`Expanded +${newNodes.length} nodes & +${newLinks.length} connections`);

        return {
          nodes: [...prev.nodes, ...newNodes],
          links: [...prev.links, ...newLinks],
        };
      });
    } catch (err: any) {
      showToast('Expansion failed: ' + (err.message || 'Unknown error'));
    } finally {
      setExpandingNodeId(null);
    }
  };

  // Node Search
  useEffect(() => {
    if (!searchQuery.trim() || searchQuery.length < 2) {
      setSearchResults([]);
      return;
    }

    const timer = setTimeout(async () => {
      setIsSearching(true);
      try {
        const res = await searchGraphNodes(searchQuery.trim(), organizationId, 8);
        setSearchResults(res.results || []);
      } catch {
        setSearchResults([]);
      } finally {
        setIsSearching(false);
      }
    }, 280);

    return () => clearTimeout(timer);
  }, [searchQuery, organizationId]);

  // Focus a specific node
  const focusNode = useCallback((node: GraphNode) => {
    setSelectedNode(node);
    if (fgRef.current && typeof node.x === 'number' && typeof node.y === 'number') {
      fgRef.current.centerAt(node.x, node.y, 800);
      fgRef.current.zoom(2.2, 800);
    }
  }, []);

  // Filtered dataset
  const filteredData = useMemo(() => {
    const visibleNodes = graphData.nodes.filter((node) => activeFilters[node.label] ?? true);
    const visibleNodeIds = new Set(visibleNodes.map((n) => n.id));

    const visibleLinks = graphData.links.filter((link) => {
      const srcId = typeof link.source === 'object' ? (link.source as any).id : link.source;
      const tgtId = typeof link.target === 'object' ? (link.target as any).id : link.target;
      return visibleNodeIds.has(srcId) && visibleNodeIds.has(tgtId);
    });

    return { nodes: visibleNodes, links: visibleLinks };
  }, [graphData, activeFilters]);

  // Connected nodes map for hover highlighting
  const neighborsMap = useMemo(() => {
    const map = new Map<string, Set<string>>();
    filteredData.links.forEach((link) => {
      const srcId = typeof link.source === 'object' ? (link.source as any).id : link.source;
      const tgtId = typeof link.target === 'object' ? (link.target as any).id : link.target;
      if (!map.has(srcId)) map.set(srcId, new Set());
      if (!map.has(tgtId)) map.set(tgtId, new Set());
      map.get(srcId)!.add(tgtId);
      map.get(tgtId)!.add(srcId);
    });
    return map;
  }, [filteredData]);

  // Label counts
  const labelCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    graphData.nodes.forEach((n) => {
      counts[n.label] = (counts[n.label] || 0) + 1;
    });
    return counts;
  }, [graphData.nodes]);

  // Custom 2D Canvas Node Renderer
  const drawNode = useCallback(
    (node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const isSelected = selectedNode?.id === node.id;
      const isHovered = hoveredNode?.id === node.id;
      const isConnected =
        hoveredNode &&
        (hoveredNode.id === node.id || neighborsMap.get(hoveredNode.id)?.has(node.id));

      const radius = Math.max(4, (node.val || 10) * 0.7);
      const style = LABEL_COLORS[node.label] || { bg: '#5579e8', border: '#3b5bc4', text: '#ffffff' };

      // Fade unconnected nodes when hovering
      ctx.save();
      if (hoveredNode && !isConnected) {
        ctx.globalAlpha = 0.22;
      }

      // Outer Glow Halo for Selected / Hovered
      if (isSelected || isHovered) {
        ctx.beginPath();
        ctx.arc(node.x, node.y, radius + 5 / Math.sqrt(globalScale), 0, 2 * Math.PI, false);
        ctx.fillStyle = isSelected ? 'rgba(185, 245, 106, 0.45)' : 'rgba(85, 121, 232, 0.35)';
        ctx.fill();
      }

      // Main Circle
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI, false);
      ctx.fillStyle = isDarkMode && node.label === 'Repository' ? '#2e3d36' : style.bg;
      ctx.fill();

      // Border Ring
      ctx.lineWidth = isSelected ? 2.5 / globalScale : 1.5 / globalScale;
      ctx.strokeStyle = isSelected ? '#b9f56a' : isDarkMode ? '#52665e' : '#deded5';
      ctx.stroke();

      // Draw Node Label text (visible when zoomed in or hovered/selected)
      if (globalScale > 0.85 || isSelected || isHovered || node.label === 'Module' || node.label === 'Repository') {
        const label = node.name || node.id;
        const fontSize = Math.max(9, Math.min(14, 11 / Math.sqrt(globalScale)));
        ctx.font = `${isSelected ? 'bold ' : ''}${fontSize}px "Segoe UI", Inter, sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';

        const textY = node.y + radius + 3;
        const textWidth = ctx.measureText(label).width;

        // Label pill background
        ctx.fillStyle = isDarkMode ? 'rgba(23, 32, 29, 0.85)' : 'rgba(255, 254, 249, 0.9)';
        ctx.fillRect(node.x - textWidth / 2 - 4, textY - 1, textWidth + 8, fontSize + 3);

        ctx.fillStyle = isDarkMode ? '#f6f6ee' : '#17201d';
        ctx.fillText(label, node.x, textY);
      }

      ctx.restore();
    },
    [selectedNode, hoveredNode, neighborsMap, isDarkMode]
  );

  return (
    <div className="surface graph-explorer-card" style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 170px)', minHeight: '620px', position: 'relative', overflow: 'hidden' }}>
      
      {/* ── TOP TOOLBAR ────────────────────────────────────────── */}
      <div style={{
        padding: '12px 18px',
        borderBottom: `1px solid ${isDarkMode ? '#2d3b35' : 'var(--line)'}`,
        background: isDarkMode ? '#17201d' : 'var(--surface)',
        display: 'flex',
        flexWrap: 'wrap',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: '12px',
        zIndex: 20
      }}>
        {/* Left: Search & Quick Scope */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flex: '1 1 320px', maxWidth: '520px', position: 'relative' }}>
          <div className="input-shell" style={{ width: '100%', minHeight: '36px', height: '36px', background: isDarkMode ? '#202b27' : '#fafaf5' }}>
            <Search size={14} style={{ color: '#8b938f' }} />
            <input
              type="text"
              placeholder="Search files, functions, or modules in graph..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              style={{ color: isDarkMode ? '#f6f6ee' : '#26302c', fontSize: '12px' }}
            />
            {isSearching && <Loader2 size={13} className="animate-spin" style={{ color: '#5579e8' }} />}
            {searchQuery && (
              <button onClick={() => setSearchQuery('')} style={{ background: 'none', border: 0, padding: 0, color: '#8b938f' }}>
                <X size={13} />
              </button>
            )}
          </div>

          {/* Search Dropdown */}
          {searchResults.length > 0 && (
            <div style={{
              position: 'absolute',
              top: '42px',
              left: 0,
              right: 0,
              background: isDarkMode ? '#202b27' : '#fffef9',
              border: `1px solid ${isDarkMode ? '#34453e' : '#dcded5'}`,
              borderRadius: '9px',
              boxShadow: '0 12px 30px rgba(0,0,0,0.18)',
              zIndex: 50,
              overflow: 'hidden'
            }}>
              {searchResults.map((item) => (
                <div
                  key={item.id}
                  onClick={() => {
                    setSearchQuery('');
                    setSearchResults([]);
                    // Find node in graph or expand it
                    const existing = graphData.nodes.find((n) => n.id === item.id);
                    if (existing) {
                      focusNode(existing);
                    } else {
                      handleExpandNode({ id: item.id, label: item.label, name: item.name, val: 12, color: item.color });
                    }
                  }}
                  style={{
                    padding: '8px 12px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '9px',
                    cursor: 'pointer',
                    fontSize: '11px',
                    borderBottom: `1px solid ${isDarkMode ? '#283731' : '#f0f0e9'}`,
                    transition: 'background 0.15s'
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = isDarkMode ? '#2a3a34' : '#f4f4ee')}
                  onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                >
                  <span style={{
                    fontSize: '9px',
                    fontWeight: 700,
                    padding: '2px 5px',
                    borderRadius: '4px',
                    background: item.color,
                    color: '#fff'
                  }}>
                    {item.badge || item.label}
                  </span>
                  <strong style={{ color: isDarkMode ? '#f6f6ee' : '#17201d' }}>{item.name}</strong>
                  {item.path && <span style={{ color: '#8b938f', fontSize: '10px', marginLeft: 'auto', maxWidth: '180px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.path}</span>}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Right: Fine-grain Toggle & Reload */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '11px', fontWeight: 600, color: isDarkMode ? '#c8d4ce' : '#57635d', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={includeFunctions}
              onChange={(e) => setIncludeFunctions(e.target.checked)}
              style={{ accentColor: 'var(--green-deep)' }}
            />
            Include Call Graph & Functions
          </label>

          <button
            onClick={loadTopology}
            className="secondary-button compact"
            title="Reload topology"
            style={{ height: '34px' }}
          >
            <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
            Reset View
          </button>
        </div>
      </div>

      {/* ── FILTER PILLS BAR ────────────────────────────────────── */}
      <div style={{
        padding: '7px 18px',
        background: isDarkMode ? '#1a2420' : '#f8f8f2',
        borderBottom: `1px solid ${isDarkMode ? '#283630' : 'var(--line)'}`,
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        overflowX: 'auto',
        fontSize: '11px'
      }}>
        <span style={{ fontSize: '10px', fontWeight: 800, textTransform: 'uppercase', letterSpacing: '0.08em', color: '#88948e' }}>
          Filters:
        </span>
        {Object.entries(LABEL_COLORS).map(([label, style]) => {
          const count = labelCounts[label] || 0;
          if (count === 0 && label !== 'Module' && label !== 'File') return null;
          const active = activeFilters[label] ?? true;

          return (
            <button
              key={label}
              onClick={() => setActiveFilters((prev) => ({ ...prev, [label]: !active }))}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '5px',
                padding: '3px 8px',
                borderRadius: '999px',
                fontSize: '10px',
                fontWeight: 700,
                border: `1px solid ${active ? style.border : isDarkMode ? '#35473f' : '#deded5'}`,
                background: active ? (isDarkMode ? '#22302a' : '#ffffff') : 'transparent',
                color: active ? (isDarkMode ? '#f6f6ee' : '#17201d') : '#88948e',
                opacity: active ? 1 : 0.6,
                cursor: 'pointer',
                transition: 'all 0.15s'
              }}
            >
              <span style={{ width: '7px', height: '7px', borderRadius: '50%', background: style.bg }} />
              {label} ({count})
            </button>
          );
        })}

        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: '8px', fontSize: '10px', color: '#88948e' }}>
          <span>{filteredData.nodes.length} nodes</span>
          <span>•</span>
          <span>{filteredData.links.length} edges</span>
        </div>
      </div>

      {/* ── GRAPH CANVAS CONTAINER ─────────────────────────────── */}
      <div
        ref={containerRef}
        style={{
          flex: 1,
          position: 'relative',
          background: isDarkMode ? '#17201d' : '#fcfcf8',
          overflow: 'hidden'
        }}
      >
        {loading && (
          <div style={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            background: isDarkMode ? 'rgba(23, 32, 29, 0.75)' : 'rgba(255, 254, 249, 0.75)',
            backdropFilter: 'blur(3px)',
            zIndex: 30
          }}>
            <Loader2 size={32} className="animate-spin" style={{ color: '#5579e8', marginBottom: '8px' }} />
            <span style={{ fontSize: '12px', fontWeight: 600, color: isDarkMode ? '#e0e7e3' : '#33403a' }}>
              Querying Neo4j Knowledge Graph...
            </span>
          </div>
        )}

        {/* Force Graph 2D Canvas */}
        <ForceGraph2D
          ref={fgRef}
          width={dimensions.width}
          height={dimensions.height}
          graphData={filteredData}
          nodeLabel={(n: any) => `${n.badge || n.label}: ${n.name}${n.path ? `\n(${n.path})` : ''}`}
          nodeCanvasObject={drawNode}
          nodePointerAreaPaint={(node: any, color: string, ctx: CanvasRenderingContext2D) => {
            ctx.fillStyle = color;
            ctx.beginPath();
            ctx.arc(node.x, node.y, (node.val || 10) + 4, 0, 2 * Math.PI, false);
            ctx.fill();
          }}
          linkColor={() => (isDarkMode ? 'rgba(150, 170, 160, 0.28)' : 'rgba(120, 135, 125, 0.35)')}
          linkWidth={(link: any) => (link.type === 'CALLS' || link.type === 'IMPORTS' ? 1.6 : 1.1)}
          linkDirectionalArrowLength={(link: any) => (link.type === 'CALLS' || link.type === 'IMPORTS' ? 4.5 : 0)}
          linkDirectionalArrowRelPos={1}
          linkDirectionalParticles={(link: any) => (link.type === 'CALLS' ? 2 : 0)}
          linkDirectionalParticleSpeed={0.006}
          linkDirectionalParticleWidth={2}
          linkDirectionalParticleColor={() => '#5579e8'}
          onNodeClick={(node: any) => focusNode(node)}
          onNodeHover={(node: any) => setHoveredNode(node || null)}
          onNodeDragEnd={(node: any) => {
            // Pin node position
            node.fx = node.x;
            node.fy = node.y;
          }}
          cooldownTicks={isPhysicsActive ? 160 : 0}
          onEngineStop={() => {
            // physics settled
          }}
        />

        {/* ── FLOATING HUD TOOLBOX (Bottom-Left) ────────────────── */}
        <div style={{
          position: 'absolute',
          bottom: '18px',
          left: '18px',
          background: isDarkMode ? 'rgba(23, 32, 29, 0.9)' : 'rgba(255, 254, 249, 0.92)',
          border: `1px solid ${isDarkMode ? '#34453e' : '#deded5'}`,
          borderRadius: '10px',
          padding: '4px',
          display: 'flex',
          alignItems: 'center',
          gap: '4px',
          boxShadow: '0 8px 24px rgba(0,0,0,0.12)',
          backdropFilter: 'blur(8px)',
          zIndex: 25
        }}>
          <button
            className="icon-button"
            style={{ width: '30px', height: '30px' }}
            title="Zoom In"
            onClick={() => fgRef.current && fgRef.current.zoom(fgRef.current.zoom() * 1.35, 300)}
          >
            <Plus size={14} />
          </button>
          <button
            className="icon-button"
            style={{ width: '30px', height: '30px' }}
            title="Zoom Out"
            onClick={() => fgRef.current && fgRef.current.zoom(fgRef.current.zoom() * 0.72, 300)}
          >
            <Minus size={14} />
          </button>
          <button
            className="icon-button"
            style={{ width: '30px', height: '30px' }}
            title="Fit to Screen"
            onClick={() => fgRef.current && fgRef.current.zoomToFit(400, 50)}
          >
            <Maximize2 size={13} />
          </button>

          <div style={{ width: '1px', height: '18px', background: isDarkMode ? '#35473f' : '#deded5', margin: '0 2px' }} />

          <button
            className="icon-button"
            style={{ width: '30px', height: '30px' }}
            title={isPhysicsActive ? 'Freeze physics' : 'Resume physics'}
            onClick={() => {
              setIsPhysicsActive(!isPhysicsActive);
              if (!isPhysicsActive && fgRef.current) {
                fgRef.current.d3ReheatSimulation();
              }
            }}
          >
            {isPhysicsActive ? <Pause size={13} /> : <Play size={13} />}
          </button>

          <button
            className="icon-button"
            style={{ width: '30px', height: '30px' }}
            title={isDarkMode ? 'Light Canvas Theme' : 'Dark Studio Theme'}
            onClick={() => setIsDarkMode(!isDarkMode)}
          >
            {isDarkMode ? <Sun size={13} /> : <Moon size={13} />}
          </button>
        </div>

        {/* ── NODE INSPECTOR DRAWER (Right Slide-Over) ─────────── */}
        {selectedNode && (
          <div style={{
            position: 'absolute',
            top: 0,
            right: 0,
            bottom: 0,
            width: '340px',
            background: isDarkMode ? '#1e2925' : '#fffef9',
            borderLeft: `1px solid ${isDarkMode ? '#33423c' : 'var(--line)'}`,
            boxShadow: '-10px 0 35px rgba(0,0,0,0.1)',
            zIndex: 35,
            display: 'flex',
            flexDirection: 'column',
            overflowY: 'auto',
            animation: 'slide-left 0.22s ease'
          }}>
            {/* Drawer Header */}
            <div style={{
              padding: '16px 18px',
              borderBottom: `1px solid ${isDarkMode ? '#2d3b35' : 'var(--line)'}`,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between'
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <span style={{
                  padding: '3px 7px',
                  borderRadius: '5px',
                  fontSize: '10px',
                  fontWeight: 800,
                  background: selectedNode.color,
                  color: '#ffffff'
                }}>
                  {selectedNode.badge || selectedNode.label}
                </span>
                <span style={{ fontSize: '11px', color: '#88948e' }}>Node Inspector</span>
              </div>
              <button
                onClick={() => setSelectedNode(null)}
                style={{ background: 'none', border: 0, padding: 0, cursor: 'pointer', color: '#88948e' }}
              >
                <X size={16} />
              </button>
            </div>

            {/* Node Title & Path */}
            <div style={{ padding: '18px 18px 14px' }}>
              <h3 style={{ margin: 0, fontSize: '16px', color: isDarkMode ? '#f6f6ee' : '#17201d', wordBreak: 'break-all' }}>
                {selectedNode.name}
              </h3>
              {selectedNode.path && (
                <div style={{
                  marginTop: '6px',
                  padding: '5px 8px',
                  background: isDarkMode ? '#17201d' : '#f5f5ef',
                  borderRadius: '6px',
                  fontSize: '11px',
                  fontFamily: 'monospace',
                  color: isDarkMode ? '#9db0a7' : '#57635d',
                  wordBreak: 'break-all'
                }}>
                  {selectedNode.path}
                </div>
              )}
            </div>

            {/* Action Buttons */}
            <div style={{ padding: '0 18px 16px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
              <button
                className="primary-button success-button"
                style={{ width: '100%', minHeight: '34px', fontSize: '11px' }}
                onClick={() => handleExpandNode(selectedNode)}
                disabled={expandingNodeId === selectedNode.id}
              >
                {expandingNodeId === selectedNode.id ? (
                  <>
                    <Loader2 size={13} className="animate-spin" /> Expanding...
                  </>
                ) : (
                  <>
                    <Sparkles size={13} /> Expand 1-Hop Neighbors
                  </>
                )}
              </button>

              {selectedNode.label === 'Module' && onSelectModule && (
                <button
                  className="secondary-button"
                  style={{ width: '100%', minHeight: '32px', fontSize: '11px' }}
                  onClick={() => onSelectModule(selectedNode.id, selectedNode.name)}
                >
                  <Boxes size={13} /> Open in Module Intelligence
                </button>
              )}

              {onNavigateToSearch && (
                <button
                  className="secondary-button"
                  style={{ width: '100%', minHeight: '32px', fontSize: '11px' }}
                  onClick={() => onNavigateToSearch(selectedNode.name)}
                >
                  <Search size={13} /> Search related knowledge
                </button>
              )}
            </div>

            {/* Properties & Metrics */}
            <div style={{ padding: '0 18px 20px', flex: 1 }}>
              <div style={{ fontSize: '10px', fontWeight: 800, textTransform: 'uppercase', letterSpacing: '0.08em', color: '#88948e', marginBottom: '8px' }}>
                Properties & Metrics
              </div>
              <div style={{
                background: isDarkMode ? '#17201d' : '#fafaf5',
                border: `1px solid ${isDarkMode ? '#283630' : '#e6e6de'}`,
                borderRadius: '8px',
                padding: '10px',
                display: 'flex',
                flexDirection: 'column',
                gap: '7px',
                fontSize: '11px'
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <span style={{ color: '#88948e' }}>Entity Type:</span>
                  <strong>{selectedNode.label}</strong>
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <span style={{ color: '#88948e' }}>Graph Identifier:</span>
                  <span style={{ fontFamily: 'monospace', fontSize: '10px', maxWidth: '170px', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {selectedNode.id}
                  </span>
                </div>
                {selectedNode.properties &&
                  Object.entries(selectedNode.properties).map(([key, val]) => {
                    if (typeof val === 'object' || val === null || val === undefined || key === 'id' || key === 'name' || key === 'path') {
                      return null;
                    }
                    return (
                      <div key={key} style={{ display: 'flex', justifyContent: 'space-between', gap: '8px' }}>
                        <span style={{ color: '#88948e', textTransform: 'capitalize' }}>{key.replace(/_/g, ' ')}:</span>
                        <span style={{ fontWeight: 600, maxWidth: '170px', overflow: 'hidden', textOverflow: 'ellipsis', textAlign: 'right' }}>
                          {String(val)}
                        </span>
                      </div>
                    );
                  })}
              </div>

              {/* Connected Relationships List */}
              <div style={{ marginTop: '16px' }}>
                <div style={{ fontSize: '10px', fontWeight: 800, textTransform: 'uppercase', letterSpacing: '0.08em', color: '#88948e', marginBottom: '8px' }}>
                  Immediate Connections ({neighborsMap.get(selectedNode.id)?.size || 0})
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '5px', maxHeight: '180px', overflowY: 'auto' }}>
                  {filteredData.links
                    .filter((l) => {
                      const s = typeof l.source === 'object' ? (l.source as any).id : l.source;
                      const t = typeof l.target === 'object' ? (l.target as any).id : l.target;
                      return s === selectedNode.id || t === selectedNode.id;
                    })
                    .slice(0, 15)
                    .map((link, idx) => {
                      const s = typeof link.source === 'object' ? (link.source as any).id : link.source;
                      const t = typeof link.target === 'object' ? (link.target as any).id : link.target;
                      const otherId = s === selectedNode.id ? t : s;
                      const otherNode = graphData.nodes.find((n) => n.id === otherId);
                      const isOutbound = s === selectedNode.id;

                      return (
                        <div
                          key={idx}
                          onClick={() => otherNode && focusNode(otherNode)}
                          style={{
                            padding: '6px 8px',
                            background: isDarkMode ? '#1a2420' : '#f5f5ef',
                            borderRadius: '6px',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'space-between',
                            fontSize: '11px',
                            cursor: 'pointer'
                          }}
                        >
                          <span style={{ color: '#88948e', fontSize: '9px', fontWeight: 700 }}>
                            {isOutbound ? '➔' : '⬅'} {link.label}
                          </span>
                          <span style={{ fontWeight: 600, color: otherNode?.color || '#5579e8' }}>
                            {otherNode?.name || otherId}
                          </span>
                        </div>
                      );
                    })}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Toast Notification */}
      {toastMessage && (
        <div className="toast" style={{ bottom: '15px', right: '15px', zIndex: 60 }}>
          <Info size={14} style={{ color: 'var(--green)' }} />
          <span>{toastMessage}</span>
        </div>
      )}
    </div>
  );
};
export default GraphExplorer;
