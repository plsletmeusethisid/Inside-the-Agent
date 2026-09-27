import { TraceEvent, TraceStatus } from "@/lib/trace-stream";

export function ExecutionTrace({ title, stages, events, status }: {
  title: string; stages: [string, string][]; events: TraceEvent[]; status: TraceStatus;
}) {
  const error = events.findLast(event => event.event === "error");
  const latest = events.at(-1);
  return <section className="execution-trace" aria-label={title}>
    <div className="trace-heading"><h3>{title}</h3><span role="status">{status === "running" ? "Live" : status === "complete" ? "Complete" : status === "idle" ? "Awaiting run" : status === "cancelled" ? "Cancelled" : "Failed"}</span></div>
    <ol className="trace-stages" aria-live="polite">
      {stages.map(([key, label]) => {
        const last = events.findLast(event => ["started", "completed", "skipped"].some(suffix => event.event === `${key}_${suffix}`));
        const failed = error?.data.stage === key;
        const state = failed ? "failed" : last?.event.endsWith("_completed") ? "complete" : last?.event.endsWith("_skipped") ? "skipped" : last ? status === "running" ? "running" : "stopped" : "waiting";
        const duration = last?.data.duration_ms;
        const progress = events.findLast(event => event.event === `${key}_progress`);
        return <li key={key} className={`trace-stage ${state}`}>
          <span className="trace-symbol" aria-hidden="true">{state === "complete" ? "✓" : state === "failed" ? "!" : state === "running" ? "●" : "○"}</span>
          <span><strong>{label}</strong><small>{state === "complete" ? "Completed" : state === "running" ? "Running…" : state === "failed" ? "Failed" : state === "skipped" ? "Skipped" : state === "stopped" ? "Stopped before completion was confirmed" : "Not started"}{typeof duration === "number" ? ` · ${duration.toFixed(0)} ms` : ""}</small>
            {state === "skipped" && <small>{String(last?.data.reason)}</small>}
            {progress && key === "storage" && <small>{String(progress.data.embedded_chunks)} / {String(progress.data.total_chunks)} vectors saved</small>}
          </span>
        </li>;
      })}
    </ol>
    {error && <p className="lab-error">{String(error.data.message)}</p>}
    {events.length > 0 && <details className="trace-events"><summary>Inspect server events · {latest?.elapsed_ms.toFixed(0)} ms elapsed</summary>
      <p>Run <code>{events[0].run_id}</code>. Timings are measured on the server. Events are kept for this view only.</p>
      <ol>{events.map(event => <li key={event.sequence}><details><summary><code>{event.event}</code><span>+{event.elapsed_ms.toFixed(0)} ms</span></summary><pre>{JSON.stringify(event.data, null, 2)}</pre></details></li>)}</ol>
    </details>}
  </section>;
}
