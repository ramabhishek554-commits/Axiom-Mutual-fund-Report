import pandas as pd
import pytest
from src.mf_report.analytics import estimated_allocation, overlap_pct, concentration_analysis, sector_exposure, historical_changes

def sample():
    return pd.DataFrame([
      {"scheme":"A","category":"Large Cap","company":"Issuer X Ltd","normalized_company":"issuer x","weight_pct":10.,"sector":"IT","instrument_type":"Equity","valuation_date":"2026-08-31"},
      {"scheme":"A","category":"Large Cap","company":"Issuer Y Ltd","normalized_company":"issuer y","weight_pct":5.,"sector":"Banks","instrument_type":"Equity","valuation_date":"2026-08-31"},
      {"scheme":"B","category":"Mid Cap","company":"Issuer X","normalized_company":"issuer x","weight_pct":7.,"sector":"IT","instrument_type":"Equity","valuation_date":"2026-08-31"},
      {"scheme":"B","category":"Mid Cap","company":"Issuer Y","normalized_company":"issuer y","weight_pct":8.,"sector":"Banks","instrument_type":"Equity","valuation_date":"2026-08-31"}])

def test_allocation_example(): assert estimated_allocation(100000,12.5)==12500
def test_invalid_allocation():
    with pytest.raises(ValueError): estimated_allocation(-1,10)
    with pytest.raises(ValueError): estimated_allocation(10,101)
def test_overlap_example(): assert overlap_pct(sample(),"A","B")==pytest.approx(12)
def test_overlap_dates_must_match():
    d=sample(); d.loc[d.scheme.eq("B"),"valuation_date"]="2026-09-30"
    assert overlap_pct(d,"A","B") is None
def test_concentration():
    result=concentration_analysis(sample())
    assert set(result.equity_issuer_count)=={2}
    assert result.loc[result.scheme.eq("A"),"top_10_weight_pct"].iloc[0]==pytest.approx(15)
def test_unknown_sector_retained():
    d=sample(); d.loc[0,"sector"]=""
    assert "Unknown / unmapped" in set(sector_exposure(d).sector)
def test_historical_shift_not_transaction():
    a=sample(); b=sample(); b["valuation_date"]="2026-09-30"; b.loc[b.company.eq("Issuer X Ltd"),"weight_pct"]=11
    result=historical_changes(pd.concat([a,b],ignore_index=True))
    assert not result.empty
    assert result.interpretation.str.contains("does not establish").all()



def test_overlap_uses_latest_valuation_date_shared_by_both_schemes():
    d = pd.DataFrame([
        {"scheme":"A", "company":"X", "normalized_company":"x", "weight_pct":5, "instrument_type":"Equity", "valuation_date":"2026-08-31"},
        {"scheme":"A", "company":"Y", "normalized_company":"y", "weight_pct":10, "instrument_type":"Equity", "valuation_date":"2026-08-31"},
        {"scheme":"A", "company":"X", "normalized_company":"x", "weight_pct":20, "instrument_type":"Equity", "valuation_date":"2026-09-30"},
        {"scheme":"A", "company":"Y", "normalized_company":"y", "weight_pct":1, "instrument_type":"Equity", "valuation_date":"2026-09-30"},
        {"scheme":"B", "company":"X", "normalized_company":"x", "weight_pct":6, "instrument_type":"Equity", "valuation_date":"2026-08-31"},
        {"scheme":"B", "company":"Y", "normalized_company":"y", "weight_pct":4, "instrument_type":"Equity", "valuation_date":"2026-08-31"},
        {"scheme":"B", "company":"X", "normalized_company":"x", "weight_pct":15, "instrument_type":"Equity", "valuation_date":"2026-10-31"},
        {"scheme":"B", "company":"Y", "normalized_company":"y", "weight_pct":10, "instrument_type":"Equity", "valuation_date":"2026-10-31"},
    ])
    assert overlap_pct(d, "A", "B") == pytest.approx(9)


def test_current_state_metrics_do_not_add_multiple_snapshots():
    d = sample()
    newer = pd.DataFrame([
        {"scheme":"A","category":"Large Cap","company":"Issuer X Ltd","normalized_company":"issuer x","weight_pct":20.,"sector":"IT","instrument_type":"Equity","valuation_date":"2026-09-30"},
        {"scheme":"A","category":"Large Cap","company":"Issuer Y Ltd","normalized_company":"issuer y","weight_pct":1.,"sector":"Banks","instrument_type":"Equity","valuation_date":"2026-09-30"},
    ])
    combined = pd.concat([d, newer], ignore_index=True)
    concentration = concentration_analysis(combined).set_index("scheme")
    assert concentration.loc["A", "portfolio_weight_sum_pct"] == pytest.approx(21)
    assert concentration.loc["B", "portfolio_weight_sum_pct"] == pytest.approx(15)

    sectors = sector_exposure(combined)
    assert sectors.loc[sectors.scheme.eq("A"), "weight_pct"].sum() == pytest.approx(21)


def test_overlap_is_unknown_when_valuation_dates_are_missing():
    d = sample()
    d.loc[d.scheme.eq("B"), "valuation_date"] = ""
    assert overlap_pct(d, "A", "B") is None


def test_historical_changes_ignore_blank_valuation_date():
    d = sample()
    d.loc[d.scheme.eq("A"), "valuation_date"] = ""
    result = historical_changes(d)
    assert result.empty
