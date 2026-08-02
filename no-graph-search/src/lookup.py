"""Task 2：職務對照表與城市對照表的 lookup 模組。

職缺.csv 用的是**中文名稱**（職務小類、工作城市），不是 code，所以這裡建的是
名稱 → metadata 的反查表。

資料結構（實測結果，見 scripts/inspect_lookups.py）：

職務對照表（691 筆）
    CodeNo 六位數決定層級：xx0000 = L1 大類（20 筆），xxxx00 = L2 中類（57 筆），
    其餘 = L3 小類（614 筆）。
    CodeNameA = 自己的名稱，CodeNameB = 所屬中類名稱，CodeNameC = 所屬大類名稱。
    L3 名稱在全表唯一（0 筆重複），可以安全地用名稱當 key。
    CodeAlike 用 <br> 分隔（不是逗號），填充率 99.3%。

城市對照表（1077 筆）
    CodeType 1 = 國家／洲（7 筆），2 = 城市／省（71 筆），3 = 區（999 筆）。
    CodeNameA = 自己的名稱，CodeNameB = 所屬城市名稱，CodeNameC = 所屬國家名稱。
    城市 code = CodeNo[:4] + "00"（999/999 驗證通過）
    國家 code = CodeNo[:2] + "0000"（1070/1070 驗證通過）
    城市名稱唯一；區名稱有 9 個重複（東區 x4、北區 x3 等），需要城市才能消歧。

用法：
    from src.lookup import Lookup
    lookup = Lookup.load()                      # 有快取就讀快取
    cat = lookup.get_job_category("會計／出納／記帳人員")
    city = lookup.get_city("新北市")
"""

from __future__ import annotations

import pickle
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pandas as pd
except ImportError:  # Lambda runtime — only pkl cache is used
    pd = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402

CACHE_VERSION = 2
CACHE_PATH = settings.cache_dir / "lookups.pkl"

# CodeAlike 的分隔符是 <br>，不是逗號。別名本身可能含 "/"（例如「總經理/執行長」），
# 所以只能用 <br> 切，不能用 / 切。
_ALIAS_SEP_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_WS_RE = re.compile(r"[\s\u3000\u00a0]+")


def _clean(value: object) -> str:
    """對照表欄位的統一清理：非字串 → 空字串，壓縮空白。"""
    if not isinstance(value, str):
        return ""
    return _WS_RE.sub(" ", value).strip()


def _split_aliases(raw: object) -> tuple[str, ...]:
    """把 CodeAlike 切成去重後的別名 tuple，保持原順序。"""
    text = _clean(raw)
    if not text:
        return ()
    seen: dict[str, None] = {}
    for part in _ALIAS_SEP_RE.split(text):
        alias = _clean(part)
        if alias:
            seen.setdefault(alias, None)
    return tuple(seen)


def category_level(code: str) -> int:
    """由 CodeNo 判斷層級：1 = 大類，2 = 中類，3 = 小類。"""
    if code.endswith("0000"):
        return 1
    if code.endswith("00"):
        return 2
    return 3


@dataclass(frozen=True)
class JobCategory:
    """職務分類的完整 metadata。"""

    code: str
    level: int
    name: str
    l1_code: str
    l1_name: str
    l2_code: str | None
    # 取自 CodeNameB，也就是 職缺.csv「職務中類」實際使用的寫法
    l2_name: str | None
    # L2 母層那筆的 CodeNameA。實測有 1 個群組兩者不同（140400：
    # CodeNameB='網路管理' vs L2.CodeNameA='電腦網路管理/MIS'），
    # 兩種寫法都要能被搜到，所以分開保存。相同時為 None。
    l2_name_alt: str | None
    l3_code: str | None
    l3_name: str | None
    aliases: tuple[str, ...]
    definition: str
    description: str
    name_en: str

    @property
    def aliases_text(self) -> str:
        """BM25 用的別名字串，空格分隔。"""
        return " ".join(self.aliases)

    @property
    def l2_name_variants(self) -> tuple[str, ...]:
        """中類的所有寫法，供 BM25 索引用。"""
        return tuple(dict.fromkeys(n for n in (self.l2_name, self.l2_name_alt) if n))


@dataclass(frozen=True)
class City:
    """地區的完整 metadata，含 hard filter 需要的多層 code。"""

    code: str
    level: int
    name: str
    country_code: str
    country_name: str
    city_code: str | None
    city_name: str | None
    district_code: str | None
    district_name: str | None
    zip_code: str | None
    area: str

    @property
    def location_codes(self) -> tuple[str, ...]:
        """自己與所有祖先的 code，由粗到細。

        用於 hard filter：使用者不論送城市層或區層的 code，用 terms filter 打這個
        欄位都能命中（plan 決策 [2]a「存原始 code + ancestors」）。
        """
        codes = [self.country_code]
        if self.city_code:
            codes.append(self.city_code)
        if self.district_code:
            codes.append(self.district_code)
        return tuple(dict.fromkeys(codes))


@dataclass
class Lookup:
    """名稱 → metadata 的反查表集合。"""

    categories_by_code: dict[str, JobCategory] = field(default_factory=dict)
    # 各層獨立的名稱索引，查詢時由細到粗 fallback
    categories_by_l3_name: dict[str, JobCategory] = field(default_factory=dict)
    categories_by_l2_name: dict[str, JobCategory] = field(default_factory=dict)
    categories_by_l1_name: dict[str, JobCategory] = field(default_factory=dict)

    cities_by_code: dict[str, City] = field(default_factory=dict)
    cities_by_name: dict[str, City] = field(default_factory=dict)
    # 區名稱有重複，所以值是 list，需要城市名才能消歧
    districts_by_name: dict[str, list[City]] = field(default_factory=dict)

    # ---------------------------------------------------------------- 建構

    @classmethod
    def build(cls) -> Lookup:
        """從 CSV 建構所有 lookup。"""
        lookup = cls()
        lookup._build_categories()
        lookup._build_cities()
        return lookup

    def _build_categories(self) -> None:
        df = pd.read_csv(settings.job_category_csv, dtype=str)

        # 先建 code → 名稱，供推導祖先名稱用
        name_by_code = {
            _clean(r.CodeNo): _clean(r.CodeNameA) for r in df.itertuples(index=False)
        }

        for row in df.itertuples(index=False):
            code = _clean(row.CodeNo)
            if not code:
                continue
            level = category_level(code)
            name = _clean(row.CodeNameA)

            l1_code = code[:2] + "0000"
            l2_code = code[:4] + "00" if level >= 2 else None

            l2_name = _clean(row.CodeNameB) if level >= 2 else None
            l2_canonical = name_by_code.get(l2_code, "") if l2_code else ""
            l2_name_alt = l2_canonical if l2_canonical and l2_canonical != l2_name else None

            category = JobCategory(
                code=code,
                level=level,
                name=name,
                l1_code=l1_code,
                # CodeNameC 就是大類名稱，比用 code 反查更可靠（實測 691/691 一致）
                l1_name=_clean(row.CodeNameC) or name_by_code.get(l1_code, ""),
                l2_code=l2_code,
                l2_name=l2_name,
                l2_name_alt=l2_name_alt,
                l3_code=code if level == 3 else None,
                l3_name=name if level == 3 else None,
                aliases=_split_aliases(row.CodeAlike),
                definition=_clean(row.CodeDefinition),
                description=_clean(row.CodeDescript),
                name_en=_clean(row.CodeNameEN),
            )
            self.categories_by_code[code] = category

            index = {
                1: self.categories_by_l1_name,
                2: self.categories_by_l2_name,
                3: self.categories_by_l3_name,
            }[level]
            # L3 名稱實測唯一；L1/L2 若有重複，保留第一筆（後續以 code 為準）
            index.setdefault(name, category)

    def _build_cities(self) -> None:
        df = pd.read_csv(settings.city_csv, dtype=str)

        for row in df.itertuples(index=False):
            code = _clean(row.CodeNo)
            if not code:
                continue
            level = int(_clean(row.CodeType) or 0)
            name = _clean(row.CodeNameA)
            zip_code = _clean(row.CodeZip)

            country_code = code[:2] + "0000"
            city_code = code[:4] + "00" if level >= 2 else None

            city = City(
                code=code,
                level=level,
                name=name,
                country_code=country_code,
                country_name=_clean(row.CodeNameC),
                city_code=city_code,
                city_name=(_clean(row.CodeNameB) if level >= 2 else None),
                district_code=code if level == 3 else None,
                district_name=name if level == 3 else None,
                # CodeZip 為 "0" 代表沒有郵遞區號
                zip_code=zip_code if zip_code and zip_code != "0" else None,
                area=_clean(row.CodeArea),
            )
            self.cities_by_code[code] = city

            if level <= 2:
                # 國家與城市名稱實測唯一
                self.cities_by_name.setdefault(name, city)
            else:
                self.districts_by_name.setdefault(name, []).append(city)

    # ---------------------------------------------------------------- 快取

    def save(self, path: Path | None = None) -> Path:
        path = path or CACHE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "sources": _source_fingerprint(),
            "lookup": self,
        }
        with path.open("wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        return path

    @classmethod
    def load(cls, *, use_cache: bool = True, path: Path | None = None) -> Lookup:
        """讀快取，快取不存在或來源 CSV 有變動就重建。"""
        path = path or CACHE_PATH
        if use_cache and path.exists():
            try:
                with path.open("rb") as f:
                    payload = pickle.load(f)
                fingerprint = _source_fingerprint()
                if (
                    payload.get("version") == CACHE_VERSION
                    and (fingerprint is None or payload.get("sources") == fingerprint)
                ):
                    return payload["lookup"]
            except (pickle.UnpicklingError, EOFError, KeyError, AttributeError):
                pass  # 快取壞了就重建

        lookup = cls.build()
        if use_cache:
            lookup.save(path)
        return lookup

    # ---------------------------------------------------------------- 查詢

    def get_job_category(
        self,
        l3_name: str | None,
        *,
        l2_name: str | None = None,
        l1_name: str | None = None,
    ) -> JobCategory | None:
        """用職務名稱查分類 metadata，由細到粗 fallback。

        職缺.csv 的「職務小類」實測 99.98% 命中 L3，少數值其實是中類名稱
        （例如「採購資材」），所以 L3 查不到時往上退到 L2、L1。

        Returns:
            找到的 JobCategory；三層都查不到回傳 None（不拋錯）。
        """
        for name, index in (
            (l3_name, self.categories_by_l3_name),
            (l3_name, self.categories_by_l2_name),
            (l3_name, self.categories_by_l1_name),
            (l2_name, self.categories_by_l2_name),
            (l1_name, self.categories_by_l1_name),
        ):
            cleaned = _clean(name)
            if cleaned and cleaned in index:
                return index[cleaned]
        return None

    def get_job_category_by_code(self, code: str | None) -> JobCategory | None:
        return self.categories_by_code.get(_clean(code))

    def get_city(self, name: str | None, *, parent_city: str | None = None) -> City | None:
        """用地區名稱查 metadata。

        職缺.csv 的「工作城市」實測 100% 是城市層（CodeType=2），沒有區層資料，
        但仍支援區名查詢以備後續使用。區名有重複（東區、北區等），需要 parent_city
        才能消歧；無法消歧時回傳 None 而不是猜一個。

        Returns:
            找到的 City；查不到或無法消歧回傳 None（不拋錯）。
        """
        cleaned = _clean(name)
        if not cleaned:
            return None

        if cleaned in self.cities_by_name:
            return self.cities_by_name[cleaned]

        candidates = self.districts_by_name.get(cleaned, [])
        if len(candidates) == 1:
            return candidates[0]
        if candidates and parent_city:
            hint = _clean(parent_city)
            matched = [c for c in candidates if c.city_name == hint]
            if len(matched) == 1:
                return matched[0]
        return None

    def get_city_by_code(self, code: str | None) -> City | None:
        return self.cities_by_code.get(_clean(code))

    # ---------------------------------------------------------------- 統計

    def stats(self) -> dict[str, int]:
        levels = [c.level for c in self.categories_by_code.values()]
        return {
            "職務分類總數": len(self.categories_by_code),
            "職務 L1 大類": levels.count(1),
            "職務 L2 中類": levels.count(2),
            "職務 L3 小類": levels.count(3),
            "有別名的分類": sum(1 for c in self.categories_by_code.values() if c.aliases),
            "別名總數": sum(len(c.aliases) for c in self.categories_by_code.values()),
            "地區總數": len(self.cities_by_code),
            "國家／洲": sum(1 for c in self.cities_by_code.values() if c.level == 1),
            "城市／省": sum(1 for c in self.cities_by_code.values() if c.level == 2),
            "區": sum(1 for c in self.cities_by_code.values() if c.level == 3),
            "重複的區名": sum(1 for v in self.districts_by_name.values() if len(v) > 1),
        }


def _source_fingerprint() -> tuple | None:
    """來源 CSV 的 (大小, mtime)，用來判斷快取是否過期。

    CSV 不存在時回傳 None，讓快取直接視為有效（打包部署時不帶 CSV）。
    """
    try:
        return tuple(
            (p.name, p.stat().st_size, int(p.stat().st_mtime))
            for p in (settings.job_category_csv, settings.city_csv)
        )
    except (FileNotFoundError, OSError):
        return None


def main() -> int:
    """人工檢視：印出統計與幾筆範例。"""
    import argparse

    parser = argparse.ArgumentParser(description="Task 2：對照表 lookup")
    parser.add_argument("--rebuild", action="store_true", help="忽略快取重新建構")
    args = parser.parse_args()

    settings.ensure_dirs()
    lookup = Lookup.load(use_cache=not args.rebuild)

    print("=== 統計 ===")
    for key, value in lookup.stats().items():
        print(f"  {key:<12}: {value:,}")

    print("\n=== 職務分類範例 ===")
    for name in ("人事／人力資源專員", "會計／出納／記帳人員", "外務／快遞／送貨", "採購資材"):
        cat = lookup.get_job_category(name)
        if cat is None:
            print(f"\n  {name!r} → 查無")
            continue
        print(f"\n  {name!r} → code={cat.code} level=L{cat.level}")
        print(f"    L1={cat.l1_name}")
        print(f"    L2={cat.l2_name}")
        print(f"    L3={cat.l3_name}")
        print(f"    EN={cat.name_en}")
        print(f"    定義={cat.definition[:60]}")
        print(f"    別名({len(cat.aliases)})={cat.aliases[:8]}")

    print("\n=== 地區範例 ===")
    for name in ("台北市", "新北市", "桃園市", "信義區", "東區"):
        city = lookup.get_city(name)
        if city is None:
            print(f"\n  {name!r} → 查無或無法消歧")
            continue
        print(f"\n  {name!r} → code={city.code} level={city.level}")
        print(f"    國家={city.country_name}({city.country_code})")
        print(f"    城市={city.city_name}({city.city_code})")
        print(f"    區={city.district_name}({city.district_code}) zip={city.zip_code}")
        print(f"    location_codes={city.location_codes}")

    print("\n=== 區名消歧 ===")
    for name, hint in (("東區", "台中市"), ("東區", "台南市"), ("東區", None)):
        city = lookup.get_city(name, parent_city=hint)
        result = f"{city.code}（{city.city_name}）" if city else "None（無法消歧）"
        print(f"  get_city({name!r}, parent_city={hint!r}) → {result}")

    print(f"\n快取: {CACHE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
