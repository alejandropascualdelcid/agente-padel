import json
import sys
from datetime import date, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor_padel as mp  # noqa: E402

RID_A = "aaaaaaaa-0000-0000-0000-000000000001"
RID_B = "bbbbbbbb-0000-0000-0000-000000000002"


def cfg(tmp_path, **kw):
    base = dict(
        bot_token="T", chat_id="1", days_ahead=12, start_min="18:30", start_max="21:00",
        duration=90, interval=60, state_file=tmp_path / "state.json", court_names={},
    )
    base.update(kw)
    return mp.Config(**base)


def availability(day: str):
    return [
        {
            "resource_id": RID_A,
            "start_date": day,
            "slots": [
                {"start_time": "17:30:00", "duration": 90, "price": "4.29 EUR"},  # fuera de franja
                {"start_time": "18:30:00", "duration": 90, "price": "4.29 EUR"},
                {"start_time": "20:30:00", "duration": 60, "price": "3 EUR"},     # duración != 90
                {"start_time": "21:00:00", "duration": 90, "price": "4.29 EUR"},  # límite incluido
                {"start_time": "21:30:00", "duration": 90, "price": "4.29 EUR"},  # fuera
            ],
        },
        {
            "resource_id": RID_B,
            "start_date": day,
            "slots": [{"start_time": "20:00:00", "duration": 90, "price": "4.29 EUR"}],
        },
    ]


def make_client(sent, avail=availability, fail_dates=()):
    def handler(request: httpx.Request):
        url = str(request.url)
        if "api.telegram.org" in url:
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if "/resources" in url:
            return httpx.Response(200, json=[
                {"resource_id": RID_A, "name": "Padel 6", "sport_id": "PADEL"},
                {"resource_id": RID_B, "name": "Padel 1", "sport_id": "PADEL"},
            ])
        if "availability" in url:
            d = request.url.params["date"]
            if d in fail_dates:
                return httpx.Response(403)
            return httpx.Response(200, json=avail(d))
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_target_days_only_weekdays():
    days = mp.target_days(date(2026, 10, 15), 12)  # jueves
    assert all(d.weekday() < 5 for d in days)
    assert days[0] == date(2026, 10, 15) and days[-1] == date(2026, 10, 27)
    assert len(days) == 9


def test_filter_no_timezone_shift(tmp_path):
    now = datetime(2026, 10, 15, 10, 0, tzinfo=mp.TZ)
    names = {RID_A: "Padel 6", RID_B: "Padel 1"}
    slots = mp.filter_slots(availability("2026-10-16"), date(2026, 10, 16), cfg(tmp_path), names, now)
    assert [(s.start, s.court) for s in slots] == [("18:30", "Padel 6"), ("20:00", "Padel 1"), ("21:00", "Padel 6")]


def test_filter_skips_past_slots_today(tmp_path):
    now = datetime(2026, 10, 15, 19, 0, tzinfo=mp.TZ)
    slots = mp.filter_slots(availability("2026-10-15"), date(2026, 10, 15), cfg(tmp_path), {}, now)
    assert [s.start for s in slots] == ["20:00", "21:00"]


def test_run_once_notifies_then_dedups(tmp_path):
    c = cfg(tmp_path)
    now = datetime(2026, 10, 15, 10, 0, tzinfo=mp.TZ)
    sent = []
    with make_client(sent) as client:
        assert mp.run_once(c, client, now=now) == 0
        assert len(sent) == 1
        text = sent[0]["text"]
        assert "📅 *Jue 15/10*" in text
        assert "• 18:30 ➜ Padel 6 (4.29 EUR)" in text
        assert "[Reservar Lun 19/10](https://playtomic.com/clubs/coslada-dreamfit?date=2026-10-19)" in text
        assert "Sáb" not in text and "17:30" not in text
        assert sent[0]["parse_mode"] == "Markdown"

        assert mp.run_once(c, client, now=now) == 0
        assert len(sent) == 1  # sin repetir


def test_failed_day_does_not_renotify(tmp_path):
    c = cfg(tmp_path)
    now = datetime(2026, 10, 15, 10, 0, tzinfo=mp.TZ)
    sent = []
    with make_client(sent) as client:
        mp.run_once(c, client, now=now)
    with make_client(sent, fail_dates={"2026-10-16"}) as client:
        mp.run_once(c, client, now=now)
    with make_client(sent) as client:
        mp.run_once(c, client, now=now)
    assert len(sent) == 1


def test_slot_freed_again_is_renotified(tmp_path):
    c = cfg(tmp_path)
    now = datetime(2026, 10, 15, 10, 0, tzinfo=mp.TZ)
    sent = []
    with make_client(sent) as client:
        mp.run_once(c, client, now=now)
    with make_client(sent, avail=lambda d: []) as client:
        mp.run_once(c, client, now=now)  # todo reservado
    with make_client(sent) as client:
        mp.run_once(c, client, now=now)  # se liberan de nuevo
    assert len(sent) == 2


def test_all_days_failing_returns_error(tmp_path):
    c = cfg(tmp_path)
    now = datetime(2026, 10, 15, 10, 0, tzinfo=mp.TZ)
    days = {d.isoformat() for d in mp.target_days(now.date(), 12)}
    with make_client([], fail_dates=days) as client:
        assert mp.run_once(c, client, now=now) == 1
