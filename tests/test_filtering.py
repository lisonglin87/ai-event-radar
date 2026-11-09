"""核心判定逻辑的单元测试：跑 `python -m pytest -q`。"""

from datetime import date

from src.filtering import fingerprint, is_actionable, merge, norm
from src.models import Event

TODAY = date(2026, 11, 9)


def make(**kw) -> Event:
    base: dict = dict(title="NVIDIA GTC 2027", organizer="NVIDIA", start_date="2027-03-15", end_date="2027-03-19")
    base.update(kw)
    return Event(**base)


def test_norm_strips_noise():
    assert norm("NVIDIA GTC 2027 大会") == norm("nvidia gtc")


def test_fingerprint_stable_across_sources():
    a = make(title="NVIDIA GTC 2027", organizer="NVIDIA")
    b = make(title="NVIDIA GTC 2027 大会", organizer="NVIDIA GTC")
    assert fingerprint(a) == fingerprint(b)


def test_open_future_is_actionable():
    assert is_actionable(make(registration_status="open"), TODAY)


def test_full_is_not_actionable():
    assert not is_actionable(make(registration_status="full"), TODAY)


def test_closed_and_upcoming_not_actionable():
    assert not is_actionable(make(registration_status="closed"), TODAY)
    assert not is_actionable(make(registration_status="upcoming"), TODAY)


def test_expired_event_not_actionable():
    ev = make(start_date="2026-10-15", end_date="2026-10-16", registration_status="open")
    assert not is_actionable(ev, TODAY)


def test_passed_deadline_not_actionable():
    ev = make(registration_status="open", registration_deadline="2026-11-01")
    assert not is_actionable(ev, TODAY)


def test_unknown_status_on_future_event_is_actionable():
    assert is_actionable(make(registration_status="unknown"), TODAY)


def test_merge_fills_gaps_and_keeps_stronger_status():
    a = make(registration_status="unknown")
    b = make(registration_status="open", location="San Jose", registration_url="https://example.com/reg")
    merge(a, b)
    assert a.registration_status == "open"
    assert a.location == "San Jose"
    assert a.registration_url == "https://example.com/reg"


def test_merge_does_not_downgrade_open():
    a = make(registration_status="open", location="线上")
    b = make(registration_status="unknown", location="北京")
    merge(a, b)
    assert a.registration_status == "open"
    assert a.location == "线上"


def test_stale_year_in_title_is_not_actionable():
    ev = make(title="Powerful Devs Conference 2025", start_date=None, end_date=None, registration_status="open")
    assert not is_actionable(ev, TODAY)


def test_current_year_title_is_actionable():
    ev = make(title="NVIDIA GTC 2026", start_date=None, end_date=None, registration_status="open")
    assert is_actionable(ev, TODAY)


# ---- 地域判定 ----
from src.filtering import is_domestic


def test_domestic_by_location():
    ev = make(location="上海世博中心", title="外滩大会")
    assert is_domestic(ev, "cn")


def test_domestic_by_english_city():
    ev = make(location="Shanghai", title="NVIDIA GTC China")
    assert is_domestic(ev, "global")


def test_foreign_by_location():
    ev = make(location="San Jose", title="NVIDIA GTC")
    assert not is_domestic(ev, "global")


def test_foreign_by_organizer_when_online():
    ev = make(location="", organizer="Anthropic", title="The Briefing")
    assert not is_domestic(ev, "global")


def test_china_in_title_wins():
    ev = make(location="", organizer="PyTorch Foundation", title="PyTorch Day China 2026")
    assert is_domestic(ev, "global")


def test_cn_source_counts_as_domestic_when_no_location():
    ev = make(location="", organizer="SegmentFault", title="MatrixNEXT 2026 观众报名")
    assert is_domestic(ev, "cn")
