"""Tests for page provenance, size bounds, and lossless non-overlap chunking."""

import unittest

import pymupdf

from app.ingestion import Page, chunk_pages, count_tokens, parse_pdf


class IngestionTests(unittest.TestCase):
    def test_pdf_pages_and_chunk_provenance(self):
        pdf = pymupdf.open()
        for text in ("Onboarding policy for new employees.", "VPN access requires approval."):
            page = pdf.new_page()
            page.insert_text((70, 70), text)
        pages = parse_pdf(pdf.tobytes())
        pdf.close()
        self.assertEqual([p.number for p in pages], [1, 2])
        chunks = chunk_pages(pages, size=50, overlap=0)
        self.assertEqual([c.page_number for c in chunks], [1, 2])
        self.assertIn("VPN access", chunks[1].content)

    def test_long_words_and_unicode_do_not_exceed_limit_or_disappear(self):
        source = "A" * 180 + " 한국어😀 documentation" + " extra" * 30
        chunks = chunk_pages([Page(1, source)], size=50, overlap=0)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c.token_count == count_tokens(c.content) <= 50 for c in chunks))
        self.assertEqual(" ".join(c.content for c in chunks).replace(" ", ""), source.replace(" ", ""))
        self.assertTrue(all(c.content in source for c in chunks))

    def test_overlap_advances_and_keeps_page_boundary(self):
        pages = [Page(1, " ".join(f"step{i}" for i in range(60))), Page(2, "Other page information")]
        chunks = chunk_pages(pages, size=50, overlap=12)
        self.assertEqual([c.index for c in chunks], list(range(len(chunks))))
        self.assertTrue(all(c.token_count <= 50 for c in chunks))
        self.assertEqual(chunks[-1].page_number, 2)
        self.assertGreater(len([c for c in chunks if c.page_number == 1]), 1)
        self.assertIn(chunks[0].content.split()[-1], chunks[1].content.split())

    def test_invalid_overlap(self):
        with self.assertRaises(ValueError):
            chunk_pages([Page(1, "text")], size=50, overlap=50)


if __name__ == "__main__":
    unittest.main()
