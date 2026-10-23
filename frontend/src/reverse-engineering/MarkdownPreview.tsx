import { ReactNode, isValidElement, useEffect, useId, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

let mermaidConfigured = false;

function safeUrlTransform(url: string) {
  const value = url.trim();
  if (!value || value.startsWith('#')) return value;
  try {
    const parsed = new URL(value, window.location.origin);
    return ['http:', 'https:', 'mailto:'].includes(parsed.protocol) ? value : '';
  } catch {
    return '';
  }
}

function MermaidDiagram({ chart }: { chart: string }) {
  const reactId = useId();
  const [svg, setSvg] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    const render = async () => {
      try {
        const { default: mermaid } = await import('mermaid');
        if (!mermaidConfigured) {
          mermaid.initialize({
            startOnLoad: false,
            securityLevel: 'strict',
            theme: 'neutral',
            flowchart: { htmlLabels: false },
          });
          mermaidConfigured = true;
        }
        const id = `autokt-mermaid-${reactId.replace(/[^a-zA-Z0-9_-]/g, '')}`;
        const rendered = await mermaid.render(id, chart.trim());
        if (active) {
          setSvg(rendered.svg);
          setError('');
        }
      } catch (caught) {
        if (active) {
          setSvg('');
          setError(caught instanceof Error ? caught.message : 'Diagram could not be rendered.');
        }
      }
    };
    render();
    return () => { active = false; };
  }, [chart, reactId]);

  if (error) return <div className="re-mermaid-error"><strong>Diagram source could not be rendered</strong><span>{error}</span><pre>{chart}</pre></div>;
  if (!svg) return <div className="re-mermaid-loading">Rendering diagram…</div>;
  return <div className="re-mermaid-diagram" dangerouslySetInnerHTML={{ __html: svg }} />;
}

function MarkdownPre({ children }: { children?: ReactNode }) {
  if (isValidElement(children)) {
    const properties = children.props as { className?: string; children?: ReactNode };
    if (properties.className?.split(' ').includes('language-mermaid')) {
      return <MermaidDiagram chart={String(properties.children || '')} />;
    }
  }
  return <pre>{children}</pre>;
}

export function MarkdownPreview({ markdown }: { markdown: string }) {
  return (
    <article className="re-markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={safeUrlTransform}
        components={{
          pre: MarkdownPre,
          img: () => null,
          a: ({ href, children, ...properties }) => (
            <a
              {...properties}
              href={href}
              target={href?.startsWith('http') ? '_blank' : undefined}
              rel={href?.startsWith('http') ? 'noreferrer noopener' : undefined}
            >
              {children}
            </a>
          ),
        }}
      >
        {markdown}
      </ReactMarkdown>
    </article>
  );
}
