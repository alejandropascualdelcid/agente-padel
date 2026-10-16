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
    # Horas en UTC, como las devuelve la API. Comentarios = hora de Madrid en verano (+2).
    return [
        {
            "resource_id": RID_A,
            "start_date": day,
            "slots": [
                {"start_time": "15:30:00", "duration": 90, "price": "4.29 EUR"},  # 17:30 fuera
                {"start_time": "16:30:00", "duration": 90, "price": "4.29 EUR"},  # 18:30
                {"start_time": "18:30:00", "duration": 60, "price": "3 EUR"},     # duración != 90
                {"start_time": "19:00:00", "duration": 90, "price": "4.29 EUR"},  # 21:00 incluido
                {"start_time": "19:30:00", "duration": 90, "price": "4.29 EUR"},  # 21:30 fuera
            ],
        },
        {
            "resource_id": RID_B,
            "start_date": day,
            "slots": [{"start_time": "18:00:00", "duration": 90, "price": "4.29 EUR"}],  # 20:00
        },
    ]


CLUB_HTML = (
    '<html><script>self.__next_f.push([1,"{\\"resources\\":['
    '{\\"resourceId\\":\\"' + RID_A + '\\",\\"name\\":\\"Padel 6\\",\\"sport\\":\\"PADEL\\",'
    '\\"features\\":[\\"indoor\\",\\"double\\"]},'
    '{\\"resourceId\\":\\"' + RID_B + '\\",\\"name\\":\\"Padel 1\\",\\"sport\\":\\"PADEL\\",'
    '\\"features\\":[\\"outdoor\\"]},'
    '{\\"resourceId\\":\\"cccccccc-0000-0000-0000-000000000003\\",\\"name\\":\\"Tenis 1\\",'
    '\\"sport\\":\\"TENNIS\\",\\"features\\":[]}'
    ']}"])</script></html>'
)


def test_parse_court_names_from_club_page():
    assert mp.parse_court_names(CLUB_HTML) == {RID_A: "Padel 6", RID_B: "Padel 1"}


def make_client(sent, avail=availability, fail_dates=()):
    def handler(request: httpx.Request):
        url = str(request.url)
        if "api.telegram.org" in url:
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if url == mp.CLUB_URL:
            return httpx.Response(200, text=CLUB_HTML)
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


def test_filter_converts_utc_to_madrid(tmp_path):
    now = datetime(2026, 10, 15, 10, 0, tzinfo=mp.TZ)
    names = {RID_A: "Padel 6", RID_B: "Padel 1"}
    slots = mp.filter_slots(availability("2026-10-16"), date(2026, 10, 16), cfg(tmp_path), names, now)
    assert [(s.start, s.court) for s in slots] == [("18:30", "Padel 6"), ("20:00", "Padel 1"), ("21:00", "Padel 6")]


def test_dst_change_real_case(tmp_path):
    """Caso real: el turno de las 21:30 llegaba como 19:30 (CEST) y 20:30 (CET)."""
    now = datetime(2026, 10, 16, 2, 0, tzinfo=mp.TZ)
    c = cfg(tmp_path)
    for day, utc in ((date(2026, 10, 22), "19:30:00"), (date(2026, 10, 26), "20:30:00")):
        raw = [{"resource_id": RID_A, "start_date": day.isoformat(),
                "slots": [{"start_time": utc, "duration": 90, "price": "4.29 EUR"}]}]
        assert mp.filter_slots(raw, day, c, {}, now) == []
    # Tras el cambio de hora, 18:30 en Madrid es 17:30 UTC.
    raw = [{"resource_id": RID_A, "start_date": "2026-10-26",
            "slots": [{"start_time": "17:30:00", "duration": 90, "price": "4.29 EUR"}]}]
    assert [s.start for s in mp.filter_slots(raw, date(2026, 10, 26), c, {}, now)] == ["18:30"]


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
