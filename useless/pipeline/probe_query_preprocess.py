"""Probe query preprocess / resolve coverage after train-driven changes."""

from __future__ import annotations

import duckdb
import step8_retrieval_smoke as s8


def main() -> None:
    samples = [
        "司機",
        "清潔人員",
        "小貨車司機",
        "包裝員/作業員",
        "行政助理",
        "Python 資料分析",
        "門市，櫃檯，op",
        "麵包二手/////",
        "會計",
        "現領",
        "人員",
        "夜班保全",
        "作業員",
        "護理師",
        "大貨車司機",
    ]
    print("=== preprocess ===")
    for q in samples:
        p = s8.preprocess_query(q)
        print(f"{q!r:30} → {p.tokens}")

    idx = s8.GraphIndex()
    idx.load()
    print("\n=== resolve ===")
    for q in samples:
        r = s8.resolve_query(q, idx)
        print(
            f"{q!r:30} skills={r.resolved_skills} "
            f"occ={r.resolved_occupations} unresolved={r.unresolved_terms[:3]}"
        )

    con = duckdb.connect()
    rows = con.execute(
        """
        SELECT query, count(*) AS n
        FROM read_parquet('graph/eval_dataset/labeled_queries.parquet')
        WHERE data_split = 'train'
        GROUP BY 1
        """
    ).fetchall()

    skill = occ = both = none = 0
    total = 0
    for q, n in rows:
        r = s8.resolve_query(q, idx)
        total += n
        hs, ho = bool(r.resolved_skills), bool(r.resolved_occupations)
        if hs and ho:
            both += n
        elif hs:
            skill += n
        elif ho:
            occ += n
        else:
            none += n
    print(f"\n=== train weighted resolve ({total:,}) ===")
    print(f"  skill only: {skill:,} ({skill / total:.1%})")
    print(f"  occ only:   {occ:,} ({occ / total:.1%})")
    print(f"  both:       {both:,} ({both / total:.1%})")
    print(f"  none:       {none:,} ({none / total:.1%})")
    print(f"  any anchor: {(total - none) / total:.1%}")

    test_qs = [
        r[0]
        for r in con.execute(
            """
            SELECT query
            FROM read_parquet('graph/eval_dataset/labeled_queries.parquet')
            WHERE data_split = 'test'
            LIMIT 2000
            """
        ).fetchall()
    ]
    skill = occ = both = none = 0
    for q in test_qs:
        r = s8.resolve_query(q, idx)
        hs, ho = bool(r.resolved_skills), bool(r.resolved_occupations)
        if hs and ho:
            both += 1
        elif hs:
            skill += 1
        elif ho:
            occ += 1
        else:
            none += 1
    n = len(test_qs)
    print(f"\n=== test first-2000 resolve ===")
    print(f"  skill anchor: {skill + both} ({(skill + both) / n:.1%})")
    print(f"  occ any:      {occ + both} ({(occ + both) / n:.1%})")
    print(f"  none:         {none} ({none / n:.1%})")
    print(f"  any anchor:   {(n - none) / n:.1%}")


if __name__ == "__main__":
    main()
