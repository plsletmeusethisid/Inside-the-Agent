"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { ExecutionTrace } from "./execution-trace";
import { appendTrace, traceRequest, TraceEvent, TraceStatus } from "@/lib/trace-stream";

type Result = {
  rank: number; chunk_id: string; chunk_index: number; page_number: number;
  content: string; token_count: number; similarity: number;
};
type Retrieval = {
  document_id: string; question: string; k: number; min_similarity: number;
  searched_chunks: number; model: string; dimensions: number;
  query_embedding_preview: number[];
  timings_ms: { query_embedding: number; vector_search: number };
  results: Result[];
  answer?: string;
  answer_status?: "answered" | "insufficient_evidence";
  evidence_chunk_ids?: string[];
  citations?: { chunk_id: string; chunk_index: number; page_number: number }[];
  context?: { chunk_id: string; chunk_index: number; page_number: number; content: string }[];
  generation?: { model: string; performed: boolean; duration_ms: number };
};
const api = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export function RetrievalWorkbench({ documentId, filename, ready, onSelectChunk }: {
  documentId: string; filename: string; ready: boolean; onSelectChunk: (id: string) => void;
}) {
  const [question, setQuestion] = useState("");
  const [k, setK] = useState(3);
  const [minimum, setMinimum] = useState("-1");
  const [mode, setMode] = useState("answer");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Retrieval | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [traceStatus, setTraceStatus] = useState<TraceStatus>("idle");
  const [draft, setDraft] = useState("");
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const queryEmbedding = events.findLast(event => event.event === "query_embedding_completed");
  const submittedQuestion = events.find(event => event.event === "question")?.data.question;

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!ready || busy || !question.trim()) return;
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;
    setBusy(true);
    setError(null);
    setResult(null);
    setEvents([]); setDraft(""); setTraceStatus("running");
    try {
      const data = await traceRequest<Retrieval>(`${api}/documents/${documentId}/${mode}/stream`, {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
        body: JSON.stringify({ question: question.trim(), k, min_similarity: Number(minimum) }),
      }, (event) => {
        if (controller.signal.aborted) return;
        setEvents(current => appendTrace(current, event));
        if (event.event === "token") setDraft(current => current + String(event.data.text));
        if (event.event === "retrieval_completed") setResult(event.data as unknown as Retrieval);
        if (event.event === "context_selected") setResult(current => current ? { ...current, context: event.data.context as Retrieval["context"] } : current);
      });
      if (!controller.signal.aborted) { setResult(data); setDraft(""); setTraceStatus("complete"); }
    } catch (reason) {
      if (!controller.signal.aborted) { setTraceStatus("failed"); setDraft(""); }
      if (!controller.signal.aborted) setError(reason instanceof TypeError ? "Could not reach the API. Check the connection and retry." : reason instanceof Error ? reason.message : "Search failed. Please retry.");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return <section className="retrieval-lab" aria-label="Document questions and retrieval">
    <div className="section-heading"><span>QUESTION LAB</span><span>QUESTION → EVIDENCE → ANSWER</span></div>
    <div className="retrieval-grid">
      <form className="lab-panel retrieval-form" onSubmit={search}>
        <h2>Ask your document.</h2>
        <p>Search within <strong>{filename}</strong>. Your question is embedded with the same model as the document.</p>
        <label htmlFor="question-mode">Response</label>
        <select id="question-mode" value={mode} disabled={busy} onChange={(event) => { setMode(event.target.value); setResult(null); setError(null); setEvents([]); setDraft(""); setTraceStatus("idle"); }}>
          <option value="answer">Answer with evidence</option>
          <option value="retrieve">Retrieve chunks only</option>
        </select>
        <label htmlFor="retrieval-question">Question</label>
        <textarea id="retrieval-question" rows={4} maxLength={2000} required value={question} disabled={busy}
          placeholder="How do I reset my password?" onChange={(event) => setQuestion(event.target.value)} />
        <label htmlFor="retrieval-k">Number of chunks (top k)</label>
        <select id="retrieval-k" value={k} disabled={busy} onChange={(event) => setK(Number(event.target.value))}>
          {Array.from({ length: 20 }, (_, i) => <option key={i + 1} value={i + 1}>{i + 1}</option>)}
        </select>
        <details className="retrieval-filter">
          <summary>Filter by similarity (optional)</summary>
          <label htmlFor="retrieval-minimum">Minimum cosine similarity</label>
          <input id="retrieval-minimum" type="number" min="-1" max="1" step="0.01" required value={minimum}
            disabled={busy} onChange={(event) => setMinimum(event.target.value)} />
          <p>−1 includes all nearest matches. A higher cutoff may return fewer than k chunks, including none. This cutoff is a filter you choose, not a tested relevance threshold.</p>
        </details>
        <button className="primary-button" disabled={!ready || busy || !question.trim()}>
          {busy ? mode === "answer" ? "Retrieving & answering…" : "Embedding question & searching…" : mode === "answer" ? "Answer from document →" : "Find matching chunks →"}
        </button>
        {!ready && <p>Embed and store all document chunks above to enable search.</p>}
        {busy && <button className="cancel-search" type="button" onClick={() => {
          request.current?.abort(); setBusy(false); setDraft(""); setTraceStatus("cancelled"); setError("Request cancelled. You can try again.");
        }}>Cancel request</button>}
        {error && <p className="lab-error" role="alert">{error}</p>}
        <p>{mode === "answer" ? "Answers use only the retrieved context. If it does not contain the answer, the assistant will say so." : "Retrieval only. No answer is generated."}</p>
      </form>
      <div className="lab-panel retrieval-output" aria-busy={busy}>
        <ExecutionTrace title="Question execution" events={events} status={traceStatus}
          stages={[["query_embedding", "Embed Query"], ["retrieval", "Retrieve"], ...(mode === "answer" ? [["generation", "Generate"]] as [string, string][] : [])]} />
        <div role="status" aria-live="polite" className="retrieval-status">
          {busy ? "Following live server events. Intermediate outputs appear as each step finishes." : result ? result.answer_status === "insufficient_evidence" ? "Insufficient evidence in the supplied context." : result.answer_status === "answered" ? "Answer ready with source evidence." : `${result.results.length} matching chunks returned.` : "Enter a question to inspect the evidence."}
        </div>
        {busy && draft && <section className="grounded-answer draft-answer" aria-label="Streaming draft">
          <h3>Generating answer…</h3><p className="answer-text">{draft}<span className="stream-cursor" aria-hidden="true">▍</span></p>
          <small>Draft · evidence references will be checked when generation finishes.</small>
        </section>}
        {result?.answer && <section className="grounded-answer" aria-label="Document answer">
          <h3>{result.answer_status === "answered" ? "Answer" : "Insufficient evidence"}</h3>
          <p className="answer-text">{result.answer}</p>
          {!!result.citations?.length && <>
            <h4>Evidence used</h4>
            <ul className="answer-citations">{result.citations.map((citation) => <li key={citation.chunk_id}>
              <button type="button" onClick={() => onSelectChunk(citation.chunk_id)} title={citation.chunk_id}>
                Chunk {citation.chunk_index + 1} · Page {citation.page_number} ↗
              </button>
              <code>{citation.chunk_id}</code>
            </li>)}</ul>
          </>}
          <p>{result.generation?.performed ? `${result.generation.model} · ${result.generation.duration_ms.toFixed(0)} ms` : "No chunks passed the filter; generation was skipped."}</p>
          <p>Evidence links identify the cited passages. Check them to verify the answer.</p>
        </section>}
        <ol className="retrieval-flow" aria-label="Retrieval pipeline">
          <li><h3>Question</h3><p>{result?.question ?? (typeof submittedQuestion === "string" ? submittedQuestion : "Awaiting search")}</p></li>
          <li><h3>Query embedding</h3>{result ? <>
            <code className="vector-preview">[{result.query_embedding_preview.map((value) => value.toFixed(3)).join(", ")}, …]</code>
            <p>{result.model} · {result.dimensions.toLocaleString()} dimensions · {result.timings_ms.query_embedding.toFixed(0)} ms</p>
            <p>First three values only; a small slice of the vector.</p>
          </> : queryEmbedding ? <>
            <code className="vector-preview">[{(queryEmbedding.data.preview as number[]).map(value => value.toFixed(3)).join(", ")}, …]</code>
            <p>{String(queryEmbedding.data.model)} · {String(queryEmbedding.data.dimensions)} dimensions · {Number(queryEmbedding.data.duration_ms).toFixed(0)} ms</p>
          </> : <p>Awaiting question embedding</p>}</li>
          <li><h3>Vector search</h3>{result ? <p>{result.searched_chunks} stored chunks searched · top {result.k} · minimum {result.min_similarity.toFixed(2)} · {result.timings_ms.vector_search.toFixed(0)} ms</p> : <p>Search is scoped to this document.</p>}</li>
        </ol>
        <p className="score-explanation">Cosine similarity = 1 − cosine distance. Higher means closer (−1 to 1); scores do not establish whether a passage answers your question. Displayed scores are rounded.</p>
        {result?.context && <h3>Exact context supplied to the model</h3>}
        {result && (result.results.length ? <ol className="retrieval-results" aria-label="Ranked chunks">
          {result.results.map((chunk) => <li key={chunk.chunk_id}>
            <button type="button" onClick={() => onSelectChunk(chunk.chunk_id)} aria-label={`Open source for rank ${chunk.rank}, chunk ${chunk.chunk_index + 1}, page ${chunk.page_number}`}>
              <span>#{chunk.rank} Chunk {chunk.chunk_index + 1} — <strong title={String(chunk.similarity)}>{chunk.similarity.toFixed(3)}</strong></span>
              <small>Page {chunk.page_number} · {chunk.token_count} tokens · Open source ↗</small>
              {result.context && <code className="context-id">{chunk.chunk_id}{result.evidence_chunk_ids?.includes(chunk.chunk_id) ? " · Cited" : ""}</code>}
            </button>
            <p>{chunk.content}</p>
          </li>)}
        </ol> : <p className="lab-empty">No chunks met your similarity cutoff. Lower the cutoff or rephrase the question. This does not prove the document has no answer.</p>)}
      </div>
    </div>
  </section>;
}
