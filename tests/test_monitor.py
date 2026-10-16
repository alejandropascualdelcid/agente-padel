import json
import sys
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor_padel as mp  # noqa: E402

RID_A = "aaaaaaaa-0000-0000-0000-000000000001"
RID_B = "bbbbbbbb-0000-0000-0000-000000000002"
TENANT_2 = "dddddddd-0000-0000-0000-000000000004"
NOW = datetime(2026, 10, 15, 10, 0, tzinfo=mp.TZ)  # jueves
COSLADA = mp.Club(**mp.DEFAULT_CLUB)
OTHER = mp.Club(tenant_id=TENANT_2, name="Otro Club", page_url="https://playtomic.com/clubs/otro-club")


@pytest.fixture(autouse=True)
def no_bundled_config(tmp_path, monkeypatch):
    # Los tests no deben leer el config.json real del repo.
    monkeypatch.setattr(mp, "BUNDLED_CONFIG", tmp_path / "no-existe.json")


def settings(tmp_path):
    return mp.Settings(
        bot_token="T", chat_id="1", interval=60,
        state_file=tmp_path / "state.json", config_file=tmp_path / "config.json",
    )


def write_prefs(tmp_path, **kw):
    prefs = mp.Prefs(**kw)
    mp.write_json(tmp_path / "config.json", prefs.to_dict())
    return prefs


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
    '<html><head><meta property="og:title" content="Otro Club | Playtomic"></head>'
    '<script>self.__next_f.push([1,"{\\"tenantId\\":\\"' + TENANT_2 + '\\",\\"resources\\":['
    '{\\"resourceId\\":\\"' + RID_A + '\\",\\"name\\":\\"Padel 6\\",\\"sport\\":\\"PADEL\\",'
    '\\"features\\":[\\"indoor\\",\\"double\\"]},'
    '{\\"resourceId\\":\\"' + RID_B + '\\",\\"name\\":\\"Padel 1\\",\\"sport\\":\\"PADEL\\",'
    '\\"features\\":[\\"outdoor\\"]},'
    '{\\"resourceId\\":\\"cccccccc-0000-0000-0000-000000000003\\",\\"name\\":\\"Tenis 1\\",'
    '\\"sport\\":\\"TENNIS\\",\\"features\\":[]}'
    ']}"])</script></html>'
)


class FakeServer:
    """Simula Playtomic y Telegram. Guarda lo enviado al bot y sirve updates encolados."""

    def __init__(self, avail=availability, fail_dates=(), other_tenants=None):
        self.avail = avail
        self.other_tenants = other_tenants or {}  # tenant_id -> respuesta fija
        self.fail_dates = set(fail_dates)
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.updates: list[dict] = []
        self.availability_calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request):
        url = str(request.url)
        if "api.telegram.org" in url:
            method = url.rsplit("/", 1)[1]
            body = json.loads(request.content or b"{}")
            if method == "getUpdates":
                pending = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
                return httpx.Response(200, json={"ok": True, "result": pending})
            if method == "sendMessage":
                self.sent.append(body)
            elif method == "editMessageText":
                self.edited.append(body)
            return httpx.Response(200, json={"ok": True, "result": True})
        if "availability" in url:
            d, tenant = request.url.params["date"], request.url.params["tenant_id"]
            self.availability_calls.append((tenant, d))
            if d in self.fail_dates:
                return httpx.Response(403)
            if tenant in self.other_tenants:
                return httpx.Response(200, json=self.other_tenants[tenant])
            return httpx.Response(200, json=self.avail(d))
        if "/clubs/" in url or "/tenant/" in url:
            return httpx.Response(200, text=CLUB_HTML)
        return httpx.Response(404)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def texts(self):
        return [m["text"] for m in self.sent]


def message(update_id, text, chat=1):
    return {"update_id": update_id, "message": {"chat": {"id": chat}, "text": text}}


def callback(update_id, data, chat=1):
    return {
        "update_id": update_id,
        "callback_query": {"id": f"cb{update_id}", "data": data,
                           "message": {"chat": {"id": chat}, "message_id": 99}},
    }


# --------------------------------------------------------------------------- #
# Filtro y formato
# --------------------------------------------------------------------------- #
def test_target_days_only_selected_weekdays():
    days = mp.target_days(date(2026, 10, 15), mp.Prefs())
    assert all(d.weekday() < 5 for d in days)
    assert days[0] == date(2026, 10, 15) and days[-1] == date(2026, 10, 27)
    assert len(days) == 9
    weekend = mp.target_days(date(2026, 10, 15), mp.Prefs(weekdays=[5, 6], days_ahead=7))
    assert [d.isoformat() for d in weekend] == ["2026-10-17", "2026-10-18"]


def test_filter_converts_utc_to_madrid():
    names = {RID_A: "Padel 6", RID_B: "Padel 1"}
    slots = mp.filter_slots(availability("2026-10-16"), COSLADA, date(2026, 10, 16), mp.Prefs(), names, NOW)
    assert [(s.start, s.court) for s in slots] == [("18:30", "Padel 6"), ("20:00", "Padel 1"), ("21:00", "Padel 6")]


def test_dst_change_real_case():
    """Caso real: el turno de las 21:30 llegaba como 19:30 (CEST) y 20:30 (CET)."""
    prefs = mp.Prefs()
    for day, utc in ((date(2026, 10, 22), "19:30:00"), (date(2026, 10, 26), "20:30:00")):
        raw = [{"resource_id": RID_A, "start_date": day.isoformat(),
                "slots": [{"start_time": utc, "duration": 90, "price": "4.29 EUR"}]}]
        assert mp.filter_slots(raw, COSLADA, day, prefs, {}, NOW) == []
    raw = [{"resource_id": RID_A, "start_date": "2026-10-26",
            "slots": [{"start_time": "17:30:00", "duration": 90, "price": "4.29 EUR"}]}]
    assert [s.start for s in mp.filter_slots(raw, COSLADA, date(2026, 10, 26), prefs, {}, NOW)] == ["18:30"]


def test_filter_skips_past_slots_today():
    now = datetime(2026, 10, 15, 19, 0, tzinfo=mp.TZ)
    slots = mp.filter_slots(availability("2026-10-15"), COSLADA, date(2026, 10, 15), mp.Prefs(), {}, now)
    assert [s.start for s in slots] == ["20:00", "21:00"]


def test_filter_multiple_durations():
    prefs = mp.Prefs(durations=[60, 90])
    slots = mp.filter_slots(availability("2026-10-16"), COSLADA, date(2026, 10, 16), prefs, {}, NOW)
    assert ("20:30", 60) in [(s.start, s.duration) for s in slots]


def test_message_grouped_by_club_and_sorted():
    names = {RID_A: "Padel 11", RID_B: "Padel 3"}
    raw = [{"resource_id": rid, "start_date": "2026-10-16",
            "slots": [{"start_time": "18:00:00", "duration": 90, "price": "4.29 EUR"}]}
           for rid in (RID_A, RID_B)]
    prefs = mp.Prefs()
    slots = (mp.filter_slots(raw, COSLADA, date(2026, 10, 16), prefs, names, NOW)
             + mp.filter_slots(raw, OTHER, date(2026, 10, 16), prefs, names, NOW))
    text = mp.format_messages(slots, prefs)[0]
    assert text.count("🏟") == 2
    assert text.index("Coslada Dreamfit") < text.index("Otro Club")
    assert text.index("Padel 3") < text.index("Padel 11")
    assert f"https://playtomic.com/tenant/{TENANT_2}?date=2026-10-16" in text


# --------------------------------------------------------------------------- #
# Página del club
# --------------------------------------------------------------------------- #
def test_parse_club_page():
    assert mp.parse_court_names(CLUB_HTML) == {RID_A: "Padel 6", RID_B: "Padel 1"}
    assert mp.parse_club_name(CLUB_HTML) == "Otro Club"
    assert mp.tenant_candidates(CLUB_HTML)[0] == TENANT_2


@pytest.mark.parametrize("link", [
    f"Checkout the club! https://playtomic.com/tenant/{TENANT_2}?utm_source=app_ios&utm_campaign=share",
    "https://playtomic.com/clubs/otro-club",
])
def test_resolve_club_from_links(link):
    srv = FakeServer()
    with srv.client() as client:
        club = mp.resolve_club(client, link)
    assert (club.tenant_id, club.name) == (TENANT_2, "Otro Club")


# --------------------------------------------------------------------------- #
# Ciclo completo y anti-spam
# --------------------------------------------------------------------------- #
def test_run_once_notifies_then_dedups(tmp_path):
    s = settings(tmp_path)
    srv = FakeServer()
    with srv.client() as client:
        assert mp.run_once(s, client, now=NOW) == 0
        assert len(srv.sent) == 1
        text = srv.sent[0]["text"]
        assert "🏟 *Coslada Dreamfit*" in text and "📅 *Jue 15/10*" in text
        assert "• 18:30 ➜ Padel 6 (4.29 EUR)" in text  # nombres desde la web del club
        assert "Sáb" not in text and "17:30" not in text
        assert srv.sent[0]["parse_mode"] == "Markdown"
        assert mp.run_once(s, client, now=NOW) == 0
    assert len(srv.sent) == 1  # sin repetir


def test_failed_day_does_not_renotify(tmp_path):
    s = settings(tmp_path)
    srv = FakeServer()
    with srv.client() as client:
        mp.run_once(s, client, now=NOW)
        srv.fail_dates = {"2026-10-16"}
        mp.run_once(s, client, now=NOW)
        srv.fail_dates = set()
        mp.run_once(s, client, now=NOW)
    assert len(srv.sent) == 1


def test_slot_freed_again_is_renotified(tmp_path):
    s = settings(tmp_path)
    srv = FakeServer()
    with srv.client() as client:
        mp.run_once(s, client, now=NOW)
        srv.avail = lambda d: []
        mp.run_once(s, client, now=NOW)
        srv.avail = availability
        mp.run_once(s, client, now=NOW)
    assert len(srv.sent) == 2


def test_all_failing_returns_error(tmp_path):
    s = settings(tmp_path)
    days = {d.isoformat() for d in mp.target_days(NOW.date(), mp.Prefs())}
    with FakeServer(fail_dates=days).client() as client:
        assert mp.run_once(s, client, now=NOW) == 1


def test_only_active_clubs_are_scanned(tmp_path):
    s = settings(tmp_path)
    write_prefs(tmp_path, clubs=[COSLADA, mp.Club(**{**OTHER.__dict__, "enabled": False})])
    srv = FakeServer()
    with srv.client() as client:
        mp.run_once(s, client, now=NOW)
    assert {t for t, _ in srv.availability_calls} == {COSLADA.tenant_id}


def test_paused_does_not_scan(tmp_path):
    s = settings(tmp_path)
    write_prefs(tmp_path, paused=True)
    srv = FakeServer()
    with srv.client() as client:
        mp.run_once(s, client, now=NOW)
    assert srv.availability_calls == [] and srv.sent == []


# --------------------------------------------------------------------------- #
# Bot de Telegram
# --------------------------------------------------------------------------- #
def test_menu_command_and_buttons(tmp_path):
    s = settings(tmp_path)
    write_prefs(tmp_path, clubs=[COSLADA, OTHER])
    srv = FakeServer()
    srv.updates = [
        message(1, "/menu"),
        callback(2, f"club:{TENANT_2}:0"),
        callback(3, "day:5:1"),
        callback(4, "fr:min:19:00"),
        callback(5, "du:60:1"),
        callback(6, "pause:1"),
    ]
    with srv.client() as client:
        mp.run_once(s, client, now=NOW)
    assert "Configuración del monitor" in srv.texts()[0]
    prefs = mp.load_prefs(s.config_file)
    assert prefs.club(TENANT_2).enabled is False
    assert prefs.weekdays == [0, 1, 2, 3, 4, 5]
    assert (prefs.start_min, prefs.start_max) == ("19:00", "21:00")
    assert prefs.durations == [60, 90]
    assert prefs.paused is True
    assert len(srv.edited) == 5
    assert srv.availability_calls == []  # en pausa
    assert mp.load_state(s.state_file)["tg_offset"] == 7


def test_replayed_updates_are_idempotent(tmp_path):
    s = settings(tmp_path)
    write_prefs(tmp_path, clubs=[COSLADA, OTHER])
    srv = FakeServer()
    srv.updates = [callback(1, f"club:{TENANT_2}:0")]
    with srv.client() as client:
        mp.run_once(s, client, now=NOW)
        s.state_file.unlink()  # se pierde la caché: Telegram reentrega el mismo botón
        mp.run_once(s, client, now=NOW)
    assert mp.load_prefs(s.config_file).club(TENANT_2).enabled is False


def test_ignores_other_chats(tmp_path):
    s = settings(tmp_path)
    write_prefs(tmp_path, clubs=[COSLADA, OTHER])
    srv = FakeServer()
    srv.updates = [callback(1, f"club:{TENANT_2}:0", chat=666), message(2, "/pausa", chat=666)]
    with srv.client() as client:
        mp.run_once(s, client, now=NOW)
    prefs = mp.load_prefs(s.config_file)
    assert prefs.club(TENANT_2).enabled and not prefs.paused


def test_add_club_by_sharing_link(tmp_path):
    s = settings(tmp_path)
    srv = FakeServer()
    srv.updates = [message(1, f"Checkout the club! https://playtomic.com/tenant/{TENANT_2}?utm_source=app_ios")]
    with srv.client() as client:
        mp.run_once(s, client, now=NOW)
    prefs = mp.load_prefs(s.config_file)
    assert [c.name for c in prefs.clubs] == ["Coslada Dreamfit", "Otro Club"]
    assert any("Añadido *Otro Club*" in t for t in srv.texts())


def test_buscar_sends_full_listing_even_if_already_notified(tmp_path):
    s = settings(tmp_path)
    srv = FakeServer()
    with srv.client() as client:
        mp.run_once(s, client, now=NOW)
        srv.updates = [message(1, "/buscar")]
        mp.run_once(s, client, now=NOW)
    assert any("Pistas libres ahora" in t for t in srv.texts())


def test_franja_command(tmp_path):
    s = settings(tmp_path)
    srv = FakeServer()
    srv.updates = [message(1, "/franja 18:00 21:30"), message(2, "/franja 22:00 18:00")]
    with srv.client() as client:
        mp.run_once(s, client, now=NOW)
    prefs = mp.load_prefs(s.config_file)
    assert (prefs.start_min, prefs.start_max) == ("18:00", "21:30")
    assert any("Formato" in t for t in srv.texts())


def test_delete_club_asks_confirmation(tmp_path):
    s = settings(tmp_path)
    write_prefs(tmp_path, clubs=[COSLADA, OTHER])
    srv = FakeServer()
    srv.updates = [callback(1, f"delask:{TENANT_2}")]
    with srv.client() as client:
        mp.run_once(s, client, now=NOW)
        assert len(mp.load_prefs(s.config_file).clubs) == 2  # aún no se borra
        assert "¿Seguro" in srv.edited[-1]["text"]
        srv.updates = [callback(2, f"del:{TENANT_2}")]
        mp.run_once(s, client, now=NOW)
    assert [c.name for c in mp.load_prefs(s.config_file).clubs] == ["Coslada Dreamfit"]


def test_add_club_cli_with_custom_name(tmp_path):
    s = settings(tmp_path)
    with FakeServer().client() as client:
        assert mp.add_club_cli(s, client, "https://playtomic.com/clubs/otro-club", "ProPadel") == 0
    assert mp.load_prefs(s.config_file).club(TENANT_2).name == "ProPadel"


def test_slug_link_ignores_recommended_clubs(tmp_path):
    """La página /clubs/<slug> enseña otros clubes; hay que elegir el que tiene sus pistas."""
    recommended = "eeeeeeee-0000-0000-0000-000000000005"
    page = CLUB_HTML.replace("</html>", ('<script>{\\"tenantId\\":\\"' + recommended + '\\"}</script>') * 5 + "</html>")
    assert mp.tenant_candidates(page)[0] == recommended  # el más repetido es el equivocado
    other_courts = [{"resource_id": "ffffffff-0000-0000-0000-000000000006", "start_date": "2026-10-16", "slots": []}]
    srv = FakeServer(other_tenants={recommended: other_courts})
    with srv.client() as client:
        assert mp.find_tenant_id(client, page) == TENANT_2


def test_durations_merged_in_one_line():
    prefs = mp.Prefs(durations=[90, 120])
    raw = [{"resource_id": RID_A, "start_date": "2026-10-16", "slots": [
        {"start_time": "16:30:00", "duration": 90, "price": "36.66 EUR"},
        {"start_time": "16:30:00", "duration": 120, "price": "48.88 EUR"},
    ]}]
    slots = mp.filter_slots(raw, COSLADA, date(2026, 10, 16), prefs, {RID_A: "Pista 1"}, NOW)
    text = mp.format_messages(slots, prefs)[0]
    assert "• 18:30 ➜ Pista 1 · 90'/120' (36.66 / 48.88 EUR)" in text
    assert text.count("Pista 1") == 1


def test_clubs_with_same_name_not_interleaved():
    prefs = mp.Prefs()
    twin = mp.Club(tenant_id=TENANT_2, name=COSLADA.name, page_url="")
    slots = []
    for d in ("2026-10-16", "2026-10-19"):
        day = date.fromisoformat(d)
        slots += mp.filter_slots(availability(d), COSLADA, day, prefs, {}, NOW)
        slots += mp.filter_slots(availability(d), twin, day, prefs, {}, NOW)
    text = mp.format_messages(slots, prefs)[0]
    assert text.count("🏟") == 2


def test_cloudflare_block_stops_scan_and_keeps_state(tmp_path):
    s = settings(tmp_path)
    srv = FakeServer()
    with srv.client() as client:
        mp.run_once(s, client, now=NOW)
        calls_before = len(srv.availability_calls)
        srv.fail_dates = {d.isoformat() for d in mp.target_days(NOW.date(), mp.Prefs())}
        assert mp.run_once(s, client, now=NOW) == 1
        assert len(srv.availability_calls) - calls_before == 1  # no insiste tras el 403
        assert mp.load_state(s.state_file)["blocked"] is True
        srv.fail_dates = set()
        mp.run_once(s, client, now=NOW)
    assert len(srv.sent) == 1  # nada se re-avisa tras el bloqueo


def test_bot_never_writes_repo_config(monkeypatch):
    monkeypatch.setenv("CONFIG_FILE", "config.json")
    monkeypatch.chdir(Path(mp.__file__).resolve().parent)
    assert mp.Settings.from_env().config_file.name == "config.local.json"
    monkeypatch.setenv("CONFIG_FILE", "/data/config.json")
    assert str(mp.Settings.from_env().config_file) == "/data/config.json"
