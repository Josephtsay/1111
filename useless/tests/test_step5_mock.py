"""
Mock test for Step 5 Statistical Edges.
Verifies NPMI, conditional probability, occupation aggregation logic.
"""

import json
import math
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "pipeline") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "pipeline"))

from step5_statistical_edges import (  # noqa: E402
    StatisticalConfig,
    StatisticalEdgeBuilder,
    compute_co_occurrence,
    compute_core_skills,
    compute_global_job_frequency,
    is_statistically_eligible,
)


def test_eligibility_filter():
    """Test the statistical_eligible policy."""
    print("=== Test: statistical eligibility ===")
    config = StatisticalConfig()

    # Eligible: affirmed, accepted, structured, high confidence
    m1 = {
        "assertion_status": "affirmed",
        "canonicalization_status": "accepted",
        "confidence": 1.0,
        "method": "structured",
        "requirement_level": "required",
        "source_field": "電腦技能資料",
        "canonical_candidate": "skill:python",
    }
    assert is_statistically_eligible(m1, config), "Should be eligible"
    print("  ✓ affirmed + accepted + required + structured = eligible")

    # Eligible: unspecified but from structured field
    m2 = {**m1, "requirement_level": "unspecified", "source_field": "電腦技能資料"}
    assert is_statistically_eligible(m2, config)
    print("  ✓ unspecified + 電腦技能資料 = eligible")

    # NOT eligible: unspecified from non-structured field
    m3 = {**m1, "requirement_level": "unspecified", "source_field": "職務內容"}
    assert not is_statistically_eligible(m3, config)
    print("  ✓ unspecified + 職務內容 = NOT eligible")

    # NOT eligible: negated
    m4 = {**m1, "assertion_status": "negated"}
    assert not is_statistically_eligible(m4, config)
    print("  ✓ negated = NOT eligible")

    # NOT eligible: quarantined
    m5 = {**m1, "canonicalization_status": "quarantined"}
    assert not is_statistically_eligible(m5, config)
    print("  ✓ quarantined = NOT eligible")

    # NOT eligible: low confidence
    m6 = {**m1, "confidence": 0.3}
    assert not is_statistically_eligible(m6, config)
    print("  ✓ low confidence = NOT eligible")

    # NOT eligible: credential
    m7 = {**m1, "canonical_candidate": "credential:護理師"}
    assert not is_statistically_eligible(m7, config)
    print("  ✓ credential = NOT eligible")

    # NOT eligible: blacklisted
    config_bl = StatisticalConfig(soft_skill_blacklist={"skill:communication"})
    m8 = {**m1, "canonical_candidate": "skill:communication"}
    assert not is_statistically_eligible(m8, config_bl)
    print("  ✓ blacklisted = NOT eligible")


def test_co_occurrence():
    """Test NPMI and conditional probability computation."""
    print("\n=== Test: co-occurrence ===")

    # 10 jobs, skills distributed to test NPMI
    job_skill_sets = {
        "j1": {"skill:python", "skill:django"},
        "j2": {"skill:python", "skill:django"},
        "j3": {"skill:python", "skill:django"},
        "j4": {"skill:python", "skill:django"},
        "j5": {"skill:python", "skill:django"},
        "j6": {"skill:python", "skill:react"},
        "j7": {"skill:python", "skill:react"},
        "j8": {"skill:react", "skill:javascript"},
        "j9": {"skill:react", "skill:javascript"},
        "j10": {"skill:excel"},
    }

    config = StatisticalConfig(min_count=2, min_npmi=-1.0, top_n_per_skill=100)
    edges = compute_co_occurrence(job_skill_sets, config)

    # python-django: co-occurs 5 times, python appears in 7, django in 5
    pd_edge = next((e for e in edges if
                    {e["source_id"], e["target_id"]} == {"skill:python", "skill:django"}), None)
    assert pd_edge is not None, "python-django edge should exist"
    assert pd_edge["count"] == 5
    assert pd_edge["p_b_given_a"] == round(5 / 7, 4) or pd_edge["p_a_given_b"] == round(5 / 7, 4)
    print(f"  ✓ python-django: count=5, NPMI={pd_edge['npmi']}")

    # react-javascript: co-occurs 2 times
    rj_edge = next((e for e in edges if
                    {e["source_id"], e["target_id"]} == {"skill:react", "skill:javascript"}), None)
    assert rj_edge is not None
    assert rj_edge["count"] == 2
    print(f"  ✓ react-javascript: count=2, NPMI={rj_edge['npmi']}")

    # Verify NPMI formula manually for python-django
    # P(A,B) = 5/10 = 0.5, P(A) = 7/10, P(B) = 5/10
    # PMI = log2(0.5 / (0.7 * 0.5)) = log2(0.5/0.35) = log2(1.4286)
    # NPMI = PMI / (-log2(0.5))
    p_ab = 5 / 10
    p_a = 7 / 10
    p_b = 5 / 10
    pmi = math.log2(p_ab / (p_a * p_b))
    npmi = pmi / (-math.log2(p_ab))
    assert abs(pd_edge["npmi"] - round(npmi, 4)) < 0.001, f"NPMI mismatch: {pd_edge['npmi']} vs {round(npmi, 4)}"
    print(f"  ✓ NPMI formula verified: {round(npmi, 4)}")

    # Excel appears alone → no co-occurrence edges
    excel_edges = [e for e in edges if "skill:excel" in (e["source_id"], e["target_id"])]
    assert len(excel_edges) == 0
    print("  ✓ Isolated skill (excel) has no co-occurrence edges")


def test_core_skills():
    """Test occupation core skill aggregation."""
    print("\n=== Test: core skills ===")

    job_skill_sets = {
        "j1": {"skill:python", "skill:sql"},
        "j2": {"skill:python"},
        "j3": {"skill:python", "skill:java"},
        "j4": {"skill:excel"},
        "j5": {"skill:excel", "skill:word"},
    }

    # j1, j2, j3 → occ:140201 (minor under 140200)
    # j4, j5 → occ:110206 (minor under 110200)
    job_occupation = {
        "j1": "140201",
        "j2": "140201",
        "j3": "140201",
        "j4": "110206",
        "j5": "110206",
    }

    hierarchy = [
        {"child_code": "140201", "parent_code": "140200"},
        {"child_code": "140200", "parent_code": "140000"},
        {"child_code": "110206", "parent_code": "110200"},
        {"child_code": "110200", "parent_code": "110000"},
    ]

    config = StatisticalConfig()
    edges = compute_core_skills(job_skill_sets, job_occupation, hierarchy, config, min_rate=0.01)

    # occ:140201 (minor, direct): python appears in 3/3 = 1.0
    python_140201 = next((e for e in edges if
                          e["source_id"] == "occ:140201" and e["target_id"] == "skill:python"), None)
    assert python_140201 is not None
    assert python_140201["rate"] == 1.0
    assert python_140201["aggregation_scope"] == "direct"
    assert python_140201["job_count"] == 3
    print(f"  ✓ occ:140201 core python: rate=1.0, scope=direct, jobs=3")

    # occ:140200 (middle, descendants): includes j1,j2,j3 → python 3/3
    python_140200 = next((e for e in edges if
                          e["source_id"] == "occ:140200" and e["target_id"] == "skill:python"), None)
    assert python_140200 is not None
    assert python_140200["aggregation_scope"] == "descendants"
    assert python_140200["job_count"] == 3
    print(f"  ✓ occ:140200 core python: rate={python_140200['rate']}, scope=descendants")

    # occ:110206 (minor): excel 2/2 = 1.0
    excel_110206 = next((e for e in edges if
                         e["source_id"] == "occ:110206" and e["target_id"] == "skill:excel"), None)
    assert excel_110206 is not None
    assert excel_110206["rate"] == 1.0
    print(f"  ✓ occ:110206 core excel: rate=1.0")


def test_global_job_frequency():
    """Test global_job_frequency computation."""
    print("\n=== Test: global_job_frequency ===")

    job_skill_sets = {
        "j1": {"skill:python"},
        "j2": {"skill:python"},
        "j3": {"skill:python"},
        "j4": {"skill:excel"},
    }
    total = 10  # More jobs exist but only 4 have skills

    freq = compute_global_job_frequency(job_skill_sets, total)
    assert freq["skill:python"] == 0.3  # 3/10
    assert freq["skill:excel"] == 0.1   # 1/10
    print(f"  ✓ python: {freq['skill:python']}, excel: {freq['skill:excel']}")


if __name__ == "__main__":
    test_eligibility_filter()
    test_co_occurrence()
    test_core_skills()
    test_global_job_frequency()
    print("\n✓ All Step 5 mock tests passed!")
