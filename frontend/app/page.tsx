import { ServiceStatus } from "@/components/service-status";
import { DocumentWorkbench } from "@/components/document-workbench";
import Link from "next/link";

const ingestion = [
  { number: "01", name: "Parse", description: "Extract text and page locations from a PDF." },
  { number: "02", name: "Chunk", description: "Split the text into inspectable passages." },
  { number: "03", name: "Embed", description: "Convert passages into numeric vectors." },
  { number: "04", name: "Store", description: "Persist vectors and source references." },
];

const answering = [
  { number: "05", name: "Ask", description: "Turn a question into a query vector." },
  { number: "06", name: "Retrieve", description: "Inspect the closest matching passages." },
  { number: "07", name: "Answer", description: "Answer from retrieved text with source evidence." },
];

export default function Home() {
  return (
    <main className="shell">
      <header className="topbar">
        <Link className="brand" href="/" aria-label="Inside the Agent home"><span className="brand-mark">◈</span> INSIDE THE AGENT</Link>
        <span className="topbar-label">DOCUMENT QA / LAB 001</span>
      </header>

      <section className="hero">
        <div className="eyebrow"><span className="eyebrow-line" /> AN INTERACTIVE RAG EXPLORER</div>
        <h1>See how an answer<br /><em>takes shape.</em></h1>
        <p>Follow a document through parsing, chunking, embedding, vector search, and an answer with cited evidence.</p>
        <ServiceStatus />
      </section>

      <section className="workbench" aria-label="Pipeline overview">
        <div className="section-heading"><span>THE WORKBENCH</span><span>01 / 02</span></div>
        <div className="workbench-grid">
          <div className="workbench-intro">
            <div className="panel-icon">↳</div>
            <h2>Knowledge in.<br />Answers out.</h2>
            <p>Upload a PDF to inspect its pages and chunks, store their embeddings, then ask a question to see an answer alongside its retrieved evidence.</p>
            <a href="#document-lab" className="phase-tag">EXPLORE THE DOCUMENT LAB ↓</a>
          </div>
          <div className="pipeline-column">
            <div className="column-heading"><span className="mini-dot amber" /> INGESTION <span className="heading-side">DOCUMENT → VECTORS</span></div>
            {ingestion.map((stage) => <div className="stage" key={stage.number}><span className="stage-number">{stage.number}</span><div><h3>{stage.name}</h3><p>{stage.description}</p></div><span className="stage-arrow">↗</span></div>)}
          </div>
          <div className="pipeline-column">
            <div className="column-heading"><span className="mini-dot green" /> INFERENCE <span className="heading-side">QUESTION → ANSWER</span></div>
            {answering.map((stage) => <div className="stage" key={stage.number}><span className="stage-number">{stage.number}</span><div><h3>{stage.name}</h3><p>{stage.description}</p></div><span className="stage-arrow">↗</span></div>)}
            <div className="trace-note"><span>✳</span><span>Execution traces will show each step and the evidence behind an answer.</span></div>
          </div>
        </div>
      </section>

      <DocumentWorkbench />

      <footer><span>BUILT TO MAKE RETRIEVAL VISIBLE</span><span>FASTAPI · NEXT.JS · POSTGRESQL / PGVECTOR</span></footer>
    </main>
  );
}
