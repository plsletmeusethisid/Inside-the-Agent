export type TraceEvent = {
  event: string; run_id: string; sequence: number; document_id: string | null;
  timestamp: string; elapsed_ms: number; data: Record<string, unknown>;
};
export type TraceStatus = "idle" | "running" | "complete" | "failed" | "cancelled";

/** POST SSE, with UTF-8/frame boundaries independent of network read boundaries. */
export async function traceRequest<T>(url: string, init: RequestInit, onEvent: (event: TraceEvent) => void): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(typeof data?.detail === "string" ? data.detail : `Request failed (${response.status}). Check your inputs and retry.`);
  }
  if (!response.headers.get("content-type")?.includes("text/event-stream") || !response.body) {
    throw new Error("The API did not return an execution stream.");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "", sequence = 0, runId = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary: RegExpExecArray | null;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        const data = frame.split(/\r?\n/).filter(line => line.startsWith("data:")).map(line => line.slice(5).replace(/^ /, "")).join("\n");
        if (!data) continue; // SSE comments/heartbeats.
        const event = JSON.parse(data) as TraceEvent;
        if (!event.run_id || event.sequence !== sequence + 1 || (runId && event.run_id !== runId)) {
          throw new Error("The execution stream was interrupted. Please retry.");
        }
        runId = event.run_id; sequence = event.sequence;
        if (init.signal?.aborted) throw new DOMException("Cancelled", "AbortError");
        onEvent(event);
        if (event.event === "error") throw new Error(String(event.data.message ?? "Pipeline failed. Please retry."));
        if (event.event === "completed") return event.data.result as T;
      }
      if (done) throw new Error("The connection ended before completion. Saved batches are retained; retry to continue.");
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export function appendTrace(events: TraceEvent[], event: TraceEvent) {
  // Text deltas are rendered in the draft, never repeated in the event inspector.
  if (event.event === "token") return events;
  // Retain lifecycle events for long documents, replacing only batch progress.
  const retained = event.event.endsWith("_progress") ? events.filter(item => item.event !== event.event) : events;
  return [...retained, event];
}
