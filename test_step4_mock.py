"""
Mock test for Step 4 Edge Assembler.
Creates a small extractions.jsonl and verifies correct edge assembly.
"""

import json
import csv
import tempfile
from pathlib import Path

from step4_edge_assembler import EdgeAssembler, validate_extraction

# ─────────────────────────────────────────────────────────────────────────────
# Mock data: 3 jobs with various edge cases
# ─────────────────────────────────────────────────────────────────────────────

MOCK_EXTRACTIONS = [
    # Job 1: Multiple mentions of same skill → one edge; has credential
    {
        "job_id": "1370179",
        "skills": [
            {
                "mention_id": "mention:1370179:電腦技能資料:0:5:v0.1",
                "raw_mention": "Excel",
                "canonical_candidate": "skill:excel",
                "source_field": "電腦技能資料",
                "start_offset": 0,
                "end_offset": 5,
                "requirement_level": "unspecified",
                "assertion_status": "affirmed",
                "confidence": 1.0,
                "evidence": "Excel",
                "method": "structured",
                "extractor_version": "v0.1",
                "canonicalization_status": "accepted",
            },
            {
                "mention_id": "mention:1370179:職務內容:42:47:v0.1",
                "raw_mention": "Excel",
                "canonical_candidate": "skill:excel",
                "source_field": "職務內容",
                "start_offset": 42,
                "end_offset": 47,
                "requirement_level": "required",
                "assertion_status": "affirmed",
                "confidence": 0.85,
                "evidence": "需熟悉 Excel 報表",
                "method": "phrase",
                "extractor_version": "v0.1",
                "canonicalization_status": "accepted",
            },
            {
                "mention_id": "mention:1370179:職務內容:60:66:v0.1",
                "raw_mention": "Python",
                "canonical_candidate": "skill:python",
                "source_field": "職務內容",
                "start_offset": 60,
                "end_offset": 66,
                "requirement_level": "preferred",
                "assertion_status": "affirmed",
                "confidence": 0.9,
                "evidence": "熟悉 Python 佳",
                "method": "phrase",
                "extractor_version": "v0.1",
                "canonicalization_status": "accepted",
            },
        ],
        "credentials": [
            {
                "mention_id": "mention:1370179:專業證照:0:v0.1",
                "raw_mention": "會計師執照",
                "canonical_candidate": "credential:會計師執照",
                "source_field": "專業證照",
                "start_offset": None,
                "end_offset": None,
                "requirement_level": "required",
                "assertion_status": "affirmed",
                "confidence": 1.0,
                "evidence": "會計師執照",
                "method": "structured",
                "extractor_version": "v0.1",
                "canonicalization_status": "accepted",
            }
        ],
        "extraction_version": "v0.1",
    },
    # Job 2: Negated mention should NOT be materialized
    {
        "job_id": "1746140",
        "skills": [
            {
                "mention_id": "mention:1746140:職務內容:10:16:v0.1",
                "raw_mention": "Python",
                "canonical_candidate": "skill:python",
                "source_field": "職務內容",
                "start_offset": 10,
                "end_offset": 16,
                "requirement_level": "unspecified",
                "assertion_status": "negated",
                "confidence": 0.95,
                "evidence": "不需 Python 經驗",
                "method": "llm",
                "extractor_version": "v0.1",
                "canonicalization_status": "accepted",
            },
            {
                "mention_id": "mention:1746140:工作技能:0:3:v0.1",
                "raw_mention": "C++",
                "canonical_candidate": "skill:cpp",
                "source_field": "工作技能",
                "start_offset": 0,
                "end_offset": 3,
                "requirement_level": "required",
                "assertion_status": "affirmed",
                "confidence": 1.0,
                "evidence": "C++",
                "method": "structured",
                "extractor_version": "v0.1",
                "canonicalization_status": "accepted",
            },
        ],
        "credentials": [],
        "extraction_version": "v0.1",
    },
    # Job 3: Quarantined canonicalization should NOT be materialized
    {
        "job_id": "1888612",
        "skills": [
            {
                "mention_id": "mention:1888612:電腦技能資料:0:4:v0.1",
                "raw_mention": "Node",
                "canonical_candidate": "skill:nodejs",
                "source_field": "電腦技能資料",
                "start_offset": 0,
                "end_offset": 4,
                "requirement_level": "unspecified",
                "assertion_status": "affirmed",
                "confidence": 0.6,
                "evidence": "Node",
                "method": "structured",
                "extractor_version": "v0.1",
                "canonicalization_status": "quarantined",
            },
        ],
        "credentials": [],
        "extraction_version": "v0.1",
    },
]


def test_edge_assembly():
    """Test the assembler with mock data."""
    print("=" * 60)
    print("TEST: Step 4 Edge Assembler (mock)")
    print("=" * 60)

    # Write mock extractions to temp file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    ) as f:
        for record in MOCK_EXTRACTIONS:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        mock_path = Path(f.name)

    try:
        assembler = EdgeAssembler(
            extractions_path=mock_path,
            train_jobs_path=Path(__file__).parent / "graph" / "train_jobs.parquet",
            hierarchy_path=Path(__file__).parent / "graph" / "occupation_hierarchy.csv",
        )
        assembler.run()

        # Assertions
        print("\n  --- Verification ---")

        # 1. Excel: 2 mentions from job 1370179 → 1 edge, requirement=required (upgraded)
        excel_edges = [e for e in assembler.has_skill_edges if e["target_id"] == "skill:excel"]
        assert len(excel_edges) == 1, f"Expected 1 Excel edge, got {len(excel_edges)}"
        assert excel_edges[0]["requirement_level"] == "required", \
            f"Expected 'required', got '{excel_edges[0]['requirement_level']}'"
        assert excel_edges[0]["confidence"] == 1.0, \
            f"Expected max confidence 1.0, got {excel_edges[0]['confidence']}"
        assert excel_edges[0]["evidence_count"] == 2, \
            f"Expected 2 evidence refs, got {excel_edges[0]['evidence_count']}"
        print("  ✓ Excel: 2 mentions → 1 edge, requirement upgraded to 'required', confidence=max")

        # 2. Python in job 1370179: affirmed → should exist
        python_edges_1370179 = [
            e for e in assembler.has_skill_edges
            if e["target_id"] == "skill:python" and e["source_id"] == "job:1370179"
        ]
        assert len(python_edges_1370179) == 1
        print("  ✓ Python (job:1370179, affirmed): materialized")

        # 3. Python in job 1746140: negated → should NOT exist
        python_edges_1746140 = [
            e for e in assembler.has_skill_edges
            if e["target_id"] == "skill:python" and e["source_id"] == "job:1746140"
        ]
        assert len(python_edges_1746140) == 0, "Negated Python should not be materialized!"
        print("  ✓ Python (job:1746140, negated): correctly excluded")

        # 4. C++ in job 1746140: affirmed → should exist
        cpp_edges = [e for e in assembler.has_skill_edges if e["target_id"] == "skill:cpp"]
        assert len(cpp_edges) == 1
        print("  ✓ C++ (affirmed): materialized")

        # 5. Node.js in job 1888612: quarantined → should NOT exist
        node_edges = [e for e in assembler.has_skill_edges if e["target_id"] == "skill:nodejs"]
        assert len(node_edges) == 0, "Quarantined Node.js should not be materialized!"
        print("  ✓ Node.js (quarantined): correctly excluded")

        # 6. Credential edge
        cred_edges = assembler.requires_credential_edges
        assert len(cred_edges) == 1
        assert cred_edges[0]["target_id"] == "credential:會計師執照"
        print("  ✓ Credential (會計師執照): materialized")

        # 7. IN_OCCUPATION edges loaded from train_jobs
        assert len(assembler.in_occupation_edges) > 0
        print(f"  ✓ IN_OCCUPATION: {len(assembler.in_occupation_edges)} edges loaded")

        # 8. SUBCATEGORY_OF loaded
        assert len(assembler.subcategory_of_edges) > 0
        print(f"  ✓ SUBCATEGORY_OF: {len(assembler.subcategory_of_edges)} edges loaded")

        # Summary
        summary = assembler.summary()
        print(f"\n  Total HAS_SKILL: {summary['edge_counts']['HAS_SKILL']}")
        print(f"  Total REQUIRES_CREDENTIAL: {summary['edge_counts']['REQUIRES_CREDENTIAL']}")
        print(f"  Mentions processed: {summary['mention_stats']['total_processed']}")
        print(f"  Materialized: {summary['mention_stats']['materialized_edges']}")

        print("\n✓ All mock tests passed!")

    finally:
        mock_path.unlink()


def test_validation():
    """Test validation catches contract violations."""
    print("\n  --- Validation tests ---")

    # Missing job_id
    errors = validate_extraction({})
    assert "missing job_id" in errors
    print("  ✓ Catches missing job_id")

    # Missing evidence
    errors = validate_extraction({
        "job_id": "123",
        "skills": [{"mention_id": "m1", "canonical_candidate": "skill:x"}],
    })
    assert any("missing evidence" in e for e in errors)
    print("  ✓ Catches missing evidence")

    # Invalid confidence
    errors = validate_extraction({
        "job_id": "123",
        "skills": [{"mention_id": "m1", "canonical_candidate": "skill:x",
                    "evidence": "test", "confidence": 1.5}],
    })
    assert any("confidence" in e for e in errors)
    print("  ✓ Catches invalid confidence")

    print("  ✓ All validation tests passed!")


if __name__ == "__main__":
    test_validation()
    test_edge_assembly()
