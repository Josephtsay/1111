"""
A0b — LLM bake-off 執行與評分（Gate 1）
Role: A (Content Graph)
Playbook: §Model Decision Contract — Gate 1 用 golden slice 比較候選模型，Gate 2 才 freeze

本腳本只讀 §3.4 契約來「驗證」模型輸出，不修改契約、不改 confidence / assertion 政策、
不產生任何進圖的 artifact。LLM 抽取結果要進圖必須另走 Gate 2 freeze 與 canonicalization。

前置：
  python step1_train_data_prep.py        # golden slice
  python step_a0_llm_bakeoff_prep.py     # graph/llm_bakeoff_v0.1/prompts.jsonl

模式：
  --mode mock      不呼叫 API，用合成回應驗證評分管線（零成本）
  --mode dry-run   只算 prompt / token / request 形狀與成本預估，不送出
  --mode live      真的呼叫 Bedrock（會產生費用）

指標分兩類，不混為一談：
  A. 有 ground truth（fixtures/assertion_challenge_set_v0.1.jsonl，48 筆）
     assertion_accuracy / requirement_accuracy
  B. 無人工 mention 標註 → 只報契約與 grounding 代理指標（不謊稱 precision/recall）
     strict_json_success_rate, contract_valid_rate, evidence_grounded_rate,
     offset_exact_rate, credential_routing_ok_rate, hallucinated_mention_rate,
     protected_pair_violations, structured_agreement_rate（recall 代理）,
     novel_mentions（增益代理）
  C. 成本 / 效能：latency p50/p95、tokens、throughput、每筆成本（需 --price-*）

輸出：graph/llm_bakeoff_v0.1/runs/<candidate_id>/{responses.jsonl,report.json}
      graph/llm_bakeoff_v0.1/bakeoff_summary_<mode>.json
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import llm_client

ROOT = Path(__file__).parent
GRAPH_DIR = ROOT / "graph"
BAKEOFF_DIR = GRAPH_DIR / "llm_bakeoff_v0.1"
PROMPTS = BAKEOFF_DIR / "prompts.jsonl"
CHALLENGE_SET = ROOT / "fixtures" / "assertion_challenge_set_v0.1.jsonl"
EXTRACTIONS_STRUCTURED = GRAPH_DIR / "extractions_structured.jsonl"
EXTRACTIONS_PHRASE = GRAPH_DIR / "extractions_phrase.jsonl"

PROMPT_VERSION = "llm_extraction_v0.1"
RESPONSE_SCHEMA_VERSION = "extraction_contract_v0.1"

# 唯讀鏡像 §3.4 的 enum（用於驗證，不定義政策）
VALID_REQUIREMENT = {"required", "preferred", "unspecified"}
VALID_ASSERTION = {"affirmed", "negated", "uncertain"}
VALID_SOURCE_FIELDS = {"職務名稱", "職務內容", "附加條件"}

# protected pairs（§Step 3）：不可被合併到同一 canonical
PROTECTED_PAIRS = [
    ("java", "javascript"), ("c", "c++"), ("c", "c#"), ("c++", "c#"),
    ("react", "react native"), ("sql", "mysql"), ("aws", "azure"),
    ("tensorflow", "pytorch"), ("node", "node.js"),
]

CREDENTIAL_KEYWORDS = re.compile(r"證照|執照|檢定|技術士|考試及格|資格證")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip().casefold()


# ─────────────────────────────────────────────────────────────────────────────
# JSON 解析（limited repair only，對齊 extraction_config.retry.repair）
# ─────────────────────────────────────────────────────────────────────────────
def parse_strict_json(text: str) -> tuple[dict | None, bool, str]:
    """回傳 (obj, strict_ok, repair_note)；strict_ok 表示不需修補就是合法 JSON。"""
    raw = (text or "").strip()
    try:
        obj = json.loads(raw)
        return (obj if isinstance(obj, dict) else None), isinstance(obj, dict), ""
    except Exception:
        pass
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj, False, "stripped_code_fence"
    except Exception:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(cleaned[start : end + 1])
            if isinstance(obj, dict):
                return obj, False, "extracted_brace_block"
        except Exception:
            pass
    return None, False, "unparseable"


# ─────────────────────────────────────────────────────────────────────────────
# 契約 + grounding 評分
# ─────────────────────────────────────────────────────────────────────────────
def score_record(
    obj: dict,
    source_fields: dict[str, str],
    phrase_skills: set[str],
    structured_skills: set[str],
) -> dict[str, Any]:
    res: dict[str, Any] = {
        "contract_valid": True, "contract_errors": [],
        "n_skills": 0, "n_credentials": 0,
        "evidence_checked": 0, "evidence_grounded": 0,
        "offset_checked": 0, "offset_exact": 0,
        "credential_routing_ok": True, "hallucinated": 0,
        "assertion_counts": {}, "requirement_counts": {},
        "protected_pair_violations": [],
        "matched_phrase": 0, "novel_vs_phrase": 0,
        "matched_structured": 0, "llm_skill_keys": [],
    }

    skills = obj.get("skills")
    creds = obj.get("credentials")
    if not isinstance(skills, list):
        res["contract_valid"] = False
        res["contract_errors"].append("skills_not_list")
        skills = []
    if not isinstance(creds, list):
        res["contract_valid"] = False
        res["contract_errors"].append("credentials_not_list")
        creds = []
    res["n_skills"] = len(skills)
    res["n_credentials"] = len(creds)

    # 明確標記來源清單，避免用 dict 相等性判斷歸屬
    tagged = [(True, m) for m in skills] + [(False, m) for m in creds]

    for is_skill, m in tagged:
        if not isinstance(m, dict):
            res["contract_valid"] = False
            res["contract_errors"].append("mention_not_object")
            continue

        for field in ("mention_id", "raw_mention", "source_field", "evidence", "method"):
            if not m.get(field):
                res["contract_valid"] = False
                res["contract_errors"].append(f"missing_{field}")

        if m.get("requirement_level") not in VALID_REQUIREMENT:
            res["contract_valid"] = False
            res["contract_errors"].append(
                f"bad_requirement_level:{m.get('requirement_level')}"
            )
        if m.get("assertion_status") not in VALID_ASSERTION:
            res["contract_valid"] = False
            res["contract_errors"].append(
                f"bad_assertion_status:{m.get('assertion_status')}"
            )
        conf = m.get("confidence")
        if not isinstance(conf, (int, float)) or not 0.0 <= float(conf) <= 1.0:
            res["contract_valid"] = False
            res["contract_errors"].append(f"bad_confidence:{conf}")

        sf = m.get("source_field")
        if sf not in VALID_SOURCE_FIELDS:
            res["contract_valid"] = False
            res["contract_errors"].append(f"bad_source_field:{sf}")

        a_key = str(m.get("assertion_status"))
        r_key = str(m.get("requirement_level"))
        res["assertion_counts"][a_key] = res["assertion_counts"].get(a_key, 0) + 1
        res["requirement_counts"][r_key] = res["requirement_counts"].get(r_key, 0) + 1

        # evidence 必須逐字出現在所引用的 source_field 原文
        text = source_fields.get(sf or "", "") or ""
        ev = m.get("evidence") or ""
        res["evidence_checked"] += 1
        if ev and ev in text:
            res["evidence_grounded"] += 1
            s, e = m.get("start_offset"), m.get("end_offset")
            if isinstance(s, int) and isinstance(e, int):
                res["offset_checked"] += 1
                expected = m.get("raw_mention") or ev
                if 0 <= s <= e <= len(text) and text[s:e] == expected:
                    res["offset_exact"] += 1
        else:
            res["hallucinated"] += 1

        if is_skill:
            if CREDENTIAL_KEYWORDS.search(m.get("raw_mention") or ""):
                res["credential_routing_ok"] = False
            key = _norm(m.get("raw_mention") or "")
            if key:
                res["llm_skill_keys"].append(key)

    # protected pair：同一 canonical_candidate 底下同時掛了兩個受保護技能
    canon_map: dict[str, set[str]] = {}
    for m in skills:
        if not isinstance(m, dict):
            continue
        cc, rm = _norm(m.get("canonical_candidate") or ""), _norm(m.get("raw_mention") or "")
        if cc and rm:
            canon_map.setdefault(cc, set()).add(rm)
    for cc, mentions in canon_map.items():
        for a, b in PROTECTED_PAIRS:
            if a in mentions and b in mentions:
                res["protected_pair_violations"].append(f"{cc}<-{a}+{b}")

    keys = set(res["llm_skill_keys"])
    res["matched_phrase"] = len(keys & phrase_skills)
    res["novel_vs_phrase"] = len(keys - phrase_skills)
    res["matched_structured"] = len(keys & structured_skills)
    return res


# ─────────────────────────────────────────────────────────────────────────────
# Assertion challenge set（唯一有 ground truth 的部分）
# ─────────────────────────────────────────────────────────────────────────────
CHALLENGE_PROMPT = """You classify how a skill is asserted in a Taiwan job posting snippet.

Return strict JSON only, no Markdown:
{"assertion_status": "affirmed|negated|uncertain", "requirement_level": "required|preferred|unspecified"}

Rules:
- 不需/無需/不用/免 → negated
- 若…佳 / 可能 / 尤佳 / 加分 → uncertain（requirement_level 視語氣給 preferred）
- 必備 / 需具備 / 須 → required
- 只是被列出、無法判斷 → unspecified

SOURCE_FIELD: {source_field}
SNIPPET: {context}
SKILL_MENTION: {skill_mention}
"""


def load_challenge_set() -> list[dict]:
    if not CHALLENGE_SET.exists():
        return []
    return [
        json.loads(line)
        for line in CHALLENGE_SET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def run_challenge_set(
    model_id: str, mode: str, *, temperature: float
) -> dict[str, Any]:
    items = load_challenge_set()
    labeled = [i for i in items if i.get("expected_assertion_status")]
    correct = total = req_correct = req_total = 0
    failures: list[dict] = []
    by_category: dict[str, dict[str, int]] = {}
    seen_in_category: dict[str, int] = {}

    for it in labeled:
        prompt = (
            CHALLENGE_PROMPT.replace("{source_field}", it.get("source_field", ""))
            .replace("{context}", it.get("context", ""))
            .replace("{skill_mention}", it.get("skill_mention", ""))
        )
        cat = it.get("category", "unknown")
        by_category.setdefault(cat, {"total": 0, "correct": 0})
        seen_in_category[cat] = seen_in_category.get(cat, 0) + 1

        if mode == "mock":
            # mock 故意在 conditional 類別的第一題答錯，
            # 用來證明評分器真的會記錄不一致（否則 mock 全對，錯誤路徑等於沒被測到）
            pred = {
                "assertion_status": it["expected_assertion_status"],
                "requirement_level": it.get("expected_requirement_level", "unspecified"),
            }
            if cat == "conditional" and seen_in_category[cat] == 1:
                wrong = "affirmed" if pred["assertion_status"] != "affirmed" else "negated"
                pred["assertion_status"] = wrong
        else:
            try:
                out = llm_client.invoke(
                    model_id, prompt, max_tokens=256, temperature=temperature
                )
                parsed, _, _ = parse_strict_json(out["text"])
                pred = parsed or {}
            except llm_client.BedrockError as e:
                failures.append(
                    {"challenge_id": it["challenge_id"], "error": str(e)[:200]}
                )
                pred = {}

        total += 1
        by_category[cat]["total"] += 1
        if pred.get("assertion_status") == it["expected_assertion_status"]:
            correct += 1
            by_category[cat]["correct"] += 1
        else:
            failures.append({
                "challenge_id": it["challenge_id"], "category": cat,
                "expected": it["expected_assertion_status"],
                "got": pred.get("assertion_status"),
            })
        if it.get("expected_requirement_level"):
            req_total += 1
            if pred.get("requirement_level") == it["expected_requirement_level"]:
                req_correct += 1

    return {
        "assertion_total": total,
        "assertion_correct": correct,
        "assertion_accuracy": round(correct / total, 4) if total else None,
        "requirement_total": req_total,
        "requirement_correct": req_correct,
        "requirement_accuracy": round(req_correct / req_total, 4) if req_total else None,
        "by_category": by_category,
        "protected_pair_items_in_set": sum(
            1 for i in items if i.get("category") == "protected_pairs"
        ),
        "failures": failures[:20],
        "note": (
            "challenge set 的 protected_pairs 題目沒有 assertion label；"
            "protected-pair 錯誤另由 golden slice 輸出的 canonical 合併行為衡量"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 結構化 baseline（recall 代理）
# ─────────────────────────────────────────────────────────────────────────────
def _load_baseline(source: Path, job_ids: set[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {j: set() for j in job_ids}
    if not source.exists():
        return out
    remaining = set(job_ids)
    with source.open("r", encoding="utf-8") as f:
        for line in f:
            if not remaining:
                break
            if '"skills": [], "credentials": []' in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            jid = str(rec.get("job_id"))
            if jid in remaining:
                out[jid] = {
                    _norm(m.get("raw_mention") or "")
                    for m in (rec.get("skills") or [])
                    if m.get("raw_mention")
                }
                remaining.discard(jid)
    return out


def load_baselines(job_ids: set[str]) -> dict[str, dict[str, set[str]]]:
    """
    phrase = 同一組非結構化欄位（職務名稱／職務內容／附加條件）的規則式抽取
             → 這才是 LLM 的 apples-to-apples 比較基準
    structured = 電腦技能資料／工作技能／專業證照
             → 與 LLM 看到的欄位互斥，只能當「跨欄位補充」資訊，不是 recall 基準
    """
    return {
        "phrase": _load_baseline(EXTRACTIONS_PHRASE, job_ids),
        "structured": _load_baseline(EXTRACTIONS_STRUCTURED, job_ids),
    }


def mock_response(prompt_rec: dict) -> str:
    """合成一筆 §3.4 形狀的回應，並刻意放一筆幻覺，驗證評分器抓得到。"""
    jid = prompt_rec["job_id"]
    sf = prompt_rec["source_fields"]
    desc = sf.get("職務內容") or ""
    skills = []
    if len(desc) >= 4:
        frag = desc[:4]
        skills.append({
            "mention_id": f"mention:{jid}:職務內容:0:4:llm_v0.1",
            "raw_mention": frag, "canonical_candidate": "skill:mock_grounded",
            "source_field": "職務內容", "start_offset": 0, "end_offset": 4,
            "requirement_level": "unspecified", "assertion_status": "affirmed",
            "confidence": 0.8, "evidence": frag, "method": "llm",
            "extractor_version": "llm_v0.1",
        })
    skills.append({
        "mention_id": f"mention:{jid}:職務內容:900:905:llm_v0.1",
        "raw_mention": "MOCK_HALLUCINATION", "canonical_candidate": "skill:mock_halluc",
        "source_field": "職務內容", "start_offset": 900, "end_offset": 905,
        "requirement_level": "preferred", "assertion_status": "affirmed",
        "confidence": 0.5, "evidence": "這段文字不存在於原文", "method": "llm",
        "extractor_version": "llm_v0.1",
    })
    return json.dumps(
        {
            "job_id": jid, "skills": skills, "credentials": [],
            "extraction_version": "llm_v0.1", "failure_tags": [],
            "relation_candidates": [],
        },
        ensure_ascii=False,
    )


def run_candidate(
    *,
    candidate_id: str,
    model_id: str,
    mode: str,
    prompts: list[dict],
    baselines: dict[str, dict[str, set[str]]],
    max_tokens: int,
    temperature: float,
    price_in: float | None,
    price_out: float | None,
    out_dir: Path,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    resp_path = out_dir / "responses.jsonl"
    phrase_base = baselines["phrase"]
    structured_base = baselines["structured"]

    # rescore：重讀既有 responses.jsonl 的 raw_text 重新評分，不再呼叫 API
    saved: dict[str, dict] = {}
    if mode == "rescore":
        if not resp_path.exists():
            raise FileNotFoundError(f"rescore needs existing {resp_path}")
        for line in resp_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            saved[str(e.get("job_id"))] = e

    n = len(prompts)
    strict_ok = contract_ok = parsed_count = 0
    ev_checked = ev_grounded = off_checked = off_exact = 0
    halluc = cred_ok = matched = novel = llm_skill_total = 0
    latencies: list[float] = []
    in_tokens: list[int] = []
    out_tokens: list[int] = []
    errors: dict[str, int] = {}
    protected_violations: list[str] = []
    repair_notes: dict[str, int] = {}
    matched_structured_total = 0
    aborted = False
    t_start = time.perf_counter()

    sink_path = resp_path if mode != "rescore" else out_dir / "responses_rescored.jsonl"
    with sink_path.open("w", encoding="utf-8") as sink:
        for i, rec in enumerate(prompts, 1):
            jid = rec["job_id"]
            entry: dict[str, Any] = {"job_id": jid, "candidate_id": candidate_id}

            if mode == "dry-run":
                approx = round(len(rec["prompt"]) / 2.5)
                entry["dry_run"] = {
                    "prompt_chars": len(rec["prompt"]),
                    "approx_input_tokens": approx,
                    "request_shape": {
                        "model_id": model_id,
                        "maxTokens": max_tokens,
                        "temperature": temperature,
                        "api": "bedrock-runtime Converse",
                    },
                }
                in_tokens.append(approx)
                sink.write(json.dumps(entry, ensure_ascii=False) + "\n")
                continue

            if mode == "rescore":
                prev = saved.get(jid)
                if prev is None or "raw_text" not in prev:
                    continue
                text = prev["raw_text"]
                latency = prev.get("latency_ms") or 0.0
                itok, otok = prev.get("input_tokens"), prev.get("output_tokens")
            elif mode == "mock":
                text = mock_response(rec)
                latency = 12.0
                itok, otok = round(len(rec["prompt"]) / 2.5), round(len(text) / 2.5)
            else:
                try:
                    out = llm_client.invoke(
                        model_id, rec["prompt"],
                        max_tokens=max_tokens, temperature=temperature,
                    )
                    text, latency = out["text"], out["latency_ms"]
                    itok, otok = out["input_tokens"], out["output_tokens"]
                except llm_client.BedrockError as e:
                    key = f"http_{e.status}"
                    errors[key] = errors.get(key, 0) + 1
                    entry["error"] = str(e)[:300]
                    sink.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    if sum(errors.values()) >= max(3, n // 2):
                        print(f"    [ABORT] too many errors for {candidate_id}: {errors}")
                        aborted = True
                        break
                    continue

            latencies.append(latency)
            if itok:
                in_tokens.append(itok)
            if otok:
                out_tokens.append(otok)

            obj, s_ok, note = parse_strict_json(text)
            if s_ok:
                strict_ok += 1
            if note:
                repair_notes[note] = repair_notes.get(note, 0) + 1
            entry.update({
                "strict_json": s_ok, "repair_note": note,
                "raw_text": text[:8000],
                "latency_ms": latency, "input_tokens": itok, "output_tokens": otok,
            })

            if obj is None:
                errors["unparseable_json"] = errors.get("unparseable_json", 0) + 1
                sink.write(json.dumps(entry, ensure_ascii=False) + "\n")
                continue

            parsed_count += 1
            sc = score_record(
                obj, rec["source_fields"],
                phrase_base.get(jid, set()), structured_base.get(jid, set()),
            )
            contract_ok += 1 if sc["contract_valid"] else 0
            ev_checked += sc["evidence_checked"]
            ev_grounded += sc["evidence_grounded"]
            off_checked += sc["offset_checked"]
            off_exact += sc["offset_exact"]
            halluc += sc["hallucinated"]
            cred_ok += 1 if sc["credential_routing_ok"] else 0
            matched += sc["matched_phrase"]
            novel += sc["novel_vs_phrase"]
            matched_structured_total += sc["matched_structured"]
            llm_skill_total += sc["n_skills"]
            protected_violations.extend(sc["protected_pair_violations"])
            entry["score"] = {k: v for k, v in sc.items() if k != "llm_skill_keys"}
            sink.write(json.dumps(entry, ensure_ascii=False) + "\n")

            if i % 25 == 0:
                print(f"    {candidate_id}: {i}/{n}")

    wall_s = time.perf_counter() - t_start
    phrase_total = sum(len(phrase_base.get(r["job_id"], set())) for r in prompts)
    structured_total = sum(len(structured_base.get(r["job_id"], set())) for r in prompts)

    def ratio(a: int, b: int) -> float | None:
        return round(a / b, 4) if b else None

    cost_per_job = None
    if price_in is not None and price_out is not None and in_tokens and out_tokens:
        cost_per_job = round(
            (statistics.mean(in_tokens) / 1000.0) * price_in
            + (statistics.mean(out_tokens) / 1000.0) * price_out,
            6,
        )

    report = {
        "candidate_id": candidate_id,
        "model_id": model_id,
        "mode": mode,
        "aborted": aborted,
        "prompt_version": PROMPT_VERSION,
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "decoding": {"temperature": temperature, "max_tokens": max_tokens},
        "jobs_requested": n,
        "jobs_with_parsed_response": parsed_count,
        "labeled_metrics": {},
        "contract_metrics": {
            "strict_json_success_rate": ratio(strict_ok, n),
            "contract_valid_rate": ratio(contract_ok, n),
            "evidence_grounded_rate": ratio(ev_grounded, ev_checked),
            "offset_exact_rate": ratio(off_exact, off_checked),
            "credential_routing_ok_rate": ratio(cred_ok, parsed_count),
            "hallucinated_mention_rate": ratio(halluc, ev_checked),
            "protected_pair_violations": len(protected_violations),
            "json_repair_notes": repair_notes,
            "mentions_checked": ev_checked,
        },
        "recall_proxy": {
            "primary_baseline": "phrase (same unstructured fields as the LLM prompt)",
            "phrase_baseline_mentions": phrase_total,
            "llm_matched_phrase": matched,
            "phrase_agreement_rate": ratio(matched, phrase_total),
            "llm_novel_vs_phrase": novel,
            "llm_total_skill_mentions": llm_skill_total,
            "structured_baseline_mentions_other_fields": structured_total,
            "llm_matched_structured_other_fields": matched_structured_total,
            "structured_comparison_note": (
                "結構化 baseline 來自 電腦技能資料／工作技能／專業證照，"
                "與 LLM prompt 所見欄位（職務名稱／職務內容／附加條件）互斥，"
                "因此重疊低是預期現象，不可解讀為 LLM recall 差"
            ),
            "caveat": (
                "golden slice 無人工 mention 標註；agreement 只是 recall 代理，"
                "novel 只是增益代理（可能是真新技能，也可能是雜訊）。"
                "不可當作 mention precision / recall 匯報"
            ),
        },
        "cost_performance": {
            "latency_p50_ms": round(statistics.median(latencies), 1) if latencies else None,
            "latency_p95_ms": (
                round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1)
                if latencies else None
            ),
            "avg_input_tokens": round(statistics.mean(in_tokens), 1) if in_tokens else None,
            "avg_output_tokens": round(statistics.mean(out_tokens), 1) if out_tokens else None,
            "wall_seconds": round(wall_s, 1),
            "throughput_jobs_per_hour": (
                round(parsed_count / wall_s * 3600, 1)
                if wall_s > 0 and parsed_count else None
            ),
            "avg_cost_usd_per_job": cost_per_job,
            "price_basis": (
                {"usd_per_1k_input": price_in, "usd_per_1k_output": price_out}
                if price_in is not None and price_out is not None
                else "not supplied → cost left null"
            ),
        },
        "errors": errors,
        "responses_path": str(resp_path),
    }
    if mode == "dry-run":
        # 沒有任何回應 → 品質指標必須是 null，不能留 0.0 讓人誤讀成「0% 合格」
        est_in = statistics.mean(in_tokens) if in_tokens else None
        report["contract_metrics"] = {
            "note": "dry-run：未送出請求，故無契約／grounding 指標",
        }
        report["recall_proxy"] = {
            "structured_baseline_mentions": structured_total,
            "note": "dry-run：無模型輸出可比對",
        }
        report["dry_run_estimate"] = {
            "jobs": n,
            "avg_prompt_chars": round(
                statistics.mean([len(r["prompt"]) for r in prompts]), 1
            ) if prompts else None,
            "avg_approx_input_tokens": round(est_in, 1) if est_in else None,
            "total_approx_input_tokens": round(sum(in_tokens), 1) if in_tokens else None,
            "estimated_input_cost_usd": (
                round(sum(in_tokens) / 1000.0 * price_in, 6)
                if in_tokens and price_in is not None else None
            ),
            "token_estimate_basis": "chars / 2.5（粗估，實際以 API usage 為準）",
        }

    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="A0b: run + score LLM bake-off (Gate 1)")
    ap.add_argument(
        "--mode",
        choices=["mock", "dry-run", "live", "rescore"],
        default="mock",
        help="rescore: 重新評分既有 responses.jsonl，不呼叫 API、不重複計費",
    )
    ap.add_argument(
        "--candidates", default="",
        help="逗號分隔 candidate_id=model_id，例：cand_a=us.anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    ap.add_argument("--limit", type=int, default=25, help="golden slice 取前 N 筆")
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--price-in", type=float, default=None, help="USD / 1k input tokens")
    ap.add_argument("--price-out", type=float, default=None, help="USD / 1k output tokens")
    ap.add_argument("--skip-challenge", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print(f"A0b — LLM bake-off   mode={args.mode}")
    print("=" * 70)

    if not PROMPTS.exists():
        print(f"  [FAIL] missing {PROMPTS}; run step_a0_llm_bakeoff_prep.py first")
        return 1

    prompts: list[dict] = []
    with PROMPTS.open("r", encoding="utf-8") as f:
        for line in f:
            if len(prompts) >= args.limit:
                break
            prompts.append(json.loads(line))
    print(f"  prompts: {len(prompts)} (limit={args.limit})")

    if args.mode == "live":
        keys = llm_client.load_env()
        missing = [k for k, v in llm_client.credentials_present().items() if not v]
        if missing:
            print(f"  [FAIL] missing credentials in .env: {missing}")
            return 1
        print(f"  region={llm_client.region()}  env keys={sorted(set(keys))}")

    if args.candidates:
        candidates: list[tuple[str, str]] = []
        for spec in args.candidates.split(","):
            cid, _, mid = spec.partition("=")
            if not mid.strip():
                print(f"  [FAIL] bad --candidates entry: {spec!r}")
                return 1
            candidates.append((cid.strip(), mid.strip()))
    else:
        candidates = [("mock_candidate", "mock://synthetic")]
    print(f"  candidates: {[c for c, _ in candidates]}")

    print("  loading baselines (recall proxy) ...")
    baselines = load_baselines({r["job_id"] for r in prompts})
    print(f"    phrase baseline (same fields as prompt): "
          f"{sum(len(v) for v in baselines['phrase'].values())} mentions")
    print(f"    structured baseline (other fields, informational): "
          f"{sum(len(v) for v in baselines['structured'].values())} mentions")

    runs_dir = BAKEOFF_DIR / "runs"
    reports = []
    for cid, mid in candidates:
        print(f"\n  ── {cid}  ({mid}) ──")
        rep = run_candidate(
            candidate_id=cid, model_id=mid, mode=args.mode, prompts=prompts,
            baselines=baselines, max_tokens=args.max_tokens,
            temperature=args.temperature, price_in=args.price_in,
            price_out=args.price_out, out_dir=runs_dir / cid,
        )
        if args.mode == "rescore":
            # 不重打 API：沿用先前 live run 的 assertion 結果
            prev_report = runs_dir / cid / "report_live_backup.json"
            src = prev_report if prev_report.exists() else None
            if src:
                rep["labeled_metrics"] = json.loads(
                    src.read_text(encoding="utf-8")
                ).get("labeled_metrics", {})
            rep["labeled_metrics_source"] = (
                str(src) if src else "no previous live report found"
            )
            (runs_dir / cid / "report.json").write_text(
                json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        elif not args.skip_challenge and args.mode != "dry-run" and not rep["aborted"]:
            print("    scoring assertion challenge set ...")
            rep["labeled_metrics"] = run_challenge_set(
                mid, args.mode, temperature=args.temperature
            )
            (runs_dir / cid / "report.json").write_text(
                json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        reports.append(rep)

        cm, cp = rep["contract_metrics"], rep["cost_performance"]
        lm = rep.get("labeled_metrics") or {}
        if args.mode == "dry-run":
            est = rep["dry_run_estimate"]
            print(f"    jobs={est['jobs']}  avg_prompt_chars={est['avg_prompt_chars']}  "
                  f"avg_approx_input_tokens={est['avg_approx_input_tokens']}")
            print(f"    total_approx_input_tokens={est['total_approx_input_tokens']}  "
                  f"estimated_input_cost_usd={est['estimated_input_cost_usd']}")
        else:
            print(f"    strict_json={cm['strict_json_success_rate']}  "
                  f"contract_valid={cm['contract_valid_rate']}  "
                  f"evidence_grounded={cm['evidence_grounded_rate']}")
            print(f"    halluc={cm['hallucinated_mention_rate']}  "
                  f"offset_exact={cm['offset_exact_rate']}  "
                  f"protected_violations={cm['protected_pair_violations']}")
            print(f"    assertion_acc={lm.get('assertion_accuracy')}  "
                  f"p50={cp['latency_p50_ms']}ms  p95={cp['latency_p95_ms']}ms  "
                  f"cost/job={cp['avg_cost_usd_per_job']}")
        if rep["errors"]:
            print(f"    errors={rep['errors']}")

    summary = {
        "step": "step_a0b_llm_bakeoff_run",
        "mode": args.mode,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "gate": "Gate 1 comparison — NOT a Gate 2 freeze",
        "prompt_version": PROMPT_VERSION,
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "golden_slice_limit": args.limit,
        "challenge_set": str(CHALLENGE_SET),
        "candidates": [
            {
                "candidate_id": r["candidate_id"],
                "model_id": r["model_id"],
                "aborted": r["aborted"],
                "contract_metrics": r["contract_metrics"],
                "labeled_metrics": r.get("labeled_metrics"),
                "recall_proxy": r["recall_proxy"],
                "cost_performance": r["cost_performance"],
                "errors": r["errors"],
            }
            for r in reports
        ],
        "known_limitations": [
            "golden slice 無人工 mention 標註 → 只報契約／grounding／agreement 代理指標",
            "assertion_accuracy 樣本僅 48 筆 challenge set，只夠做 Gate 1 篩選",
            "mock 模式數字為合成，不可寫入 model_registry 當品質依據",
            "本步驟不產生任何進圖 artifact；use_llm_extraction 維持 false 直到 Gate 2",
        ],
    }
    summary_path = BAKEOFF_DIR / f"bakeoff_summary_{args.mode}.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n  wrote {summary_path}")
    print("\n✓ A0b complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
