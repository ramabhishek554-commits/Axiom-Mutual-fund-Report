"""Portfolio calculations; holding weights use percentage units (12.5 means 12.5%)."""
import pandas as pd

CATEGORIES = ["Large Cap", "Mid Cap", "Small Cap"]

def estimated_allocation(investment_amount: float, weight_pct: float) -> float:
    if investment_amount < 0:
        raise ValueError("Investment amount cannot be negative.")
    if not 0 <= weight_pct <= 100:
        raise ValueError("Weight percentage must be between 0 and 100.")
    return investment_amount * weight_pct / 100

def _usable(df):
    required = {"scheme", "company", "weight_pct"}
    if not required.issubset(df.columns):
        return pd.DataFrame(columns=list(required))
    d = df.copy()
    d["weight_pct"] = pd.to_numeric(d["weight_pct"], errors="coerce")
    d = d.dropna(subset=["scheme", "company", "weight_pct"])
    d = d[d.weight_pct.between(0, 100)]
    if "instrument_type" in d:
        d = d[d.instrument_type.astype(str).str.lower().eq("equity")]
    if "normalized_company" not in d:
        d["normalized_company"] = d.company.astype(str).str.lower().str.strip()
    if "category" not in d:
        d["category"] = ""
    if "sector" not in d:
        d["sector"] = ""
    return d

def overlap_pct(df, scheme_a, scheme_b):
    d = _usable(df)
    a, b = d[d.scheme.eq(scheme_a)], d[d.scheme.eq(scheme_b)]
    if a.empty or b.empty:
        return None
    if "valuation_date" in d:
        da = set(a.valuation_date.dropna().astype(str)) - {""}
        db = set(b.valuation_date.dropna().astype(str)) - {""}
        if da and db and da.isdisjoint(db):
            return None
    wa = a.groupby("normalized_company").weight_pct.sum()
    wb = b.groupby("normalized_company").weight_pct.sum()
    common = wa.index.intersection(wb.index)
    return float(sum(min(float(wa[k]), float(wb[k])) for k in common))

def overlap_matrix(df):
    d = _usable(df)
    schemes = sorted(d.scheme.dropna().astype(str).unique())
    m = pd.DataFrame(index=schemes, columns=schemes, dtype=float)
    for a in schemes:
        for b in schemes:
            m.loc[a, b] = overlap_pct(d, a, b)
    m.index.name = "scheme"
    return m.reset_index()

def sector_exposure(df):
    d = _usable(df)
    d["sector"] = d.sector.fillna("").astype(str).str.strip().replace("", "Unknown / unmapped")
    return d.groupby(["scheme", "category", "sector"], as_index=False, dropna=False).weight_pct.sum().sort_values(["scheme", "weight_pct"], ascending=[True, False])

def concentration_analysis(df):
    d = _usable(df)
    rows = []
    for scheme, g in d.groupby("scheme"):
        weights = g.groupby("normalized_company").weight_pct.sum().sort_values(ascending=False)
        decimals = weights / 100
        hhi = float((decimals ** 2).sum()) if len(weights) else None
        rows.append({"scheme": scheme, "equity_issuer_count": len(weights),
            "largest_holding_pct": float(weights.iloc[0]) if len(weights) else None,
            "top_5_weight_pct": float(weights.head(5).sum()), "top_10_weight_pct": float(weights.head(10).sum()),
            "hhi": hhi, "effective_holdings": 1 / hhi if hhi else None,
            "portfolio_weight_sum_pct": float(weights.sum()),
            "completeness_note": "Review source and weight sum; partial portfolios make metrics incomplete."})
    return pd.DataFrame(rows)

def company_comparison(df, investment_amount):
    d = _usable(df)
    if d.empty:
        return pd.DataFrame(columns=["company", "scheme", "category", "weight_pct", "estimated_allocation_inr", "sector"])
    d = d.copy()
    d["estimated_allocation_inr"] = d.weight_pct.map(lambda x: estimated_allocation(investment_amount, float(x)))
    cols = [c for c in ["company","scheme","category","weight_pct","estimated_allocation_inr","sector","valuation_date","source_url"] if c in d]
    return d[cols].sort_values(["company","scheme"])

def historical_changes(df):
    cols = ["scheme","company","from_date","to_date","from_weight_pct","to_weight_pct","change_pct_points","interpretation"]
    if not {"scheme","company","weight_pct","valuation_date"}.issubset(df.columns):
        return pd.DataFrame(columns=cols)
    d = _usable(df)
    rows = []
    for scheme, g in d.groupby("scheme"):
        dates = sorted(g.valuation_date.dropna().astype(str).unique())
        for before, after in zip(dates, dates[1:]):
            a = g[g.valuation_date.astype(str).eq(before)].groupby("normalized_company").weight_pct.sum()
            b = g[g.valuation_date.astype(str).eq(after)].groupby("normalized_company").weight_pct.sum()
            names = g.drop_duplicates("normalized_company").set_index("normalized_company").company.to_dict()
            for key in sorted(set(a.index) | set(b.index)):
                x, y = float(a.get(key, 0)), float(b.get(key, 0))
                if x != y:
                    rows.append({"scheme":scheme,"company":names.get(key,key),"from_date":before,"to_date":after,
                        "from_weight_pct":x,"to_weight_pct":y,"change_pct_points":y-x,
                        "interpretation":"Weight changed; does not establish a purchase or sale."})
    return pd.DataFrame(rows, columns=cols)
