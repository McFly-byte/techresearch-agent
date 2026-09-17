import React, { useEffect, useMemo, useState } from 'react';

type TaskSummary = {
  task_id: string;
  query: string;
  status: string;
  created_at: number;
  updated_at: number;
  error: string;
};

type TaskDetail = TaskSummary & {
  user_context: string;
  research_depth: string;
  n_events: number;
};

type ReportResponse = {
  task_id: string;
  markdown: string;
  html: string;
  status: string;
};

type EventEnvelope = {
  event_id: string;
  event_type: string;
  task_id: string;
  timestamp: number;
  stage: string;
  data: Record<string, unknown>;
  schema_version: string;
};

function useHashRoute(): [string, (h: string) => void] {
  // Only hashes starting with "/" are app routes (e.g. #/report/task_xxx).
  // In-page anchors like #c_task_1_1 must NOT trigger a route change,
  // otherwise clicking a citation wipes the report page.
  const routeHash = () => {
    const h = window.location.hash.slice(1);
    return h.startsWith('/') ? h : '/';
  };
  const [hash, setHash] = useState(routeHash);
  useEffect(() => {
    const onChange = () => {
      const h = window.location.hash.slice(1);
      if (h.startsWith('/')) setHash(h);
      // non-route hash (in-page anchor): keep current route unchanged
    };
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  return [hash, (h: string) => { window.location.hash = h; }];
}

async function postJson(url: string, body: unknown) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function Nav({ go }: { go: (h: string) => void }) {
  return (
    <nav style={{ display: 'flex', gap: '1rem', padding: '0.5rem 2rem', background: '#f0f0f0' }}>
      <a href="#/" onClick={(e) => { e.preventDefault(); go('/'); }}>Home</a>
      <a href="#/history" onClick={(e) => { e.preventDefault(); go('/history'); }}>History</a>
    </nav>
  );
}

function HomePage({ go }: { go: (h: string) => void }) {
  const [query, setQuery] = useState('LangGraph vs LlamaIndex');
  const [context, setContext] = useState('');
  const [depth, setDepth] = useState('standard');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function start() {
    setBusy(true); setError(null);
    try {
      const t = await postJson('/api/research', { query, user_context: context, research_depth: depth });
      go(`/progress/${t.task_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ padding: '2rem', maxWidth: 600 }}>
      <h2>New Research Task</h2>
      <label>Question</label>
      <input value={query} onChange={(e) => setQuery(e.target.value)} style={{ width: '100%', padding: 8, marginBottom: 12 }} />
      <label>Background (optional)</label>
      <textarea value={context} onChange={(e) => setContext(e.target.value)} style={{ width: '100%', padding: 8, marginBottom: 12 }} rows={3} />
      <label>Depth</label>
      <select value={depth} onChange={(e) => setDepth(e.target.value)} style={{ display: 'block', marginBottom: 12 }}>
        <option value="quick">quick</option>
        <option value="standard">standard</option>
        <option value="deep">deep</option>
      </select>
      <button onClick={start} disabled={busy} style={{ padding: '0.5rem 1.5rem' }}>
        {busy ? 'Starting...' : 'Start Research'}
      </button>
      {error && <p style={{ color: 'crimson' }}>{error}</p>}
    </div>
  );
}

function ProgressPage({ taskId, go }: { taskId: string; go: (h: string) => void }) {
  const [events, setEvents] = useState<EventEnvelope[]>([]);
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [showEvents, setShowEvents] = useState(true);
  const [cancelling, setCancelling] = useState(false);
  const seenIds = useMemo(() => new Set<string>(), []);

  async function cancelTask() {
    setCancelling(true);
    try {
      await postJson(`/api/research/${taskId}/cancel`, {});
    } catch { /* ignore — status will refresh */ }
    setCancelling(false);
  }

  useEffect(() => {
    let es: EventSource | null = null;
    let closed = false;
    async function init() {
      const t = await getJson<TaskDetail>(`/api/research/${taskId}`);
      setTask(t);
      es = new EventSource(`/api/research/${taskId}/stream`);
      es.onmessage = () => {};
      const handler = (ev: MessageEvent) => {
        try {
          const parsed: EventEnvelope = JSON.parse(ev.data);
          if (seenIds.has(parsed.event_id)) return;
          seenIds.add(parsed.event_id);
          setEvents((prev) => [...prev, parsed]);
          if (parsed.event_type === 'done' || parsed.event_type === 'error' || parsed.event_type === 'cancelled') {
            if (!closed) {
              closed = true;
              es?.close();
              go(`/report/${taskId}`);
            }
          }
        } catch { /* ignore malformed */ }
      };
      es.addEventListener('stage', handler);
      es.addEventListener('done', handler);
      es.addEventListener('error', handler);
      es.addEventListener('cancelled', handler);
    }
    init();
    return () => { es?.close(); };
  }, [taskId, go, seenIds]);

  // Derive subtask status from worker_done events.
  const subtasks = useMemo(() => {
    const m = new Map<string, { status: string; stage: string }>();
    for (const e of events) {
      if (e.stage === 'worker_done' && e.data.task_id) {
        m.set(String(e.data.task_id), {
          status: String(e.data.status || 'completed'),
          stage: 'worker_done',
        });
      }
    }
    return Array.from(m.entries());
  }, [events]);

  // Overall progress: planner_start -> worker_done(s) -> verify_done -> write_done -> done.
  const stages = ['planner_start', 'worker_done', 'verify_done', 'write_done', 'done'];
  const reached = stages.filter((s) => events.some((e) => e.stage === s));
  const pct = Math.round((reached.length / stages.length) * 100);

  return (
    <div style={{ padding: '2rem' }}>
      <h2>Progress: {taskId}</h2>
      {task && <p>status: {task.status} · events: {task.n_events}</p>}

      {/* Cancel button — only meaningful while running/queued */}
      {task && task.status in {'running': 1, 'queued': 1} && (
        <button
          onClick={cancelTask}
          disabled={cancelling}
          style={{ marginBottom: '1rem', padding: '0.4rem 1rem', color: 'crimson' }}
        >
          {cancelling ? 'Cancelling...' : 'Cancel Task'}
        </button>
      )}

      {/* Progress bar */}
      <div style={{ background: '#eee', height: 12, borderRadius: 6, margin: '1rem 0' }}>
        <div data-testid="progress-bar" style={{ width: `${pct}%`, background: '#4a90d9', height: 12, borderRadius: 6 }} />
      </div>

      {/* Task tree / worker cards */}
      <h3>Subtasks</h3>
      {subtasks.length === 0 ? (
        <p style={{ color: '#888' }}>No subtask events yet (planner running...)</p>
      ) : (
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {subtasks.map(([tid, info]) => (
            <div
              key={tid}
              data-testid="worker-card"
              data-task-id={tid}
              data-status={info.status}
              style={{
                border: '1px solid #ccc',
                borderRadius: 6,
                padding: 8,
                minWidth: 160,
                background: info.status === 'completed' ? '#e8f5e9' : '#fff3e0',
              }}
            >
              <div><strong>{tid}</strong></div>
              <div>status: {info.status}</div>
            </div>
          ))}
        </div>
      )}

      {/* Collapsible event stream */}
      <div style={{ marginTop: '1rem' }}>
        <button onClick={() => setShowEvents((v) => !v)}>
          {showEvents ? '▼ Hide' : '▶ Show'} event stream ({events.length})
        </button>
        {showEvents && (
          <pre style={{ background: '#f4f4f4', padding: '1rem', maxHeight: 300, overflow: 'auto' }}>
            {events.map((e) => `${new Date(e.timestamp * 1000).toLocaleTimeString()} [${e.stage}] ${e.event_type}`).join('\n')}
          </pre>
        )}
      </div>
    </div>
  );
}

function ReportPage({ taskId }: { taskId: string }) {
  const [report, setReport] = useState<ReportResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [feishuMsg, setFeishuMsg] = useState<string | null>(null);
  const [feishuBusy, setFeishuBusy] = useState(false);

  useEffect(() => {
    getJson<ReportResponse>(`/api/research/${taskId}/report`)
      .then(setReport)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false));
  }, [taskId]);

  function downloadMarkdown() {
    if (!report) return;
    const blob = new Blob([report.markdown], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `research-${taskId}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  async function exportFeishu() {
    setFeishuBusy(true);
    setFeishuMsg(null);
    try {
      const r = await postJson(`/api/research/${taskId}/export/feishu`, {});
      if (r.exported) {
        setFeishuMsg(`Exported: ${r.doc_url}`);
      } else {
        setFeishuMsg(`Export failed: ${r.reason}`);
      }
    } catch (e) {
      setFeishuMsg(`Export error: ${e instanceof Error ? e.message : String(e)}`);
    }
    setFeishuBusy(false);
  }

  if (busy) return <div style={{ padding: '2rem' }}>Loading...</div>;
  if (error) return <div style={{ padding: '2rem', color: 'crimson' }}>{error}</div>;
  if (!report) return null;

  return (
    <div style={{ padding: '2rem', maxWidth: 800 }}>
      <h2>Report</h2>
      <p>status: {report.status}</p>
      <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '1rem' }}>
        <button onClick={downloadMarkdown} style={{ padding: '0.4rem 1rem' }}>
          ⬇ Download Markdown
        </button>
        <button onClick={exportFeishu} disabled={feishuBusy} style={{ padding: '0.4rem 1rem' }}>
          {feishuBusy ? 'Exporting...' : '📤 Export to Feishu'}
        </button>
      </div>
      {feishuMsg && (
        <p style={{ color: feishuMsg.startsWith('Exported') ? 'green' : 'crimson', fontSize: 14 }}>
          {feishuMsg}
        </p>
      )}
      <SafeMarkdown source={report.markdown} />
    </div>
  );
}

/** Minimal, safe markdown renderer.
 * - No dangerouslySetInnerHTML.
 * - Escapes all HTML by rendering strings as React children.
 * - Turns [c1], [c2] ... into clickable anchors pointing to the citation
 *   list (we render them as <a href="#c1">).
 * - Renders # / ## headings, bullet lists, paragraphs.
 */
export function SafeMarkdown({ source }: { source: string }) {
  const lines = source.split('\n');
  const out: React.ReactNode[] = [];
  const list: string[] = [];
  const flushList = () => {
    if (list.length) {
      out.push(<ul key={`ul-${out.length}`}>{list.map((it, i) => <li key={i}>{renderInline(it)}</li>)}</ul>);
      list.length = 0;
    }
  };
  lines.forEach((line, i) => {
    if (line.startsWith('## ')) {
      flushList();
      out.push(<h3 key={i}>{line.slice(3)}</h3>);
    } else if (line.startsWith('# ')) {
      flushList();
      out.push(<h2 key={i}>{line.slice(2)}</h2>);
    } else if (line.startsWith('- ')) {
      list.push(line.slice(2));
    } else if (line.trim() === '') {
      flushList();
    } else {
      flushList();
      out.push(<p key={i}>{renderInline(line)}</p>);
    }
  });
  flushList();
  return <div className="report-body">{out}</div>;
}

function renderInline(text: string): React.ReactNode {
  // Turn [cN] or [c_task_1_1] into <a href="#id">citation</a>.
  // Citation IDs may contain letters, digits, underscores, hyphens.
  const parts = text.split(/(\[c[\w-]+\])/g);
  return parts.map((p, i) => {
    const m = p.match(/^\[(c[\w-]+)\]$/);
    if (m) {
      return (
        <a key={i} id={m[1]} href={`#${m[1]}`} style={{ color: '#1a73e8' }}>
          [{m[1]}]
        </a>
      );
    }
    return <span key={i}>{p}</span>;
  });
}

function HistoryPage({ go }: { go: (h: string) => void }) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  useEffect(() => {
    getJson<TaskSummary[]>('/api/tasks').then(setTasks).catch(() => {});
  }, []);
  return (
    <div style={{ padding: '2rem' }}>
      <h2>History</h2>
      <ul>
        {tasks.map((t) => (
          <li key={t.task_id}>
            <a href={`#/report/${t.task_id}`} onClick={(e) => { e.preventDefault(); go(`/report/${t.task_id}`); }}>
              {t.task_id}
            </a>
            {' '}— {t.status} — {t.query.slice(0, 60)}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function App() {
  const [hash, go] = useHashRoute();
  const parts = hash.split('/').filter(Boolean);
  const page = parts[0] || '';
  const taskId = parts[1] || '';

  return (
    <div>
      <Nav go={go} />
      {page === '' && <HomePage go={go} />}
      {page === 'progress' && taskId && <ProgressPage taskId={taskId} go={go} />}
      {page === 'report' && taskId && <ReportPage taskId={taskId} />}
      {page === 'history' && <HistoryPage go={go} />}
    </div>
  );
}
