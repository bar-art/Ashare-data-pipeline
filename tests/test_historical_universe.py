from datetime import date

import pandas as pd
import pytest

from quant_agent.data.providers import BaoStockProvider


def test_snapshot_passes_explicit_date_and_rejects_future_fallback(monkeypatch):
    provider = BaoStockProvider()
    calls = []

    class API:
        def query_hs300_stocks(self, **kwargs):
            calls.append(kwargs)
            return pd.DataFrame(
                {
                    "updateDate": ["2018-06-11"],
                    "code": ["sh.600001"],
                    "code_name": ["later delisted"],
                }
            )

    monkeypatch.setattr(provider, "_query", lambda action: action(API()))
    members = provider.fetch_index_members("hs300", date(2018, 6, 12))
    assert calls == [{"date": "2018-06-12"}]
    assert members.symbol.tolist() == ["600001.SH"]
    with pytest.raises(ValueError, match="晚于"):
        provider.fetch_index_members("hs300", date(2018, 1, 1))
    with pytest.raises(ValueError):
        provider.fetch_index_members("unknown", date(2018, 6, 12))
