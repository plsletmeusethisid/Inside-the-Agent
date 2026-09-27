# Retrieval and answer evaluation

`harbor-handbook-v1.pdf` is a six-page fictional handbook, generated from `handbook-v1.json` by `build_document.py`. The same JSON contains 16 questions: 12 supported, two ambiguous, and two unanswerable. Each supported question has an expected section, page, and one or more short verbatim evidence anchors. No user document is included.

Settings are fixed at **120 tokens / 20 overlap / top 3 / minimum similarity -1**, using the app's real `cl100k_base` tokenizer and `text-embedding-3-small` embeddings. The PDF has 12 chunks with these settings. Changing the document or chunk settings requires reviewing the labels and versioning the fixture again.

## Run

Start the development stack and configure its backend key first. From the repository root:

```powershell
# Optional: reproduce the committed PDF with the installed PyMuPDF dependency.
docker compose run --rm --no-deps -v ./evaluation:/workspace/evaluation backend python /workspace/evaluation/build_document.py

# Real retrieval only; uploads/embeds the fixture, then asks every question.
docker compose run --rm --no-deps -v ./evaluation:/workspace/evaluation -v ./backend:/workspace/backend backend python /workspace/evaluation/run.py --api http://backend:8000

# Real answers, citations, and abstention checks as well.
docker compose run --rm --no-deps -v ./evaluation:/workspace/evaluation -v ./backend:/workspace/backend backend python /workspace/evaluation/run.py --api http://backend:8000 --answers --check
```

These commands use paid provider calls and persist a small fictional document. Use `--document-id UUID` to reuse an earlier fixture and its embeddings. The runner checks extracted pages and chunk settings before reuse, and rejects labels that do not map to any chunk. With local backend dependencies installed, `python evaluation/run.py --api http://localhost:8000 --answers` works too. A deployed same-origin API uses `--api https://YOUR_DOMAIN/api`.

Reports default to `evaluation/results/UTC_TIMESTAMP.json`; `--output PATH` selects an explicit location. Each contains the dataset/PDF SHA-256, settings, embedding model/dimension, real answer-model metadata, source IDs/pages/text/scores, answers, citations, per-question results, and aggregate counts. Partial reports are written after each question. A report without `completed_at` is unfinished. Failed requests remain in metric denominators.

`--check` exits nonzero on API/contract errors, any supported retrieval miss, invalid citations, a supported question lacking an answered status or expected-source citation, or failure to abstain on an unanswerable question. Ambiguous-question abstention and partial evidence coverage are reported as diagnostics. They are not additional exit gates. The current answer run intentionally exits **1** because of the Wi-Fi-password status failure below; retrieval-only checks pass.

## What is measured

- **Hit@3:** fraction of supported questions with at least one expected evidence anchor represented by a returned top-three chunk.
- **Macro evidence recall@3:** for each supported question, fraction of its labeled evidence anchors covered by top-three chunks, averaged across questions. Overlapping chunks containing the same anchor are alternatives, not extra required sources.
- **All evidence@3:** fraction of supported questions whose full labeled evidence is covered.
- **Citation validity:** cited IDs belong to the actual returned context; page/index metadata agrees with stored chunks; answered responses have unique, nonempty citations and abstentions have none. This is a structural check, not factual-entailment grading.
- **Expected source cited / cited evidence recall:** the answer cites at least one / a fraction of the annotated evidence anchors.
- **Supported answer rate and abstention rates:** compare structured `answer_status` against expected behavior. Unanswerable and ambiguous cases are separate and excluded from retrieval denominators.

## Observed results, 2026-09-27

Both runs use real OpenAI embeddings and the fixed `gpt-4.1-mini-2025-04-14` answer snapshot. [Baseline](results/harbor-v1-baseline.json) and [current](results/harbor-v1-current.json) retain all observations, including failures.

| Metric | Baseline | Current |
| --- | --- | --- |
| Hit@3, supported questions | 12/12 (100%) | 12/12 (100%) |
| Macro evidence recall@3 | 100% | 100% |
| All expected evidence in top 3 | 12/12 | 12/12 |
| Structurally valid citations | 16/16 | 16/16 |
| Supported answers citing expected source | 12/12 | 12/12 |
| Macro cited evidence recall | 100% | 100% |
| Supported answer status | 12/12 | 12/12 |
| Unanswerable abstention | 1/2 | 1/2 |
| Ambiguous abstention | 1/2 | 1/2 |
| API errors | 0/16 | 0/16 |

The baseline exposed two generation-stage failures. For “What is the deadline?”, the model selected the travel-receipt policy without asking what deadline was meant. For the HarborGuest password, it truthfully said the handbook does not list the password but classified that as `answered` with a citation, contrary to the expected `insufficient_evidence` status. It did **not** invent a password. The unrelated parental-leave question and ambiguous allowance question abstained correctly.

The current prompt explicitly requires abstaining on an unspecified subject or a fact described as absent. The identical evaluation was rerun with the updated prompt deployed; both failures persisted. These are known generation limitations, not retrieval misses. The labels were not relaxed to make the result pass.

This is a small, authored development set with one handbook and no held-out documents. It catches concrete regressions; it does not establish general retrieval quality, factual support for every generated claim, or prompt-injection resistance. A production evaluation should add independently labeled held-out documents and human claim-level review.
