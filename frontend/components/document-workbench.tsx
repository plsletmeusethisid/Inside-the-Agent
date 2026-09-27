"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { RetrievalWorkbench } from "./retrieval-workbench";
import { ExecutionTrace } from "./execution-trace";
import { appendTrace, traceRequest, TraceEvent, TraceStatus } from "@/lib/trace-stream";

type Page = { number: number; content: string };
type Chunk = {
  id: string; index: number; page_number: number; content: string; token_count: number;
  embedding_status: "pending" | "processing" | "embedded" | "failed";
  embedding_model: string; embedding_dimensions: number; embedding_preview: number[] | null;
};
type EmbeddingSummary = {
  status: "pending" | "processing" | "complete" | "failed" | "interrupted";
  model: string; dimensions: number; total_chunks: number; embedded_chunks: number;
  remaining_chunks: number; failed_chunks: number; error: string | null;
};
type Document = { id: string; filename: string; page_count: number; chunk_size: number; chunk_overlap: number; pages: Page[] };
type UploadResult = { id: string; chunk_count: number };

const api = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const pageSize = 12;

async function readJson<T>(response: Response): Promise<T> {
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status})`);
  return data as T;
}

function HighlightedSource({ content, highlight }: { content: string; highlight?: string }) {
  if (!highlight) return <>{content || "No selectable text on this page."}</>;
  const start = content.indexOf(highlight);
  if (start < 0) return <>{content}</>;
  return <>{content.slice(0, start)}<mark>{highlight}</mark>{content.slice(start + highlight.length)}</>;
}

function vectorPreview(values: number[]) {
  return `[${values.map((value) => value.toFixed(3)).join(", ")}, ...]`;
}

async function fetchDocument(id: string, signal?: AbortSignal) {
  const [doc, chunkData, progress] = await Promise.all([
    fetch(`${api}/documents/${id}`, { signal }).then((r) => readJson<Document>(r)),
    fetch(`${api}/documents/${id}/chunks`, { signal, cache: "no-store" }).then((r) => readJson<{ chunks: Chunk[] }>(r)),
    fetch(`${api}/documents/${id}/embeddings`, { signal, cache: "no-store" }).then((r) => readJson<EmbeddingSummary>(r)),
  ]);
  return { doc, chunkData, progress };
}

export function DocumentWorkbench() {
  const [file, setFile] = useState<File | null>(null);
  const [size, setSize] = useState(500);
  const [overlap, setOverlap] = useState(75);
  const [document, setDocument] = useState<Document | null>(null);
  const [chunks, setChunks] = useState<Chunk[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [listPage, setListPage] = useState(0);
  const [sourcePage, setSourcePage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [embedding, setEmbedding] = useState<EmbeddingSummary | null>(null);
  const [embeddingBusy, setEmbeddingBusy] = useState(false);
  const [embeddingError, setEmbeddingError] = useState<string | null>(null);
  const [uploadEvents, setUploadEvents] = useState<TraceEvent[]>([]);
  const [uploadStatus, setUploadStatus] = useState<TraceStatus>("idle");
  const [embeddingEvents, setEmbeddingEvents] = useState<TraceEvent[]>([]);
  const [embeddingTraceStatus, setEmbeddingTraceStatus] = useState<TraceStatus>("idle");
  const uploadRequest = useRef<AbortController | null>(null);
  const embeddingRequest = useRef<AbortController | null>(null);
  const sourcePanel = useRef<HTMLDivElement | null>(null);
  const documentId = document?.id;
  const processing = embeddingBusy || embedding?.status === "processing";

  useEffect(() => () => { embeddingRequest.current?.abort(); uploadRequest.current?.abort(); }, []);

  useEffect(() => {
    // Only poll a saved/reopened job. Our own request receives committed counts via SSE.
    if (!documentId || !processing || embeddingBusy) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const [progress, chunkData] = await Promise.all([
          fetch(`${api}/documents/${documentId}/embeddings`, { signal: controller.signal, cache: "no-store" }).then((r) => readJson<EmbeddingSummary>(r)),
          fetch(`${api}/documents/${documentId}/chunks`, { signal: controller.signal, cache: "no-store" }).then((r) => readJson<{ chunks: Chunk[] }>(r)),
        ]);
        if (!controller.signal.aborted) {
          setEmbedding(progress);
          setChunks(chunkData.chunks);
          setEmbeddingError(null);
        }
      } catch {
        if (!controller.signal.aborted) setEmbeddingError("Could not refresh embedding progress. Reconnecting…");
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 1000);
      }
    }
    timer = setTimeout(poll, 1000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [documentId, processing, embeddingBusy]);

  function showDocument({ doc, chunkData, progress }: Awaited<ReturnType<typeof fetchDocument>>) {
    setDocument(doc);
    setChunks(chunkData.chunks);
    setEmbedding(progress);
    setEmbeddingError(null);
    setEmbeddingEvents([]); setEmbeddingTraceStatus("idle");
    setSize(doc.chunk_size);
    setOverlap(doc.chunk_overlap);
    setSelectedIndex(0);
    setListPage(0);
    setSourcePage(chunkData.chunks[0]?.page_number ?? 1);
  }

  async function handleEmbedding() {
    if (!documentId || processing) return;
    const controller = new AbortController();
    embeddingRequest.current = controller;
    setEmbeddingBusy(true);
    setEmbeddingError(null);
    setEmbeddingEvents([]); setEmbeddingTraceStatus("running");
    try {
      const progress = await traceRequest<EmbeddingSummary>(`${api}/documents/${documentId}/embeddings/stream`, { method: "POST", signal: controller.signal }, (event) => {
        if (controller.signal.aborted) return;
        setEmbeddingEvents(current => appendTrace(current, event));
        if (event.event === "storage_progress" || event.event === "storage_completed") setEmbedding(event.data as unknown as EmbeddingSummary);
      });
      if (!controller.signal.aborted) { setEmbedding(progress); setEmbeddingTraceStatus("complete"); }
    } catch (reason) {
      if (!controller.signal.aborted) setEmbeddingTraceStatus("failed");
      if (!controller.signal.aborted) setEmbeddingError(reason instanceof Error ? reason.message : "Embedding failed");
    } finally {
      if (!controller.signal.aborted) {
        try {
          const [progress, chunkData] = await Promise.all([
            fetch(`${api}/documents/${documentId}/embeddings`, { signal: controller.signal, cache: "no-store" }).then((r) => readJson<EmbeddingSummary>(r)),
            fetch(`${api}/documents/${documentId}/chunks`, { signal: controller.signal, cache: "no-store" }).then((r) => readJson<{ chunks: Chunk[] }>(r)),
          ]);
          if (!controller.signal.aborted) { setEmbedding(progress); setChunks(chunkData.chunks); }
        } catch {
          if (!controller.signal.aborted) {
            setEmbedding((current) => current ? { ...current, status: "interrupted" } : null);
            setEmbeddingError("Could not refresh saved progress. Reload this document when the API recovers.");
          }
        }
        if (!controller.signal.aborted) setEmbeddingBusy(false);
      }
    }
  }

  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("document");
    if (!id) return;
    const controller = new AbortController();
    fetchDocument(id, controller.signal).then((data) => {
      if (!controller.signal.aborted) showDocument(data);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not load document");
    });
    return () => controller.abort();
  }, []);

  async function handleUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file) return;
    setBusy(true);
    setError(null);
    const controller = new AbortController();
    uploadRequest.current = controller;
    setUploadEvents([]); setUploadStatus("running");
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("chunk_size", String(size));
      form.append("chunk_overlap", String(overlap));
      const result = await traceRequest<UploadResult>(`${api}/documents/stream`, { method: "POST", body: form, signal: controller.signal }, (event) => {
        if (!controller.signal.aborted) setUploadEvents(current => appendTrace(current, event));
      });
      if (controller.signal.aborted) return;
      setUploadStatus("complete");
      window.history.replaceState(null, "", `?document=${result.id}`);
      const loaded = await fetchDocument(result.id, controller.signal);
      if (controller.signal.aborted) return;
      showDocument(loaded);
      window.history.replaceState(null, "", `?document=${result.id}`);
    } catch (reason) {
      if (!controller.signal.aborted) { setUploadStatus(current => current === "complete" ? current : "failed"); setError(reason instanceof Error ? reason.message : "Upload failed"); }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  const selected = chunks[selectedIndex];
  const page = document?.pages.find((item) => item.number === sourcePage);
  const visible = chunks.slice(listPage * pageSize, (listPage + 1) * pageSize);
  const totalListPages = Math.ceil(chunks.length / pageSize);

  return (
    <section className="document-lab" id="document-lab" aria-label="Document chunk inspection">
      <div className="section-heading"><span>DOCUMENT LAB</span><span>PARSE / CHUNK / EMBED / STORE</span></div>
      <div className="lab-grid">
        <form className="lab-panel upload-panel" onSubmit={handleUpload}>
          <div className="lab-kicker">01 — SOURCE</div>
          <h2>Start with a PDF.</h2>
          <p>Upload a PDF with selectable text to see how its pages become individual chunks.</p>
          <label className="file-drop">
            <span className="drop-symbol">↑</span>
            <strong>{file ? file.name : "Choose a PDF"}</strong>
            <small>{file ? `${(file.size / 1024).toFixed(0)} KB selected` : "PDF · up to 10 MB · selectable text"}</small>
            <input type="file" accept=".pdf,application/pdf" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </label>
          <div className="lab-settings">
            <label>Chunk size <span>{size} tokens</span><input type="range" min="50" max="1200" step="25" value={size} onChange={(e) => { const next = Number(e.target.value); setSize(next); setOverlap((current) => Math.min(current, next - 1)); }} /></label>
            <label>Overlap <span>{overlap} tokens</span><input type="range" min="0" max={Math.min(300, size - 1)} step="5" value={Math.min(overlap, size - 1)} onChange={(e) => setOverlap(Number(e.target.value))} /></label>
          </div>
          <button className="primary-button" type="submit" disabled={busy || processing || !file || overlap >= size}>{busy ? "Processing document…" : "Parse & inspect chunks →"}</button>
          {error && <p className="lab-error" role="alert">{error}</p>}
          {busy && <button className="cancel-search" type="button" onClick={() => {
            uploadRequest.current?.abort(); setBusy(false); setUploadStatus("cancelled");
            setError("Upload cancelled. A database commit already in progress may still finish.");
          }}>Cancel upload</button>}
          <ExecutionTrace title="Document execution" events={uploadEvents} status={uploadStatus}
            stages={[["parse", "Parse"], ["chunking", "Chunk"], ["storage", "Save pages & chunks"]]} />
          {document && !uploadEvents.length && <p>Saved document loaded. Past execution events are not replayed.</p>}
          {document && <div className="lab-summary"><strong>{document.filename}</strong><span>{document.page_count} pages · {chunks.length} chunks</span><span>{document.chunk_size} token limit · {document.chunk_overlap} token overlap</span></div>}
          {document && embedding && <div className="embedding-panel">
            <h3>Embed & store</h3>
            <p>Send this document’s chunks to OpenAI to generate embeddings, then store the full vectors in pgvector.</p>
            <span className="embedding-model">{embedding.model} · {embedding.dimensions.toLocaleString()} dimensions</span>
            <ExecutionTrace title="Embedding execution" events={embeddingEvents} status={embeddingTraceStatus}
              stages={[["embedding", "Embed"], ["storage", "Store vectors"]]} />
            <button className="primary-button" type="button" onClick={handleEmbedding} disabled={busy || processing || embedding.status === "complete"}>
              {processing ? "Embedding & storing…" : embedding.status === "complete" ? "All embeddings stored ✓" : embedding.status === "failed" || embedding.status === "interrupted" ? "Retry remaining chunks →" : "Embed & store chunks →"}
            </button>
            {embeddingBusy && <button className="cancel-search" type="button" onClick={() => {
              embeddingRequest.current?.abort(); setEmbeddingBusy(false); setEmbeddingTraceStatus("cancelled");
              setEmbedding(current => current ? { ...current, status: "processing" } : null);
              setEmbeddingError("Cancellation requested. Checking saved progress while the server releases this run.");
            }}>Cancel embedding</button>}
            <div className="embedding-progress" role="status" aria-live="polite">
              <strong>{embedding.status === "complete" ? `${embedding.embedded_chunks} chunks embedded and stored.` : `${embedding.embedded_chunks} of ${embedding.total_chunks} chunks embedded and stored.`}</strong>
              <progress aria-label="Chunks embedded and stored" value={embedding.embedded_chunks} max={Math.max(1, embedding.total_chunks)} />
              {processing && <span>Completed batches appear as they are saved.</span>}
              {(embedding.status === "failed" || embedding.status === "interrupted") && <span>Saved vectors are retained. Retry resumes the remaining chunks.</span>}
            </div>
            {(embeddingError || embedding.error) && <p className="lab-error" role="alert">{embeddingError || embedding.error}</p>}
          </div>}
        </form>

        <div className="lab-panel chunks-panel">
          <div className="lab-kicker">02 — CHUNKS <span>{chunks.length ? `${chunks.length} TOTAL` : "AWAITING DOCUMENT"}</span></div>
          {chunks.length ? <>
            <div className="chunk-list">
              {visible.map((chunk) => <button className={`chunk-row ${chunk.index === selectedIndex ? "chosen" : ""}`} key={chunk.id} type="button" onClick={() => { setSelectedIndex(chunk.index); setSourcePage(chunk.page_number); }}>
                <span className="chunk-badge">{String(chunk.index + 1).padStart(2, "0")}</span>
                <span className="chunk-row-text">
                  <strong>Page {chunk.page_number} · {chunk.token_count} tokens</strong><small>{chunk.content}</small>
                  <span className={`embedding-badge ${chunk.embedding_status}`}>{chunk.embedding_status === "embedded" ? "✓ Embedded · stored" : chunk.embedding_status === "processing" ? embedding?.status === "interrupted" ? "Interrupted · retry available" : "Embedding…" : chunk.embedding_status === "failed" ? "Failed · retry available" : "Awaiting embedding"}</span>
                  {chunk.embedding_preview && <code className="vector-preview">{vectorPreview(chunk.embedding_preview)}</code>}
                </span>
                <span className="stage-arrow">↗</span>
              </button>)}
            </div>
            <div className="list-pagination"><button disabled={listPage === 0} onClick={() => setListPage(listPage - 1)}>← Prev</button><span>{listPage + 1} / {totalListPages}</span><button disabled={listPage + 1 >= totalListPages} onClick={() => setListPage(listPage + 1)}>Next →</button></div>
          </> : <div className="lab-empty">The chunks generated from your document will appear here. Select one to inspect its text and source page.</div>}
        </div>

        <div className="lab-panel source-panel" ref={sourcePanel} tabIndex={-1} aria-label="Selected chunk source">
          <div className="lab-kicker">03 — INSPECT <span>{selected ? `CHUNK ${selected.index + 1}` : "SOURCE VIEW"}</span></div>
          {document && selected ? <>
            <div className="inspector-header"><span>Chunk text</span><span>{selected.token_count} / {document.chunk_size} tokens</span></div>
            <div className="selected-content">{selected.content}</div>
            <div className="embedding-inspector">
              <div className="inspector-header"><span>Embedding preview</span><span>{selected.embedding_preview ? `3 / ${selected.embedding_dimensions.toLocaleString()} values` : "Not stored yet"}</span></div>
              {selected.embedding_preview ? <><code className="vector-preview">{vectorPreview(selected.embedding_preview)}</code><p>The full vector is stored in pgvector. These rounded values are a small slice, not a 2D map of meaning.</p></> : <p>{selected.embedding_status === "failed" ? "This batch failed. Retry embedding to resume." : "A preview will appear after this chunk’s embedding is stored."}</p>}
            </div>
            <div className="inspector-header"><span>Extracted page {sourcePage}</span><span>{sourcePage} / {document.page_count}</span></div>
            <div className="page-controls"><button onClick={() => setSourcePage(Math.max(1, sourcePage - 1))} disabled={sourcePage === 1}>←</button><select aria-label="Source page" value={sourcePage} onChange={(e) => setSourcePage(Number(e.target.value))}>{document.pages.map((p) => <option value={p.number} key={p.number}>Page {p.number}</option>)}</select><button onClick={() => setSourcePage(Math.min(document.page_count, sourcePage + 1))} disabled={sourcePage === document.page_count}>→</button></div>
            <div className="page-text"><HighlightedSource content={page?.content ?? ""} highlight={sourcePage === selected.page_number ? selected.content : undefined} /></div>
          </> : <div className="lab-empty">Choose a chunk to see its full text beside the page it came from.</div>}
        </div>
      </div>
      {document && <RetrievalWorkbench key={document.id} documentId={document.id} filename={document.filename}
        ready={!busy && !processing && !!embedding && embedding.total_chunks > 0 && embedding.embedded_chunks === embedding.total_chunks}
        onSelectChunk={(id) => {
          const index = chunks.findIndex((chunk) => chunk.id === id);
          if (index < 0) return;
          setSelectedIndex(index);
          setListPage(Math.floor(index / pageSize));
          setSourcePage(chunks[index].page_number);
          sourcePanel.current?.focus({ preventScroll: true });
          sourcePanel.current?.scrollIntoView({ behavior: "smooth", block: "center" });
        }} />}
    </section>
  );
}
