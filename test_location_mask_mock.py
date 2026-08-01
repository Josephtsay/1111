"""
Mock tests for the query-time location allowlist (simple mask).

Covers the four required behaviours:
  1. a district code (CodeType=3) expands to its parent city (CodeType=2)
  2. an empty location request does not mask at all
  3. top_k is still filled after masking (mask runs before the slice)
  4. hard_with_fallback widens back when the masked pool is too thin

Run: python3 test_location_mask_mock.py
"""

import importlib.util
import sys
import tempfile
from pathlib import Path

import pandas as pd

# The working copy lives in a directory named "1111", which is not a valid
# Python identifier, so the package is loaded under its canonical alias.
_ROOT = Path(__file__).resolve().parent
if "job_skill_graph" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "job_skill_graph",
        _ROOT / "__init__.py",
        submodule_search_locations=[str(_ROOT)],
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["job_skill_graph"] = _module
    _spec.loader.exec_module(_module)

from job_skill_graph.location_mask import (  # noqa: E402
    LocationCodeTable,
    LocationMaskMode,
    apply_location_mask,
    effective_min_candidates,
    split_location_codes,
)
from job_skill_graph.retrieval import InMemorySkillGraph  # noqa: E402
from job_skill_graph.search_service import (  # noqa: E402
    SQLiteFTSSearchBackend,
    build_sqlite_search_index,
)

# ─────────────────────────────────────────────────────────────────────────────
# Mock city table: 3 cities (CodeType=2) + their districts (CodeType=3)
# + one nationwide code (CodeType=1), mirroring dataset/城市對照表.csv
# ─────────────────────────────────────────────────────────────────────────────

MOCK_CITY_ROWS = [
    {"CodeNo": "100000", "CodeNameA": "台灣", "CodeNameB": "台灣", "CodeType": "1"},
    {"CodeNo": "100100", "CodeNameA": "台北市", "CodeNameB": "台北市", "CodeType": "2"},
    {"CodeNo": "100200", "CodeNameA": "新北市", "CodeNameB": "新北市", "CodeType": "2"},
    {"CodeNo": "100400", "CodeNameA": "宜蘭縣", "CodeNameB": "宜蘭縣", "CodeType": "2"},
    {"CodeNo": "100101", "CodeNameA": "中正區", "CodeNameB": "台北市", "CodeType": "3"},
    {"CodeNo": "100105", "CodeNameA": "大安區", "CodeNameB": "台北市", "CodeType": "3"},
    {"CodeNo": "100201", "CodeNameA": "板橋區", "CodeNameB": "新北市", "CodeType": "3"},
]

TAIPEI = "100100"
NEW_TAIPEI = "100200"
YILAN = "100400"

# Mock graph: one skill, six jobs. graph_score == REQUIRES weight, so the
# unmasked ranking is J1 > J3 > J2 > J5 > J6 > J4.
MOCK_JOBS = [
    ("J1", TAIPEI, 1.00),
    ("J2", NEW_TAIPEI, 0.90),
    ("J3", YILAN, 0.95),
    ("J4", TAIPEI, 0.50),
    ("J5", YILAN, 0.80),
    ("J6", NEW_TAIPEI, 0.70),
]
UNMASKED_ORDER = ["J1", "J3", "J2", "J5", "J6", "J4"]


def mock_table() -> LocationCodeTable:
    return LocationCodeTable(MOCK_CITY_ROWS)


def mock_graph(*, with_location: bool = True) -> InMemorySkillGraph:
    nodes = [
        {
            "node_id": "skill:python",
            "label": "Skill",
            "canonical_name": "python",
            "job_id": None,
            "location_code": None,
        }
    ]
    edges = []
    for job_id, location_code, weight in MOCK_JOBS:
        nodes.append(
            {
                "node_id": f"job:{job_id}",
                "label": "Job",
                "canonical_name": None,
                "job_id": job_id,
                "location_code": location_code,
            }
        )
        edges.append(
            {
                "from_id": f"job:{job_id}",
                "to_id": "skill:python",
                "label": "REQUIRES",
                "weight": weight,
            }
        )
    node_frame = pd.DataFrame(nodes)
    if not with_location:
        node_frame = node_frame.drop(columns=["location_code"])
    return InMemorySkillGraph(
        node_frame,
        pd.DataFrame(edges),
        location_table=mock_table(),
    )


def job_ids(candidates) -> list[str]:
    return [candidate.job_id for candidate in candidates]


def graph_from_spec(spec: list[tuple[str, str, int, float]]) -> InMemorySkillGraph:
    """Build a one-skill graph from ``(prefix, location_code, count, weight)``."""

    nodes = [
        {
            "node_id": "skill:python",
            "label": "Skill",
            "canonical_name": "python",
            "job_id": None,
            "location_code": None,
        }
    ]
    edges = []
    for prefix, location_code, count, weight in spec:
        for index in range(count):
            job_id = f"{prefix}{index:02d}"
            nodes.append(
                {
                    "node_id": f"job:{job_id}",
                    "label": "Job",
                    "canonical_name": None,
                    "job_id": job_id,
                    "location_code": location_code,
                }
            )
            edges.append(
                {
                    "from_id": f"job:{job_id}",
                    "to_id": "skill:python",
                    "label": "REQUIRES",
                    "weight": weight,
                }
            )
    return InMemorySkillGraph(
        pd.DataFrame(nodes), pd.DataFrame(edges), location_table=mock_table()
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. District -> city expansion
# ─────────────────────────────────────────────────────────────────────────────


def test_district_expands_to_parent_city():
    table = mock_table()
    mask = table.build_mask(["100101"])
    print(f"district 100101 -> {mask.codes}")
    assert mask.active
    assert mask.codes == (TAIPEI,), mask.codes
    assert mask.rollups == (("100101", TAIPEI),)
    # A district code must never survive as itself, otherwise exact-matching
    # city-level job locations filters out every row.
    assert "100101" not in mask.allowed
    assert mask.allows(TAIPEI)
    assert not mask.allows(NEW_TAIPEI)


def test_city_code_passes_through_and_codes_union():
    table = mock_table()
    mask = table.build_mask(["100101", "100201", NEW_TAIPEI, "100105"])
    print(f"union -> {mask.codes}")
    # 中正區 + 大安區 -> 台北市; 板橋區 + 新北市 -> 新北市 (deduped union)
    assert mask.codes == (TAIPEI, NEW_TAIPEI), mask.codes


def test_nationwide_code_disables_mask():
    mask = mock_table().build_mask(["100000"])
    print(f"nationwide -> active={mask.active}")
    assert not mask.active
    assert mask.allowed is None
    assert mask.nationwide == ("100000",)
    assert mask.allows(YILAN)


def test_unknown_code_falls_back_to_numeric_parent():
    # Not in the mock table at all; the 6-digit prefix rule still rolls it up.
    mask = mock_table().build_mask(["100107"])
    print(f"unknown 100107 -> {mask.codes}")
    assert mask.codes == (TAIPEI,), mask.codes


def test_comma_joined_codes_are_split():
    # The raw search log stores c0 as "100100,100200,100900".
    assert split_location_codes(["100101,100201"]) == ["100101", "100201"]
    mask = mock_table().build_mask("100101,100201")
    print(f"comma joined -> {mask.codes}")
    assert mask.codes == (TAIPEI, NEW_TAIPEI), mask.codes


def test_real_city_table_rolls_up_every_district():
    path = _ROOT / "dataset" / "城市對照表.csv"
    if not path.is_file():
        print("skipped: dataset/城市對照表.csv not present")
        return
    table = LocationCodeTable.load(path)
    districts = [
        code for code, code_type in table.code_types.items() if code_type == "3"
    ]
    unresolved = [
        code for code in districts if table.build_mask([code]).unresolved
    ]
    print(f"real table: {len(districts)} districts, {len(unresolved)} unresolved")
    assert districts, "expected CodeType=3 rows in the real table"
    assert not unresolved, unresolved[:10]
    # 中正區 -> 台北市
    assert table.build_mask(["100101"]).codes == ("100100",)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Empty location -> no mask
# ─────────────────────────────────────────────────────────────────────────────


def test_empty_location_does_not_mask():
    table = mock_table()
    for value in ([], (), None, "", ["  "]):
        mask = table.build_mask(value)
        assert mask.allowed is None, value
        assert not mask.active, value
    print("empty location -> allowed=None for all empty spellings")


def test_graph_retrieve_without_location_is_unchanged():
    graph = mock_graph()
    baseline = job_ids(graph.retrieve("python", top_k=10))
    print(f"no location -> {baseline}")
    assert baseline == UNMASKED_ORDER, baseline
    # Passing empty codes explicitly must be identical to passing nothing.
    assert job_ids(graph.retrieve("python", top_k=10, location_codes=[])) == baseline
    assert job_ids(graph.retrieve("python", top_k=10, location_codes=())) == baseline


def test_graph_retrieve_without_location_metadata_ignores_mask():
    # A graph whose nodes carry no location_code must not be filtered to nothing.
    graph = mock_graph(with_location=False)
    assert not graph.has_location_metadata
    result = job_ids(
        graph.retrieve("python", top_k=10, location_codes=["100101"])
    )
    print(f"no location metadata -> {result}")
    assert result == UNMASKED_ORDER, result


# ─────────────────────────────────────────────────────────────────────────────
# 3. Mask applies before [:top_k], so the page still fills
# ─────────────────────────────────────────────────────────────────────────────


def test_graph_mask_applied_before_top_k():
    graph = mock_graph()
    # 中正區 -> 台北市. J4 (score 0.50) is rank 6 unmasked, so it can only
    # appear at top_k=2 if the mask ran before the slice.
    result = job_ids(
        graph.retrieve(
            "python",
            top_k=2,
            location_codes=["100101"],
            location_mode=LocationMaskMode.HARD,
        )
    )
    print(f"mask=台北市 top_k=2 -> {result}")
    assert result == ["J1", "J4"], result
    assert "J3" not in result, "out-of-region job leaked into the candidate set"


def test_graph_mask_operates_on_full_scores_not_a_truncated_pool():
    """Regression guard for the ordering invariant.

    60 out-of-region jobs all outscore every in-region job. The mask must be
    applied to the complete ``scores`` dict, so any implementation that
    truncates first (``pool[:top_k]`` then mask, or masking an already-sliced
    candidate_pool) returns an empty list here instead of the 3 in-region jobs.
    """

    nodes = [
        {
            "node_id": "skill:python",
            "label": "Skill",
            "canonical_name": "python",
            "job_id": None,
            "location_code": None,
        }
    ]
    edges = []
    # Out-of-region, high scoring: these monopolise every prefix of the ranking.
    for index in range(60):
        job_id = f"OUT{index:02d}"
        nodes.append(
            {
                "node_id": f"job:{job_id}",
                "label": "Job",
                "canonical_name": None,
                "job_id": job_id,
                "location_code": YILAN,
            }
        )
        edges.append(
            {
                "from_id": f"job:{job_id}",
                "to_id": "skill:python",
                "label": "REQUIRES",
                "weight": 0.90,
            }
        )
    # In-region, low scoring: unreachable unless the mask ran on the full pool.
    for index in range(3):
        job_id = f"IN{index}"
        nodes.append(
            {
                "node_id": f"job:{job_id}",
                "label": "Job",
                "canonical_name": None,
                "job_id": job_id,
                "location_code": TAIPEI,
            }
        )
        edges.append(
            {
                "from_id": f"job:{job_id}",
                "to_id": "skill:python",
                "label": "REQUIRES",
                "weight": 0.10,
            }
        )
    graph = InMemorySkillGraph(
        pd.DataFrame(nodes), pd.DataFrame(edges), location_table=mock_table()
    )

    baseline = job_ids(graph.retrieve("python", top_k=3))
    assert all(job_id.startswith("OUT") for job_id in baseline), baseline

    result = job_ids(
        graph.retrieve(
            "python",
            top_k=3,
            location_codes=["100101"],
            location_mode=LocationMaskMode.HARD,
        )
    )
    print(f"60 high-scoring out-of-region jobs, top_k=3 -> {result}")
    assert result == ["IN0", "IN1", "IN2"], result

    # Also holds for the pool size HybridRerankBackend actually requests.
    pooled = job_ids(
        graph.retrieve(
            "python",
            top_k=200,
            location_codes=["100101"],
            location_mode=LocationMaskMode.HARD,
        )
    )
    assert pooled == ["IN0", "IN1", "IN2"], pooled


def test_graph_mask_still_fills_top_k():
    graph = mock_graph()
    result = job_ids(
        graph.retrieve(
            "python",
            top_k=3,
            location_codes=["100101", "100201"],
            location_mode=LocationMaskMode.HARD,
        )
    )
    print(f"mask=台北市+新北市 top_k=3 -> {result}")
    assert len(result) == 3, result
    assert result == ["J1", "J2", "J6"], result
    allowed = {TAIPEI, NEW_TAIPEI}
    assert all(graph.job_location_by_id[job_id] in allowed for job_id in result)


def test_graph_hard_mask_can_return_empty():
    # Pure "hard" never widens, even when nothing matches.
    graph = mock_graph()
    result = job_ids(
        graph.retrieve(
            "python",
            top_k=10,
            location_codes=["999999"],
            location_mode=LocationMaskMode.HARD,
        )
    )
    print(f"hard mask, no match -> {result}")
    assert result == [], result


# ─────────────────────────────────────────────────────────────────────────────
# 4. hard_with_fallback
# ─────────────────────────────────────────────────────────────────────────────


def test_graph_fallback_widens_thin_pool():
    graph = mock_graph()
    # Only J1 and J4 sit in 台北市; asking for 5 minimum trips the fallback.
    widened = job_ids(
        graph.retrieve(
            "python",
            top_k=10,
            location_codes=["100101"],
            location_mode=LocationMaskMode.HARD_WITH_FALLBACK,
            location_min_candidates=5,
        )
    )
    print(f"fallback (min=5) -> {widened}")
    assert widened == UNMASKED_ORDER, widened

    # With a reachable threshold the mask holds.
    kept = job_ids(
        graph.retrieve(
            "python",
            top_k=10,
            location_codes=["100101"],
            location_mode=LocationMaskMode.HARD_WITH_FALLBACK,
            location_min_candidates=2,
        )
    )
    print(f"no fallback (min=2) -> {kept}")
    assert kept == ["J1", "J4"], kept


def test_fallback_floor_is_clamped_to_top_k():
    """min_candidates is a quality floor for large pools, never above top_k.

    8 in-region jobs, floor 10. Asking for 5 must NOT fall back: the masked pool
    already fills the page. Asking for 20 SHOULD fall back: 8 is genuinely below
    the quality floor at that pool size.
    """

    graph = graph_from_spec(
        [("IN", TAIPEI, 8, 0.10), ("OUT", YILAN, 12, 0.90)]
    )

    kept = job_ids(
        graph.retrieve(
            "python",
            top_k=5,
            location_codes=["100101"],
            location_mode=LocationMaskMode.HARD_WITH_FALLBACK,
            location_min_candidates=10,
        )
    )
    print(f"top_k=5, min=10, in-region=8 -> {kept}")
    assert len(kept) == 5, kept
    assert all(job_id.startswith("IN") for job_id in kept), kept

    widened = job_ids(
        graph.retrieve(
            "python",
            top_k=20,
            location_codes=["100101"],
            location_mode=LocationMaskMode.HARD_WITH_FALLBACK,
            location_min_candidates=10,
        )
    )
    print(f"top_k=20, min=10, in-region=8 -> fell back to {len(widened)} jobs")
    assert len(widened) == 20, widened
    assert any(job_id.startswith("OUT") for job_id in widened), widened

    # Exactly at the boundary: top_k=8 -> floor 8, matched 8, no fallback.
    boundary = job_ids(
        graph.retrieve(
            "python",
            top_k=8,
            location_codes=["100101"],
            location_mode=LocationMaskMode.HARD_WITH_FALLBACK,
            location_min_candidates=10,
        )
    )
    assert len(boundary) == 8 and all(j.startswith("IN") for j in boundary), boundary
    # One past the boundary: top_k=9 -> floor 9, matched 8, fallback.
    past = job_ids(
        graph.retrieve(
            "python",
            top_k=9,
            location_codes=["100101"],
            location_mode=LocationMaskMode.HARD_WITH_FALLBACK,
            location_min_candidates=10,
        )
    )
    assert any(j.startswith("OUT") for j in past), past


def test_effective_min_candidates_clamp():
    assert effective_min_candidates(10, 5) == 5
    assert effective_min_candidates(10, 50) == 10
    assert effective_min_candidates(10, 10) == 10
    assert effective_min_candidates(0, 50) == 0
    assert effective_min_candidates(10, 0) == 0
    assert effective_min_candidates(10, -1) == 0
    print("effective_min_candidates clamps to min(floor, requested)")


def test_fts_fallback_floor_is_clamped_to_limit():
    with tempfile.TemporaryDirectory() as tmp:
        # location_boost=0 so ranking is pure bm25: if the fallback fires, the
        # terse out-of-region F1/F2 take the top slots and the assertion fails.
        # With the boost left on, both paths would surface F3/F4 and this test
        # would not discriminate.
        backend = build_fts_backend(
            Path(tmp),
            location_mode="hard_with_fallback",
            location_min_candidates=3,
            location_boost=0.0,
        )
        # 台北市 holds F3+F4. limit=2 clamps the floor from 3 to 2, so the two
        # in-region jobs are enough and the mask holds.
        kept = backend.search("python", location_codes=["100101"], limit=2)
        found = sorted(item.job_id for item in kept)
        print(f"fts limit=2, min=3, in-region=2 -> {found}")
        assert found == ["F3", "F4"], found
        # limit=10 keeps the floor at 3, so 2 in-region jobs trip the fallback.
        widened = backend.search("python", location_codes=["100101"], limit=10)
        assert len(widened) == 5, [item.job_id for item in widened]
        assert sorted(item.job_id for item in widened[:2]) == ["F1", "F2"]


def test_soft_and_off_modes_do_not_filter():
    graph = mock_graph()
    for mode in (LocationMaskMode.SOFT, LocationMaskMode.OFF):
        result = job_ids(
            graph.retrieve(
                "python", top_k=10, location_codes=["100101"], location_mode=mode
            )
        )
        assert result == UNMASKED_ORDER, (mode, result)
    print("soft/off modes leave the candidate pool untouched")


def test_apply_location_mask_helper_reports_outcome():
    mask = mock_table().build_mask(["100101"])
    items = [job_id for job_id, _, _ in MOCK_JOBS]
    locations = {job_id: code for job_id, code, _ in MOCK_JOBS}

    masked = apply_location_mask(
        items, locations.get, mask, mode="hard", min_candidates=10
    )
    assert masked.applied and masked.as_list() == ["J1", "J4"]
    assert masked.reason == "masked"

    fallen_back = apply_location_mask(
        items, locations.get, mask, mode="hard_with_fallback", min_candidates=10
    )
    assert not fallen_back.applied
    assert fallen_back.reason == "fallback_thin_pool"
    assert fallen_back.matched == 2 and fallen_back.considered == 6
    assert fallen_back.as_list() == items
    print("apply_location_mask reports applied/reason/matched correctly")


# ─────────────────────────────────────────────────────────────────────────────
# FTS backend: narrowing happens in SQL, before LIMIT
# ─────────────────────────────────────────────────────────────────────────────

# Out-of-region jobs get terse text (better bm25); in-region jobs get padded
# text (worse bm25). Without a mask the out-of-region jobs win the top slots.
FTS_JOBS = [
    ("F1", YILAN, "python"),
    ("F2", YILAN, "python"),
    ("F3", TAIPEI, "python " + "helper text " * 60),
    ("F4", TAIPEI, "python " + "helper text " * 60),
    ("F5", NEW_TAIPEI, "python " + "helper text " * 60),
]


def build_fts_backend(directory: Path, **kwargs) -> SQLiteFTSSearchBackend:
    frame = pd.DataFrame(
        [
            {
                "job_id": job_id,
                "title": text,
                "description": text,
                "requirements": "",
                "location_code": location_code,
                "occupation_code": "2001001",
                "posted_at": "2026-06-01T00:00:00",
                "salary_min": 0.0,
                "salary_max": 0.0,
            }
            for job_id, location_code, text in FTS_JOBS
        ]
    )
    parquet_path = directory / "jobs.parquet"
    frame.to_parquet(parquet_path, index=False)
    index_path = directory / f"index_{len(list(directory.glob('*.sqlite')))}.sqlite"
    build_sqlite_search_index(parquet_path, index_path)
    kwargs.setdefault("location_table", mock_table())
    return SQLiteFTSSearchBackend(index_path, **kwargs)


def test_fts_without_location_returns_everything():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_fts_backend(Path(tmp))
        results = backend.search("python", limit=10)
        found = sorted(item.job_id for item in results)
        print(f"fts no location -> {found}")
        assert found == ["F1", "F2", "F3", "F4", "F5"], found
        assert all(item.location_match == 0.0 for item in results)


def test_fts_hard_mask_narrows_before_limit():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_fts_backend(Path(tmp), location_mode="hard")
        unmasked = [item.job_id for item in backend.search("python", limit=2)]
        print(f"fts unmasked limit=2 -> {unmasked}")
        assert set(unmasked) == {"F1", "F2"}, unmasked

        masked = backend.search("python", location_codes=["100101"], limit=2)
        found = sorted(item.job_id for item in masked)
        print(f"fts mask=台北市 limit=2 -> {found}")
        # F3/F4 rank below F1/F2 lexically, so they can only surface if the
        # SQL narrowed the candidate set before LIMIT.
        assert found == ["F3", "F4"], found
        assert all(item.location_match == 1.0 for item in masked)


def test_fts_hard_mask_excludes_out_of_region():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_fts_backend(Path(tmp), location_mode="hard")
        masked = backend.search("python", location_codes=["100201"], limit=10)
        found = sorted(item.job_id for item in masked)
        print(f"fts mask=新北市 -> {found}")
        assert found == ["F5"], found

        nothing = backend.search("python", location_codes=["999999"], limit=10)
        print(f"fts hard mask, no match -> {[i.job_id for i in nothing]}")
        assert nothing == []


def test_fts_fallback_widens_thin_pool():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_fts_backend(
            Path(tmp), location_mode="hard_with_fallback", location_min_candidates=3
        )
        # 新北市 holds a single job, below the floor of 3 -> widen to nationwide.
        widened = backend.search("python", location_codes=["100201"], limit=10)
        found = sorted(item.job_id for item in widened)
        print(f"fts fallback -> {found}")
        assert found == ["F1", "F2", "F3", "F4", "F5"], found
        # The soft boost still ranks the in-region job first after fallback.
        assert widened[0].job_id == "F5", [i.job_id for i in widened]

        # 台北市 holds two jobs; still below 3, so it also widens.
        taipei = backend.search("python", location_codes=["100101"], limit=10)
        assert len(taipei) == 5, [i.job_id for i in taipei]


def test_fts_nationwide_code_behaves_like_no_location():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_fts_backend(Path(tmp), location_mode="hard")
        baseline = sorted(item.job_id for item in backend.search("python", limit=10))
        nationwide = sorted(
            item.job_id
            for item in backend.search(
                "python", location_codes=["100000"], limit=10
            )
        )
        print(f"fts nationwide -> {nationwide}")
        assert nationwide == baseline, (nationwide, baseline)


TESTS = [
    test_district_expands_to_parent_city,
    test_city_code_passes_through_and_codes_union,
    test_nationwide_code_disables_mask,
    test_unknown_code_falls_back_to_numeric_parent,
    test_comma_joined_codes_are_split,
    test_real_city_table_rolls_up_every_district,
    test_empty_location_does_not_mask,
    test_graph_retrieve_without_location_is_unchanged,
    test_graph_retrieve_without_location_metadata_ignores_mask,
    test_graph_mask_applied_before_top_k,
    test_graph_mask_operates_on_full_scores_not_a_truncated_pool,
    test_graph_mask_still_fills_top_k,
    test_graph_hard_mask_can_return_empty,
    test_graph_fallback_widens_thin_pool,
    test_fallback_floor_is_clamped_to_top_k,
    test_effective_min_candidates_clamp,
    test_fts_fallback_floor_is_clamped_to_limit,
    test_soft_and_off_modes_do_not_filter,
    test_apply_location_mask_helper_reports_outcome,
    test_fts_without_location_returns_everything,
    test_fts_hard_mask_narrows_before_limit,
    test_fts_hard_mask_excludes_out_of_region,
    test_fts_fallback_widens_thin_pool,
    test_fts_nationwide_code_behaves_like_no_location,
]


if __name__ == "__main__":
    for test in TESTS:
        print(f"\n── {test.__name__}")
        test()
    print(f"\nAll {len(TESTS)} location mask tests passed.")
