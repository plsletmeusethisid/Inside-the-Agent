# Inside the Agent

An interactive document QA pipeline demo. Upload a PDF to inspect its extracted page text and token chunks, generate real OpenAI embeddings, and persist them in pgvector. Watch real execution events over SSE as the document is processed and a question moves through query embedding, retrieval, and a streaming answer with clickable evidence. Retrieval-only inspection is also available.

## Start with Docker Compose

Requires Docker with Compose. From the repository root:

```bash
# Only if .env does not already exist; preserve existing settings and secrets.
cp .env.example .env
docker compose up --build
```

Open http://localhost:3000. API docs are at http://localhost:8000/docs. Check liveness at `/health` and database readiness at `/ready` on port 8000. The backend applies the schema at startup, including when reusing a database volume from step 1. Compose keeps database data in a named volume. Change the development password before exposing any service publicly.

For embeddings, add `OPENAI_API_KEY=...` to the **root `.env`**, save it, and run `docker compose up -d --build`. Compose passes this key only to the backend. Uploading and inspecting text work without a key. Never put it in a `NEXT_PUBLIC_` variable or commit it. Changing a key requires recreating the backend with `docker compose up -d backend`; restarting an existing container does not reload its environment.

## Run the app locally

Start only the database with `docker compose up db -d`, then from separate terminals:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
DATABASE_URL=postgresql://agent_demo:local_dev_password@localhost:5432/agent_demo uvicorn app.main:app --reload
```

```bash
cd frontend
npm ci
npm run dev
```

`FRONTEND_ORIGIN` defaults to `http://localhost:3000` and `NEXT_PUBLIC_API_URL` defaults to `http://localhost:8000`. Set these for other hostnames. Because `NEXT_PUBLIC_` variables are embedded during build, rebuild the frontend after changing its API URL.

When running Python outside Docker, set `OPENAI_API_KEY` in that process's environment as well. The API does not automatically load the root `.env` outside Compose.

## Structure

- `frontend/`: Next.js App Router, TypeScript, Tailwind, and the pipeline overview.
- `backend/app/`: FastAPI API, PDF text extraction, and token chunking.
- `backend/app/embeddings.py`: fixed embedding model, provider validation, batch persistence, and processing summaries.
- `backend/app/database.py`: transactional schema and migration runner.
- `backend/app/retrieval.py`: validated questions, shared query embeddings, and document-scoped cosine search.
- `backend/app/generation.py`: fixed system prompt, structured answers, and evidence validation.
- `backend/app/tracing.py`: request-local events, bounded SSE transport, and cooperative disconnect handling.
- `frontend/lib/trace-stream.ts` and `components/execution-trace.tsx`: POST stream consumption and measured execution states.
- `frontend/components/retrieval-workbench.tsx`: question entry, query vector preview, ranked passages, and source navigation.
- `backend/sql/init.sql`: pgvector extension and document, page, and chunk tables.
- `backend/sql/migrations/002_chunk_embeddings.sql`: additive embedding columns and processing metadata.
- `backend/tests/`: tokenizer, provider-boundary, and opt-in real pgvector integration tests.
- `compose.yaml`: all three development services.

## Inspect a document

Upload a PDF with selectable text in the Document Lab. Set a chunk size from 50–1200 tokens and an overlap up to 300 tokens (less than the size). The app extracts text page by page, normalizes whitespace, splits within page boundaries, and stores the page text and chunks. Click a chunk to see its full content and highlighted location in the normalized extracted page text. The tokenizer is `cl100k_base`.

For a quick walkthrough, upload `examples/it-support-guide.pdf` and set chunk size to 50 tokens. The example is fictional and spans three pages.

The upload limit is 10 MB. Scanned or image-only PDFs need OCR, which is outside this milestone. The original PDF is not stored; only extracted text and its page number are retained. The selected document ID stays in the browser URL for returning to its inspection view.

## Embed and store

After uploading or reopening a document, click **Embed & store chunks**. This sends its chunk text to OpenAI, then stores complete vectors in PostgreSQL. The action is explicit and separate from uploading and question answering. Existing uploaded documents can be embedded through their saved `?document=...` URL.

The fixed model for chunks and questions is **`text-embedding-3-small`**, using **1,536 dimensions**. See the [OpenAI embeddings guide](https://developers.openai.com/api/docs/guides/embeddings) and [model pricing](https://developers.openai.com/api/docs/models/text-embedding-3-small). API usage is billed by input tokens; overlap repeats some text and increases the input token total. Each search embeds one question and incurs provider usage. No local model download or GPU is required.

The UI shows pending, processing, failed, and embedded states. The progress bar counts vectors actually committed to the database. Completion reads, for example, **“5 chunks embedded and stored.”** Each stored chunk displays three rounded values such as `[0.021, -0.331, 0.182, ...]`; the actual values come from its stored vector. The preview is a small slice, not a 2D representation of meaning or a similarity score.

Processing uses batches of up to 32 chunks with a 10-second connection timeout and a 60-second HTTP operation timeout. Each batch commits atomically. Completed batches survive later provider errors or API restarts; **Retry remaining chunks** only embeds chunks without vectors. Repeating a completed request does not call OpenAI or rewrite vectors. A request interrupted after OpenAI responds but before the database commit can incur duplicate provider usage on retry.

One PostgreSQL advisory lock per document prevents concurrent embedding requests (`409`). A dropped database connection releases the lock; inspection reports an interrupted job and offers a retry. The API handles processing in a synchronous worker for the duration of the request; there is no durable background worker. The UI receives committed counts over SSE and refreshes chunk previews at completion. It polls saved counts only when reopening an active job or waiting for a cancelled worker to finish. Leaving the page cancels the browser stream and requests cooperative server cancellation; reopening the document shows saved progress. Large documents may exceed an external reverse proxy's request timeout; reopen and retry if necessary.

## Storage and API

`documents` owns `document_pages` and ordered `chunks`. The migration adds document processing state/error plus these chunk fields: `embedding vector(1536)`, `embedding_model`, `embedding_dimensions`, `embedding_status`, and `embedded_at`. Database constraints enforce the model, dimensions, and consistency between a stored vector and its state. Retrieval uses exact pgvector cosine search over the selected document; no approximate vector index or additional migration is needed for this milestone.

The backend applies numbered SQL migrations once, tracking them in `schema_migrations`. Migration execution is transactional and serialized at startup. Existing documents are preserved and begin with pending embeddings. Changing the model or dimensions requires a new explicit migration and re-embedding policy; environment settings cannot silently mix models. Question embeddings use the same `embed_texts` entry point and model as chunks.

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | Process liveness. |
| `GET /ready` | PostgreSQL, pgvector, and base schema readiness. |
| `POST /documents` | Multipart `file`, `chunk_size`, `chunk_overlap`; stores extracted pages and chunks. |
| `GET /documents/{id}` | Document metadata and ordered extracted pages. |
| `GET /documents/{id}/chunks` | Ordered chunks plus embedding state, model, dimensions, three-value `embedding_preview` (null until stored), and `embedded_at`. Full vectors stay on the backend. |
| `GET /documents/{id}/embeddings` | Processing summary with `status`, `model`, `dimensions`, `total_chunks`, `embedded_chunks`, `remaining_chunks`, `failed_chunks`, and safe `error`. |
| `POST /documents/{id}/embeddings` | Embed all missing vectors, then return the summary. Completed requests are idempotent. |
| `POST /documents/{id}/retrieve` | JSON `question`, `k` (default 3), `min_similarity` (default −1). Returns the question vector preview, measured timings, and ranked chunks with cosine similarity. No answer generation. |
| `POST /documents/{id}/answer` | Same input as retrieval. Retrieves context, then returns an answer, evidence chunk IDs, source citations, and the exact context sent to the model. |

Embedding summary states are `pending`, `processing`, `complete`, `failed`, and `interrupted`. Provider/configuration failures return `502`; database failures return `503`; unknown documents return `404`; invalid UUIDs return `422`. On failure, GET the summary to inspect saved progress. Failed chunks count only attempted failed batches; unattempted chunks remain pending. Provider bodies and credentials are excluded from user-visible errors.

## Retrieve matching passages

Once all document chunks are embedded, use the **Question Lab** below the source inspector. Choose **Retrieve chunks only** to inspect retrieval independently, enter a question, and choose top k (1–20). The returned flow shows **Question → Query embedding → Vector search → Ranked chunks**. Click any ranked chunk heading to open its extracted source page, select that chunk in the list, and highlight its text. Displayed chunk numbers are one-based; API `chunk_index` values are zero-based.

The search returns up to k chunks, scoped by `document_id` in parameterized SQL. Scores are `1 - (embedding <=> query_vector)`, where pgvector's [`<=>` operator computes cosine distance](https://github.com/pgvector/pgvector#querying). Higher scores mean closer vectors, theoretically from −1 to 1 (floating-point precision applies). Ties are ordered by chunk index, then UUID. The UI rounds scores to three decimals; the API retains database precision. Similarity is not an answer-confidence score, and an unrelated question can still have nearest neighbors.

The optional minimum-similarity filter defaults to −1, which includes nearest matches without imposing a relevance cutoff. A higher user-selected cutoff can return fewer than k results or an empty list. No cutoff has been calibrated for semantic quality, and empty results do not establish that the document lacks an answer.

Example request from PowerShell (replace the document ID):

```powershell
$documentId = 'YOUR-DOCUMENT-UUID'
$body = @{ question = 'How do I reset my password?'; k = 3; min_similarity = -1 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/documents/$documentId/retrieve" -ContentType 'application/json' -Body $body
```

Response fields: `document_id`, trimmed `question`, `k`, `min_similarity`, `searched_chunks`, `model`, `dimensions`, `query_embedding_preview` (first three values), `score_definition`, `timings_ms` (`query_embedding`, `vector_search`), and `results`. Each result has `rank`, `chunk_id`, `chunk_index`, `page_number`, `content`, `token_count`, and `similarity`. Empty results still include the query preview and timings. Full question vectors are neither returned nor stored. Searches do not change stored document vectors.

Questions must contain non-whitespace text and be at most 2,000 characters; k must be an integer from 1–20, and the minimum must be finite and between −1 and 1. Invalid inputs return `422`, absent documents `404`, incomplete or incompatible chunk embeddings `409`, provider failures `502`, and database failures `503`. Incomplete documents are rejected before calling OpenAI. Question embedding uses the existing provider timeouts and safe errors. The JSON endpoint returns once retrieval finishes; the UI uses its SSE counterpart to display completed query embeddings and ranked evidence progressively. Retrieval-only mode does not call the generation model.

## Answer from retrieved evidence

The Question Lab defaults to **Answer with evidence**. `POST /documents/{id}/answer` accepts the same validated input as `/retrieve`, calls that same retrieval function, closes the database connection, and sends the question plus all returned chunks to OpenAI. No caller-supplied context or prompt overrides are accepted. The retrieved chunks are not trimmed or reranked: at most 20 chunks of up to 1,200 tokens each, plus question, identifiers, and prompt overhead.

Generation uses the fixed **`gpt-4.1-mini-2025-04-14`** snapshot and the existing backend-only `OPENAI_API_KEY`. No migration, new dependency, or additional environment variable is needed. The model supports the Responses API and structured output ([model documentation](https://developers.openai.com/api/docs/models/gpt-4.1-mini), [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)). The request uses `store: false`, no tools or conversation history, a 2,000-output-token cap, a 10-second connection timeout, and a 60-second HTTP operation timeout. Each answer request incurs question embedding usage and, when context is present, generation input/output usage; see [model pricing](https://developers.openai.com/api/docs/models/gpt-4.1-mini). Retries run retrieval and generation again and may incur additional usage.

One fixed system prompt in `backend/app/generation.py` requires answering only from supplied evidence, treating document instructions as untrusted data, and admitting when that context does not contain an answer. The model returns structured `status`, `answer`, and `evidence_chunk_ids`. The backend rejects unknown, duplicate, or missing evidence IDs for an answered response. Citations' pages and chunk indices come from retrieved records, never from model-generated metadata. Validation confirms source membership, not factual support; users should inspect the cited passages. Prompt instructions are not a guarantee against hallucination or document prompt injection.

The response includes every retrieval response field plus:

| Field | Meaning |
| --- | --- |
| `answer` | Plain-text answer, or a fixed admission that the supplied document context does not contain enough information. |
| `answer_status` | `answered` or `insufficient_evidence`. |
| `evidence_chunk_ids` | Unique IDs of retrieved chunks cited by the model; empty for insufficient evidence. |
| `citations` | Corresponding `chunk_id`, zero-based `chunk_index`, and `page_number` from the database results. |
| `context` | Exact array supplied to generation, with `chunk_id`, `chunk_index`, `page_number`, and full `content`. |
| `generation` | Fixed `model`, whether generation was `performed`, and measured `duration_ms`. |

Example (PowerShell):

```powershell
$documentId = 'YOUR-DOCUMENT-UUID'
$body = @{ question = 'How do I reset my password?'; k = 3 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/documents/$documentId/answer" -ContentType 'application/json' -Body $body
```

If retrieval returns no chunks, generation is skipped and the response is `insufficient_evidence`, with no citations and zero generation duration. This describes the supplied context, not proof about the entire document. If the model reports insufficient evidence, the same clear admission replaces its answer text. Provider errors, refusals, unfinished output, malformed output, and invalid citations return safe `502` errors rather than being presented as missing evidence. Retrieval's `404`, `409`, `422`, and `503` behavior also applies.

The UI shows the answer with clickable evidence and the exact context beneath it, rendered as text. Its SSE request shows real stages and provisional answer text as they arrive. The final answer still requires the same structured-output and citation checks as the JSON endpoint. Answers, questions, and execution traces are not persisted by this app.

## Live execution traces

The existing JSON endpoints remain available. The UI uses these additional **POST** endpoints with the same inputs:

| Endpoint | Stream |
| --- | --- |
| `/documents/stream` | Multipart upload → parse → chunk → save extracted pages/chunks. |
| `/documents/{id}/embeddings/stream` | Explicit embedding action → batch provider requests → committed vectors. |
| `/documents/{id}/retrieve/stream` | Question → query embedding → ranked chunks. |
| `/documents/{id}/answer/stream` | Question → query embedding → retrieval → exact context → streamed answer → validated result. |

Responses use `text/event-stream`, `Cache-Control: no-cache, no-transform`, and `X-Accel-Buffering: no`. The browser consumes a streaming `fetch` POST, since native `EventSource` cannot send these request bodies. Each frame contains an SSE `id` (`run_id:sequence`), named `event`, and JSON `data` envelope:

```json
{
  "event": "parse_completed",
  "run_id": "request UUID",
  "sequence": 3,
  "document_id": "document UUID",
  "timestamp": "UTC ISO timestamp",
  "elapsed_ms": 14.8,
  "data": { "duration_ms": 12.1, "page_count": 3 }
}
```

Numbers above illustrate the contract; the UI displays actual measurements. Every new request gets a fresh run UUID. Document IDs are assigned before parsing; a failed or cancelled upload may never save that document. Sequence numbers are ordered within one run. Heartbeat comments keep otherwise idle streams active every 10 seconds. Traces are not replayable, and `Last-Event-ID` resume is unsupported. Reconnection never automatically repeats a paid call or upload.

Events include all of `parse_started`, `parse_completed`, `chunking_started`, `chunking_completed`, `embedding_started`, `embedding_completed`, `retrieval_started`, `retrieval_completed`, `generation_started`, `token`, and `generation_completed`. Additional events expose the actual pipeline boundaries:

- `trace_started` and `question` identify the run/input.
- `query_embedding_started/completed` distinguish question embedding from document embedding. Completion includes the real vector preview, model, and dimensions.
- `storage_started/progress/completed` distinguish generated vectors from committed data. Upload storage saves extracted text; embedding storage saves vectors. Embedding batches also emit `embedding_progress`. Embedding and storage overlap across batches; their durations sum measured provider and vector-commit work respectively. Already stored vectors are skipped on retry.
- `context_selected` contains exactly the chunk IDs, indices, pages, and text passed to generation. Retrieval completion includes measured scores and ranked text, so evidence can be inspected while generation runs.
- `embedding_skipped`, `storage_skipped`, and `generation_skipped` explicitly report work not performed. A completed embedding retry avoids the provider; empty retrieval skips generation.
- `completed` carries the same final `result` as the corresponding JSON endpoint. A run ends with either this event or `error`, never both. Safe errors carry `stage`, `message`, and `status_code`. Once SSE headers have been sent, HTTP status remains 200; consumers must handle the terminal event. Input validation and upload size/type errors before streaming retain normal HTTP error statuses.

Generation uses the same fixed model, prompt, schema, and timeouts with [`stream: true` in Responses](https://developers.openai.com/api/docs/guides/streaming-responses). `token` carries decoded **answer-text fragments** derived from actual `response.output_text.delta` events, not necessarily individual tokenizer tokens. JSON syntax and evidence-ID fragments are withheld. Split escapes and Unicode are decoded before display. The UI labels text as a draft until the completed response passes validation. Invalid citations, refusals, truncated streams, and provider failures clear the draft and mark the run failed. Insufficient evidence replaces any draft with the standard no-answer message.

Each stream uses a synchronous pipeline worker and a bounded 64-event buffer. The event loop remains available for other requests. Backpressure prevents unlimited event buffering. Cancelling or navigating away closes the browser reader and signals the worker to stop at the next event/provider-stream boundary. Context managers close provider responses, database connections, and advisory locks. A blocking parse, provider read, or database commit cannot be forcibly interrupted by this mechanism; it may finish first, incur usage, or persist data. Successful embedding batches remain saved. Cancellation is not a rollback, and a job can temporarily remain `processing` until the worker releases its lock.

The trace shows completion only after real work finishes, errors on the failing stage, and stopped/unstarted states for unfinished work. Animations only indicate active stages and honor reduced-motion settings. Server event details are expandable by keyboard. Reload shows saved data, not a reconstructed execution history. No credentials, system prompts, provider bodies, or arbitrary exception details are included; retrieved document text is intentionally visible as evidence. No database migration, new provider, or new environment variable is required. Reverse proxies must disable response buffering and allow the request duration; deployment-specific proxy behavior has not been verified.

## Verification

From the root with Docker running:

```powershell
docker compose up -d --build
# Unit/provider tests. Database integration tests skip unless TEST_DATABASE_URL is set.
docker compose run --rm --no-deps -v ./backend/tests:/app/tests:ro backend python -m unittest discover -s tests
# All tests, including persistence in a unique temporary schema in the configured database.
docker compose run --rm --no-deps -v ./backend/tests:/app/tests:ro backend python -c "import os, sys, unittest; os.environ['TEST_DATABASE_URL'] = os.environ['DATABASE_URL']; result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('tests')); sys.exit(not result.wasSuccessful())"
docker compose exec -T frontend npm run lint
docker compose exec -T frontend npm run typecheck
docker compose exec -T frontend npm test
```

Locally, install backend dependencies and run `python -m unittest discover -s tests` from `backend/`. Set `TEST_DATABASE_URL` to enable the integration tests. They create and remove only a uniquely named `test_embeddings_...` schema. Provider responses are fixtures in automated tests; these tests do not measure semantic quality or spend API credits.

Browser smoke path: open **http://localhost:3000**, check the connected status, upload `examples/it-support-guide.pdf` with size **50** and overlap **10**, and select each chunk to verify source highlighting. Click **Embed & store chunks**, verify all chunks have previews and a completion summary, then reload the same URL to confirm persistence. This final embedding step calls OpenAI and requires an API key.

Observed checks (2026-09-26): 17 backend tests passed with real tiktoken and pgvector, including migration preservation, partial failure/resume, concurrent requests, invalid provider data, and a PDF-to-vector API path. Frontend lint, TypeScript, and production Docker builds passed. The browser ingestion smoke passed at `localhost:3000` with five chunks and matching source highlights. Browser checks also confirmed saved errors and enabled retries before successful processing. After resolving an initial OpenAI quota error, the real embedding browser smoke passed: PostgreSQL contained five non-null vectors, each with `vector_dims(embedding) = 1536`. The UI displayed all five previews and “5 chunks embedded and stored.” The first actual rounded preview was `[0.016, 0.008, 0.002, ...]`. Reload preserved the results, and a completed repeat request left IDs, previews, and timestamps unchanged. No browser page errors or horizontal overflow at 390px width were observed. This verifies generation and persistence, not retrieval quality.

Retrieval implementation checks (2026-09-26): all **23 backend tests** passed with real tiktoken and pgvector, including cosine arithmetic, ranking and ties, document isolation, search without modifying vectors, cutoff/empty results, incomplete documents, validation, provider failures and retry. Frontend lint, typecheck, and production build passed. Automated provider responses remain fixtures, not semantic-quality measurements.

Retrieval browser smoke: upload the example at **50 tokens / overlap 10**, embed all five chunks, then search **“How do I connect to the VPN?”** and **“How do I reset my password?”** with k=3. Inspect the returned scores, click each result to verify the extracted source highlight, and try a high minimum-similarity filter to see the empty state. Each search calls OpenAI. This is a targeted smoke check, not a retrieval-quality evaluation suite.

Observed retrieval browser results (2026-09-26): VPN ranked **chunk 2 / page 2 first, 0.595721**; password reset ranked **chunk 4 / page 3 first, 0.611235**. The browser displayed matching database scores and source text. Connectivity/CORS, five-chunk upload and real embeddings, source selection/highlighting, empty cutoff results, provider-error UI, cancellation, blank input, and no horizontal overflow at 390px passed with no browser page errors. The first smoke attempt stopped on an ambiguous test alert selector; the corrected test passed with the already embedded document. Automated backend tests emitted a non-failing TestClient/httpx deprecation warning. No general semantic-quality claim is made from these two searches.

## Answer verification

Answer-layer verification (2026-09-26): all **33 backend tests** passed after the no-answer regression fix, with real tiktoken/pgvector and fixture model responses. Frontend lint, typecheck, and production Docker builds passed. Headless Edge at localhost verified `/ready` 200 with matching CORS, a fresh example upload at **50 tokens / overlap 10**, five real embeddings, and real model answers. VPN cited **chunk 2 / page 2**; password reset cited **chunks 4 and 5 / page 3**. Both matched the retrieved evidence and clickable source highlights. **“What is the capital of France?”** returned `insufficient_evidence` with no citations, despite nonempty retrieval. A cutoff of 1 produced empty context and skipped generation. Browser checks also passed for exact context display, provider-error handling, cancellation, blank input, retrieval-only mode, unchanged stored chunk metadata/previews, reload, and no horizontal overflow at 390px. No browser page errors were observed.

The first browser run exposed a valid `insufficient_evidence` model response with an empty answer string. Validation now permits that case and supplies the fixed no-answer message; supported answers still require nonblank text and valid evidence IDs. A regression test covers this behavior, and the full browser rerun passed. The existing non-failing TestClient/httpx deprecation warning remains. These three real questions are smoke evidence, not a general grounding or adversarial evaluation. No Git repository is present, so no commit was created.

To repeat the answer smoke manually: upload/embed the bundled example with the settings above, select **Answer with evidence**, ask the VPN and password questions, and click every citation to inspect its highlighted text. Ask the unrelated capital question and confirm insufficient evidence and no citations. Set minimum similarity to 1 to check generation is skipped, then switch to **Retrieve chunks only** to confirm retrieval remains available. Each nonempty-context answer calls OpenAI.

## Remaining milestones

The current milestone includes live execution traces and streaming grounded answers with validated source references. A versioned retrieval/grounding evaluation set remains future work. The fixed role is document question answering; no autonomous tool loop is planned for this initial demo. OCR, original-PDF coordinates, authentication, and public multiuser hosting remain outside this milestone.

## Trace verification

Verified on 2026-09-27: **43 backend tests** passed with real tiktoken/pgvector, including event delivery before completion, stable identifiers/order, provider streaming, split JSON escapes/Unicode, truncated streams, invalid citations, safe errors, and disconnect cleanup. A cancellation after one committed batch retained 32 of 33 vectors, released the lock, and resumed the final chunk. Final review fixed a worker's reentrant advisory-lock probe incorrectly reporting its own progress as interrupted; the suite passed with an assertion for the correct processing state. **Three frontend stream-parser tests**, frontend lint, typecheck, and the production Docker build passed. Parser tests cover byte-split UTF-8, CRLF/heartbeats, terminal errors, premature EOF, ordering, and abort cleanup.

The Edge browser smoke confirmed actual SSE upload and embedding of the bundled five-chunk example, live running states, evidence visible before generation completion, and progressive draft text. VPN returned 50 answer-text fragments and cited chunk 2/page 2; password reset returned 61 fragments and cited chunks 4–5/page 3. These fragment counts are observed outputs, not required or deterministic counts. Unrelated questions returned insufficient evidence, and a cutoff of 1 skipped generation. Citation highlighting, retrieval-only mode, fixture provider-error display, cancellation without stale output, unchanged stored vectors, reload without invented trace history, and 390px overflow checks passed with no page errors. The first smoke attempt hit a DevTools response-body retrieval limitation; capturing the stream in-page fixed the test. Provider fixtures verify contracts; these real questions are smoke evidence, not a grounding evaluation. The pre-existing non-failing TestClient/httpx deprecation warning remains.

Reproduce the browser smoke from the root with the services running, a configured backend key, Node.js, and Microsoft Edge installed (uses paid OpenAI calls and saves a new bundled example document):

```powershell
npm install --prefix .verification --no-save playwright-core
node scripts/trace-smoke.cjs
```

Screenshots are written to the ignored `.verification/trace-desktop.png` and `trace-mobile.png`. To inspect the flow manually, upload the example at 50 tokens/10 overlap, embed it, ask a VPN question, and expand **Inspect server events**. Cancel during a run to check that unfinished stages are not marked complete.
