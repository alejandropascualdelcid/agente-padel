#!/usr/bin/env python3
"""Monitor de pistas de pádel libres en clubes de Playtomic, con avisos y menú por Telegram.

Uso:
    python monitor_padel.py --once            # una pasada (cron / GitHub Actions)
    python monitor_padel.py --loop            # daemon: escanea cada INTERVAL_SECONDS y
                                              # responde al bot de Telegram al momento
    python monitor_padel.py --once --dry-run  # imprime el mensaje en vez de enviarlo
    python monitor_padel.py --add-club URL    # añade un club (enlace de Playtomic)

Variables de entorno (o fichero .env en el mismo directorio):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (obligatorias salvo en --dry-run)
    INTERVAL_SECONDS=60, STATE_FILE=state.json, CONFIG_FILE=config.json, API_TIME_ZONE=UTC

Las preferencias (clubes, días, franja, duración, pausa) viven en CONFIG_FILE y se
cambian desde el bot de Telegram (/menu) o editando el fichero.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #
SPORT_ID = "PADEL"
PLAYTOMIC = "https://playtomic.com"
AVAILABILITY_URL = f"{PLAYTOMIC}/api/clubs/availability"
TELEGRAM_API = "https://api.telegram.org"
TZ = ZoneInfo("Europe/Madrid")
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=8.0)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": f"{PLAYTOMIC}/",
}

WEEKDAYS_ES = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")
WEEKDAY_LETTERS = ("L", "M", "X", "J", "V", "S", "D")
DURATION_OPTIONS = (60, 90, 120)
DAYS_AHEAD_OPTIONS = (7, 12, 14)
BLOCKED_BACKOFF = 300  # segundos de espera tras un 403 de Cloudflare en modo --loop
TELEGRAM_MAX_LEN = 4000  # límite real 4096; dejamos margen

DEFAULT_CLUB = {
    "tenant_id": "a328dd8a-031b-4230-adc5-928a53d16ad6",
    "name": "Coslada Dreamfit",
    "page_url": f"{PLAYTOMIC}/clubs/coslada-dreamfit",
    "enabled": True,
}

log = logging.getLogger("monitor_padel")


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path) -> None:
    """Carga un .env sencillo (KEY=VALUE) sin pisar variables ya definidas."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Settings:
    """Lo que no cambia en tiempo de ejecución (secretos y rutas)."""

    bot_token: str
    chat_id: str
    interval: int
    state_file: Path
    config_file: Path
    api_tz: ZoneInfo = ZoneInfo("UTC")
    request_delay: float = 0.0  # pausa entre consultas a Playtomic (evita el bloqueo de Cloudflare)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            interval=int(os.getenv("INTERVAL_SECONDS", "60")),
            state_file=Path(os.getenv("STATE_FILE", "state.json")),
            config_file=Path(os.getenv("CONFIG_FILE", "config.json")),
            api_tz=ZoneInfo(os.getenv("API_TIME_ZONE", "UTC")),
            request_delay=float(os.getenv("REQUEST_DELAY", "0.5")),
        )


@dataclass
class Club:
    tenant_id: str
    name: str
    page_url: str
    enabled: bool = True

    @property
    def booking_url(self) -> str:
        # Formato que genera la app al compartir el club: en el móvil abre la app.
        return f"{PLAYTOMIC}/tenant/{self.tenant_id}"


@dataclass
class Prefs:
    """Preferencias editables desde Telegram. Se guardan en config.json."""

    clubs: list[Club] = field(default_factory=lambda: [Club(**DEFAULT_CLUB)])
    weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    start_min: str = "18:30"
    start_max: str = "21:00"
    durations: list[int] = field(default_factory=lambda: [90])
    days_ahead: int = 12
    paused: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> Prefs:
        base = cls()
        clubs = [Club(**c) for c in data.get("clubs", [])] if "clubs" in data else base.clubs
        return cls(
            clubs=clubs,
            weekdays=sorted(set(data.get("weekdays", base.weekdays))),
            start_min=data.get("start_min", base.start_min),
            start_max=data.get("start_max", base.start_max),
            durations=sorted(set(data.get("durations", base.durations))),
            days_ahead=int(data.get("days_ahead", base.days_ahead)),
            paused=bool(data.get("paused", base.paused)),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def club(self, tenant_id: str) -> Club | None:
        return next((c for c in self.clubs if c.tenant_id == tenant_id), None)

    @property
    def active_clubs(self) -> list[Club]:
        return [c for c in self.clubs if c.enabled]


def load_prefs(path: Path) -> Prefs:
    """Lee config.json; si no existe usa el del repo junto al script, o los valores por defecto."""
    for candidate in (path, Path(__file__).with_name("config.json")):
        if candidate.is_file():
            try:
                return Prefs.from_dict(json.loads(candidate.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError) as exc:
                log.error("config ilegible en %s (%s); uso valores por defecto", candidate, exc)
    return Prefs()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Estado ilegible (%s), se empieza de cero", exc)
        return {}


# --------------------------------------------------------------------------- #
# Modelo
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Slot:
    club: Club
    day: date
    start: str  # "HH:MM" en hora de Madrid
    court: str
    resource_id: str
    duration: int
    price: str

    @property
    def key(self) -> str:
        # La fecha va primero para poder purgar y conservar por (día, club).
        return f"{self.day.isoformat()}|{self.club.tenant_id}|{self.start}|{self.resource_id}|{self.duration}"


def slot_sort_key(slot: Slot):
    """Club, día, hora y número de pista como número (Padel 3 antes que Padel 11)."""
    digits = "".join(ch for ch in slot.court if ch.isdigit())
    return (slot.club.name, slot.club.tenant_id, slot.day, slot.start,
            int(digits) if digits else 999, slot.court, slot.duration)


# --------------------------------------------------------------------------- #
# HTTP Playtomic
# --------------------------------------------------------------------------- #
class Blocked(httpx.HTTPError):
    """Cloudflare (403): Playtomic nos está frenando. Hay que parar y esperar."""


def get_json(client: httpx.Client, url: str, params: dict | None = None, retries: int = 2):
    """GET con reintentos para 429/5xx/errores de red. Lanza la última excepción."""
    for attempt in range(retries + 1):
        try:
            resp = client.get(url, params=params)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 403:
                raise Blocked("Playtomic ha bloqueado temporalmente las consultas (403)") from exc
            if attempt >= retries:
                raise
            time.sleep(2 ** attempt)


# --------------------------------------------------------------------------- #
# Página del club: nombres de pistas y datos para añadir clubes
# --------------------------------------------------------------------------- #
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_RESOURCE_OBJ = re.compile(r'\{[^{}]*?"resource_?[iI]d"\s*:\s*"(' + _UUID + r')"[^{}]*\}')
_NAME = re.compile(r'"name"\s*:\s*"([^"]+)"')
_SPORT = re.compile(r'"sport(?:_id)?"\s*:\s*"([^"]+)"')
_TENANT_ID = re.compile(r'"tenant_?[iI]d"\s*:\s*"(' + _UUID + r')"')
_OG_TITLE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', re.I)
_TITLE = re.compile(r"<title[^>]*>([^<]+)</title>", re.I)
_CLUB_LINK = re.compile(
    r"https?://(?:www\.|app\.)?playtomic\.(?:com|io)/(?:tenant/(" + _UUID + r")|clubs/([\w-]+))",
    re.I,
)


def _unescape_payload(html: str) -> str:
    # La página lleva los datos como JSON escapado dentro de un <script> (\"name\":\"...\").
    return html.replace('\\"', '"')


def parse_court_names(html: str) -> dict[str, str]:
    """Extrae resourceId -> name ("Padel 7") del payload embebido en la web del club."""
    text = _unescape_payload(html)
    names: dict[str, str] = {}
    for m in _RESOURCE_OBJ.finditer(text):
        obj = m.group(0)
        name = _NAME.search(obj)
        sport = _SPORT.search(obj)
        if not name or (sport and sport.group(1) != SPORT_ID):
            continue
        names.setdefault(m.group(1), name.group(1).strip())
    return names


def parse_club_name(html: str) -> str | None:
    m = _OG_TITLE.search(html) or _TITLE.search(html)
    if not m:
        return None
    title = html_lib.unescape(m.group(1)).strip()
    # "Coslada Dreamfit | Playtomic" / "Coslada Dreamfit - Playtomic" -> "Coslada Dreamfit"
    title = re.split(r"\s+[|\-–]\s+", title)[0].strip()
    return title or None


def fetch_court_names(client: httpx.Client, club: Club) -> dict[str, str]:
    try:
        resp = client.get(club.page_url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("No se pudieron obtener nombres de %s: %s", club.name, exc)
        return {}
    names = parse_court_names(resp.text)
    log.info("%s: %d nombres de pistas resueltos", club.name, len(names))
    return names


def court_label(resource_id: str, names: dict[str, str]) -> str:
    return names.get(resource_id) or f"Pista {resource_id[:8]}"


def resolve_club(client: httpx.Client, url: str) -> Club:
    """Crea un Club a partir de un enlace de Playtomic (el de 'Compartir' de la app o el de la web)."""
    m = _CLUB_LINK.search(url)
    if not m:
        raise ValueError("No es un enlace de un club de Playtomic")
    tenant_id, slug = m.group(1), m.group(2)
    page = f"{PLAYTOMIC}/tenant/{tenant_id}" if tenant_id else f"{PLAYTOMIC}/clubs/{slug}"
    resp = client.get(page)
    resp.raise_for_status()
    name = parse_club_name(resp.text) or "Club"
    page_url = str(resp.url)
    if tenant_id:
        club = Club(tenant_id=tenant_id, name=name, page_url=page_url)
        fetch_availability(client, club, datetime.now(TZ).date())  # validación
        return club
    tenant_id = find_tenant_id(client, resp.text)
    return Club(tenant_id=tenant_id, name=name, page_url=page_url)


def tenant_candidates(html: str) -> list[str]:
    """UUIDs de la página que podrían ser el club: primero los marcados como tenantId,
    luego el resto, por frecuencia. Se excluyen los de las pistas."""
    text = _unescape_payload(html)
    resources = set(parse_court_names(html))
    keyed = _TENANT_ID.findall(text)
    others = [u for u in re.findall(_UUID, text) if u not in resources]
    ordered: list[str] = []
    for pool in (keyed, others):
        for u in sorted(set(pool), key=lambda x: -pool.count(x)):
            if u not in ordered and u not in resources:
                ordered.append(u)
    return ordered


def find_tenant_id(client: httpx.Client, html: str, max_candidates: int = 25) -> str:
    """Identifica el club de una página /clubs/<slug>.

    La página también enseña otros clubes (cercanos, recomendados), así que no basta
    con el identificador más repetido: se prueba cada candidato contra la API y se
    elige aquel cuyas pistas coinciden con las de la página.
    """
    page_courts = set(parse_court_names(html))
    if not page_courts:
        raise ValueError("La página no lista pistas; usa el enlace de Compartir de la app")
    today = datetime.now(TZ).date()
    for candidate in tenant_candidates(html)[:max_candidates]:
        probe = Club(tenant_id=candidate, name="", page_url="")
        for d in (today, today + timedelta(days=1)):
            try:
                raw = fetch_availability(client, probe, d)
            except (httpx.HTTPError, ValueError):
                break
            if {r.get("resource_id") for r in raw} & page_courts:
                return candidate
    raise ValueError("No he podido identificar el club; usa el enlace de Compartir de la app")


# --------------------------------------------------------------------------- #
# Disponibilidad
# --------------------------------------------------------------------------- #
def target_days(today: date, prefs: Prefs) -> list[date]:
    """Días elegidos (L-V por defecto) desde hoy hasta hoy + days_ahead (incluidos)."""
    days = (today + timedelta(days=i) for i in range(prefs.days_ahead + 1))
    return [d for d in days if d.weekday() in prefs.weekdays]


def fetch_availability(client: httpx.Client, club: Club, day: date) -> list[dict]:
    data = get_json(
        client,
        AVAILABILITY_URL,
        params={"tenant_id": club.tenant_id, "date": day.isoformat(), "sport_id": SPORT_ID},
    )
    if not isinstance(data, list):
        raise ValueError(f"Respuesta inesperada de availability: {type(data).__name__}")
    return data


def slot_local_start(start_date: str, start_time: str, api_tz: ZoneInfo) -> datetime:
    """Convierte la fecha/hora del slot (en la zona de la API, UTC) a hora de Madrid.

    Comprobado con datos reales: el mismo turno de las 21:30 aparece como 19:30
    antes del cambio de hora de octubre y como 20:30 después, así que la API
    devuelve UTC. Se convierte con zoneinfo (respeta el horario de verano),
    nunca con un desfase fijo.
    """
    naive = datetime.fromisoformat(f"{start_date[:10]}T{start_time[:8]}")
    return naive.replace(tzinfo=api_tz).astimezone(TZ)


def filter_slots(
    raw: list[dict],
    club: Club,
    day: date,
    prefs: Prefs,
    names: dict[str, str],
    now: datetime,
    api_tz: ZoneInfo = ZoneInfo("UTC"),
) -> list[Slot]:
    """Aplica el filtro estricto sobre la hora de inicio YA convertida a hora de Madrid."""
    slots: list[Slot] = []
    for resource in raw:
        rid = resource.get("resource_id", "")
        start_date = resource.get("start_date") or day.isoformat()
        for s in resource.get("slots") or []:
            if s.get("duration") not in prefs.durations or not s.get("start_time"):
                continue
            local = slot_local_start(start_date, str(s["start_time"]), api_tz)
            start = local.strftime("%H:%M")
            # Nos quedamos solo con los turnos que en Madrid caen en el día pedido.
            if local.date() != day:
                continue
            if not (prefs.start_min <= start <= prefs.start_max):
                continue
            if local <= now:
                continue
            slots.append(
                Slot(
                    club=club,
                    day=day,
                    start=start,
                    court=court_label(rid, names),
                    resource_id=rid,
                    duration=s["duration"],
                    price=str(s.get("price") or "").strip(),
                )
            )
    return sorted(slots, key=slot_sort_key)


# --------------------------------------------------------------------------- #
# Mensajes de avisos
# --------------------------------------------------------------------------- #
def md_escape(text: str) -> str:
    """Escapa caracteres especiales del Markdown 'legacy' de Telegram."""
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def booking_link(club: Club, d: date) -> str:
    return f"{club.booking_url}?date={d.isoformat()}"


def day_label(d: date) -> str:
    return f"{WEEKDAYS_ES[d.weekday()]} {d:%d/%m}"


def format_prices(variants: list[Slot]) -> str:
    """'(36.66 EUR)' o, con varias duraciones, '(36.66 / 48.88 EUR)'."""
    prices = [v.price for v in variants if v.price]
    if not prices:
        return ""
    parts = [p.rsplit(" ", 1) for p in prices]
    currencies = {pp[1] for pp in parts if len(pp) == 2}
    if len(currencies) == 1 and all(len(pp) == 2 for pp in parts):
        text = " / ".join(pp[0] for pp in parts) + " " + currencies.pop()
    else:
        text = " / ".join(prices)
    return f" ({md_escape(text)})"


def format_messages(slots: list[Slot], prefs: Prefs, title: str = "🎾 *¡Pistas libres!*") -> list[str]:
    """Agrupa por club y día; trocea en varios mensajes si excede el límite de Telegram."""
    header = f"{title}\n🕡 Inicio {prefs.start_min}–{prefs.start_max}\n"
    show_duration = len(prefs.durations) > 1
    groups: dict[tuple[str, date], list[Slot]] = {}
    for s in sorted(slots, key=slot_sort_key):
        groups.setdefault((s.club.tenant_id, s.day), []).append(s)

    messages: list[str] = []
    current, current_club = header, None
    for (_, d), day_slots in groups.items():
        club = day_slots[0].club
        lines = [f"📅 *{day_label(d)}*"]
        merged: dict[tuple[str, str], list[Slot]] = {}
        for s in day_slots:
            merged.setdefault((s.start, s.court), []).append(s)
        for (start, court), variants in merged.items():
            extra = " · " + "/".join(f"{v.duration}'" for v in variants) if show_duration else ""
            lines.append(f"• {start} ➜ {md_escape(court)}{extra}{format_prices(variants)}")
        lines.append(f"👉 [Reservar {day_label(d)}]({booking_link(club, d)})")
        block = "\n".join(lines)
        club_header = f"\n🏟 *{md_escape(club.name)}*" if club.tenant_id != current_club else ""

        if len(current) + len(club_header) + len(block) + 2 > TELEGRAM_MAX_LEN and current != header:
            messages.append(current.rstrip())
            current, current_club = header, None
            club_header = f"\n🏟 *{md_escape(club.name)}*"
        current += club_header + "\n" + block + "\n"
        current_club = club.tenant_id
    if groups:
        messages.append(current.rstrip())
    return messages


# --------------------------------------------------------------------------- #
# Telegram: API
# --------------------------------------------------------------------------- #
class TelegramError(RuntimeError):
    pass


def tg_call(client: httpx.Client, settings: Settings, method: str, http_timeout: float | None = None, **payload):
    resp = client.post(
        f"{TELEGRAM_API}/bot{settings.bot_token}/{method}",
        json=payload,
        timeout=http_timeout or HTTP_TIMEOUT,
    )
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200 or not data.get("ok"):
        # No logueamos la URL: contiene el token.
        raise TelegramError(f"{method}: {resp.status_code} {data.get('description') or resp.text[:200]}")
    return data["result"]


def send_telegram(client: httpx.Client, settings: Settings, text: str, markup: dict | None = None) -> None:
    payload = {
        "chat_id": settings.chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if markup:
        payload["reply_markup"] = markup
    tg_call(client, settings, "sendMessage", **payload)


def edit_telegram(client: httpx.Client, settings: Settings, message: dict, text: str, markup: dict) -> None:
    try:
        tg_call(
            client,
            settings,
            "editMessageText",
            chat_id=message["chat"]["id"],
            message_id=message["message_id"],
            text=text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=markup,
        )
    except TelegramError as exc:
        if "not modified" not in str(exc):
            raise


# --------------------------------------------------------------------------- #
# Telegram: menús con botones
# --------------------------------------------------------------------------- #
BOT_COMMANDS = [
    {"command": "menu", "description": "Ver y cambiar la configuración"},
    {"command": "buscar", "description": "Buscar pistas libres ahora"},
    {"command": "franja", "description": "Cambiar franja: /franja 18:30 21:00"},
    {"command": "pausa", "description": "Pausar los avisos"},
    {"command": "reanudar", "description": "Reanudar los avisos"},
    {"command": "ayuda", "description": "Cómo usar el bot"},
]

HELP_TEXT = (
    "🤖 *Monitor de pádel*\n\n"
    "/menu – configuración con botones\n"
    "/buscar – todas las pistas libres ahora mismo\n"
    "/franja 18:30 21:00 – hora de inicio desde/hasta\n"
    "/pausa · /reanudar – parar o reactivar avisos\n\n"
    "➕ *Añadir un club:* en la app de Playtomic entra en el club → *Compartir* → "
    "pega aquí el enlace."
)


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _back() -> list[dict]:
    return [_btn("⬅️ Volver", "menu")]


def _shift(hhmm: str, minutes: int) -> str:
    h, m = map(int, hhmm.split(":"))
    total = max(6 * 60, min(23 * 60 + 30, h * 60 + m + minutes))
    return f"{total // 60:02d}:{total % 60:02d}"


def menu_main(prefs: Prefs) -> tuple[str, dict]:
    active = prefs.active_clubs
    clubs = ", ".join(md_escape(c.name) for c in active) or "ninguno"
    days = " ".join(WEEKDAY_LETTERS[d] for d in prefs.weekdays) or "ninguno"
    durations = " / ".join(str(d) for d in prefs.durations)
    text = (
        "⚙️ *Configuración del monitor*\n\n"
        f"{'⏸ *En pausa*' if prefs.paused else '▶️ *Activo*'}\n"
        f"🏟 Clubes ({len(active)}/{len(prefs.clubs)}): {clubs}\n"
        f"📅 Días: {days} · próximos {prefs.days_ahead} días\n"
        f"🕡 Inicio: {prefs.start_min}–{prefs.start_max}\n"
        f"⏱ Duración: {durations} min\n\n"
        "➕ Para añadir un club, pega aquí su enlace de Playtomic."
    )
    markup = {
        "inline_keyboard": [
            [_btn("🏟 Clubes", "clubs"), _btn("📅 Días", "days")],
            [_btn("🕡 Franja", "franja"), _btn("⏱ Duración", "dur")],
            [
                _btn("▶️ Reanudar", "pause:0") if prefs.paused else _btn("⏸ Pausar", "pause:1"),
                _btn("🔎 Buscar ahora", "scan"),
            ],
        ]
    }
    return text, markup


def menu_clubs(prefs: Prefs) -> tuple[str, dict]:
    text = (
        "🏟 *Clubes*\n\nToca un club para activarlo o desactivarlo.\n"
        "➕ Para añadir otro, pega aquí su enlace de Playtomic (app → club → *Compartir*)."
    )
    rows = [
        [
            _btn(f"{'✅' if c.enabled else '▫️'} {c.name}", f"club:{c.tenant_id}:{0 if c.enabled else 1}"),
            _btn("🗑", f"delask:{c.tenant_id}"),
        ]
        for c in prefs.clubs
    ]
    return text, {"inline_keyboard": rows + [_back()]}


def menu_confirm_delete(prefs: Prefs, tenant_id: str) -> tuple[str, dict]:
    club = prefs.club(tenant_id)
    if not club:
        return menu_clubs(prefs)
    text = f"🗑 ¿Seguro que quieres eliminar *{md_escape(club.name)}*?\n\nSi solo quieres dejar de vigilarlo, mejor desactívalo."
    rows = [[_btn("Sí, eliminar", f"del:{tenant_id}"), _btn("Cancelar", "clubs")]]
    return text, {"inline_keyboard": rows}


def menu_days(prefs: Prefs) -> tuple[str, dict]:
    text = "📅 *Días de la semana*\n\nElige qué días vigilar y cuántos días vista."
    week = [
        _btn(f"{'✅' if i in prefs.weekdays else '▫️'}{WEEKDAY_LETTERS[i]}", f"day:{i}:{0 if i in prefs.weekdays else 1}")
        for i in range(7)
    ]
    ahead = [
        _btn(f"{'✅ ' if n == prefs.days_ahead else ''}{n} días", f"ahead:{n}") for n in DAYS_AHEAD_OPTIONS
    ]
    return text, {"inline_keyboard": [week[:4], week[4:], ahead, _back()]}


def menu_franja(prefs: Prefs) -> tuple[str, dict]:
    lo, hi = prefs.start_min, prefs.start_max
    text = (
        "🕡 *Franja de hora de inicio*\n\n"
        f"Desde *{lo}* hasta *{hi}* (ambas incluidas).\n"
        "Usa los botones o escribe por ejemplo /franja 18:00 21:30"
    )
    rows = [
        [_btn(f"◀ {_shift(lo, -30)}", f"fr:min:{_shift(lo, -30)}"), _btn(f"Desde {lo}", "noop"),
         _btn(f"{_shift(lo, 30)} ▶", f"fr:min:{_shift(lo, 30)}")],
        [_btn(f"◀ {_shift(hi, -30)}", f"fr:max:{_shift(hi, -30)}"), _btn(f"Hasta {hi}", "noop"),
         _btn(f"{_shift(hi, 30)} ▶", f"fr:max:{_shift(hi, 30)}")],
        _back(),
    ]
    return text, {"inline_keyboard": rows}


def menu_durations(prefs: Prefs) -> tuple[str, dict]:
    text = "⏱ *Duración de la reserva*\n\nElige una o varias duraciones."
    row = [
        _btn(f"{'✅' if d in prefs.durations else '▫️'} {d} min", f"du:{d}:{0 if d in prefs.durations else 1}")
        for d in DURATION_OPTIONS
    ]
    return text, {"inline_keyboard": [row, _back()]}


MENUS = {"menu": menu_main, "clubs": menu_clubs, "days": menu_days, "franja": menu_franja, "dur": menu_durations}


def set_franja(prefs: Prefs, start_min: str | None = None, start_max: str | None = None) -> None:
    if start_min:
        prefs.start_min = start_min
        prefs.start_max = max(prefs.start_max, start_min)
    if start_max:
        prefs.start_max = start_max
        prefs.start_min = min(prefs.start_min, start_max)


def apply_callback(prefs: Prefs, data: str) -> tuple[str, bool, str]:
    """Aplica un botón. Devuelve (menú a mostrar, preferencias cambiadas, aviso corto).

    Los botones llevan el valor final (no "alternar"), así que repetir un mismo
    callback es inofensivo si Telegram lo reentrega.
    """
    parts = data.split(":", 2)
    kind = parts[0]
    if kind in MENUS:
        return kind, False, ""
    if kind == "pause":
        prefs.paused = parts[1] == "1"
        return "menu", True, "Avisos en pausa" if prefs.paused else "Avisos reanudados"
    if kind == "club":
        club = prefs.club(parts[1])
        if club:
            club.enabled = parts[2] == "1"
        return "clubs", club is not None, ""
    if kind == "del":
        before = len(prefs.clubs)
        prefs.clubs = [c for c in prefs.clubs if c.tenant_id != parts[1]]
        return "clubs", len(prefs.clubs) != before, "Club eliminado"
    if kind == "day":
        day, on = int(parts[1]), parts[2] == "1"
        days = set(prefs.weekdays)
        days.add(day) if on else days.discard(day)
        prefs.weekdays = sorted(days)
        return "days", True, ""
    if kind == "ahead":
        prefs.days_ahead = int(parts[1])
        return "days", True, ""
    if kind == "fr":
        set_franja(prefs, **{f"start_{parts[1]}": parts[2]})
        return "franja", True, ""
    if kind == "du":
        dur, on = int(parts[1]), parts[2] == "1"
        durs = set(prefs.durations)
        durs.add(dur) if on else durs.discard(dur)
        if not durs:
            return "dur", False, "Deja al menos una duración"
        prefs.durations = sorted(durs)
        return "dur", True, ""
    return "menu", False, ""


_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _norm_hhmm(value: str) -> str | None:
    m = _HHMM.match(value.strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


@dataclass
class BotResult:
    prefs_changed: bool = False
    scan_requested: bool = False


def handle_message(
    client: httpx.Client, settings: Settings, prefs: Prefs, text: str, result: BotResult
) -> None:
    text = text.strip()
    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

    if command in ("/start", "/menu", "/estado", "/config"):
        if command == "/start":
            try:
                tg_call(client, settings, "setMyCommands", commands=BOT_COMMANDS)
            except (httpx.HTTPError, TelegramError) as exc:
                log.warning("No se pudieron registrar los comandos: %s", exc)
        send_telegram(client, settings, *menu_main(prefs))
    elif command == "/buscar":
        result.scan_requested = True
        send_telegram(client, settings, "🔎 Buscando pistas libres…")
    elif command in ("/pausa", "/reanudar"):
        prefs.paused = command == "/pausa"
        result.prefs_changed = True
        send_telegram(client, settings, *menu_main(prefs))
    elif command == "/franja":
        args = text.split()[1:]
        values = [_norm_hhmm(a) for a in args]
        if len(values) != 2 or None in values or values[0] > values[1]:
            send_telegram(client, settings, "Formato: /franja 18:30 21:00 (desde y hasta)")
            return
        prefs.start_min, prefs.start_max = values
        result.prefs_changed = True
        send_telegram(client, settings, *menu_franja(prefs))
    elif _CLUB_LINK.search(text):
        try:
            club = resolve_club(client, text)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("No se pudo añadir club desde %r: %s", text, exc)
            send_telegram(client, settings, f"❌ No he podido añadir ese club: {md_escape(str(exc))}")
            return
        existing = prefs.club(club.tenant_id)
        if existing:
            existing.enabled = True
            msg = f"✅ *{md_escape(existing.name)}* ya estaba en la lista; queda activado."
        else:
            prefs.clubs.append(club)
            msg = f"✅ Añadido *{md_escape(club.name)}*. Lo vigilaré desde la próxima búsqueda."
        result.prefs_changed = True
        send_telegram(client, settings, msg, menu_clubs(prefs)[1])
    else:
        send_telegram(client, settings, HELP_TEXT)


def handle_callback(
    client: httpx.Client, settings: Settings, prefs: Prefs, cq: dict, result: BotResult
) -> None:
    data = cq.get("data") or ""
    notice = ""
    if data == "scan":
        result.scan_requested = True
        notice = "Buscando…"
        menu = "menu"
    elif data == "noop":
        menu = None
    elif data.startswith("delask:"):
        menu = None
        try:
            tg_call(client, settings, "answerCallbackQuery", callback_query_id=cq["id"])
        except (httpx.HTTPError, TelegramError):
            pass
        if cq.get("message"):
            edit_telegram(client, settings, cq["message"], *menu_confirm_delete(prefs, data.split(":", 1)[1]))
        return
    else:
        menu, changed, notice = apply_callback(prefs, data)
        result.prefs_changed |= changed
    try:
        tg_call(client, settings, "answerCallbackQuery", callback_query_id=cq["id"], text=notice)
    except (httpx.HTTPError, TelegramError):
        pass  # los callbacks caducan; si respondemos tarde (cron), no pasa nada
    if menu and cq.get("message"):
        edit_telegram(client, settings, cq["message"], *MENUS[menu](prefs))


def process_updates(
    client: httpx.Client, settings: Settings, prefs: Prefs, state: dict, poll_timeout: int = 0
) -> BotResult:
    """Lee mensajes y botones pendientes del bot y aplica los cambios a `prefs`.

    Solo se atiende al chat configurado (TELEGRAM_CHAT_ID); el resto se ignora.
    """
    result = BotResult()
    try:
        updates = tg_call(
            client,
            settings,
            "getUpdates",
            http_timeout=poll_timeout + 10,
            offset=state.get("tg_offset", 0),
            timeout=poll_timeout,
            allowed_updates=["message", "callback_query"],
        )
    except (httpx.HTTPError, TelegramError) as exc:
        log.warning("No se pudieron leer los mensajes del bot: %s", exc)
        return result

    for upd in updates:
        state["tg_offset"] = upd["update_id"] + 1
        try:
            if "callback_query" in upd:
                cq = upd["callback_query"]
                chat = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
                if chat == settings.chat_id:
                    handle_callback(client, settings, prefs, cq, result)
            elif "message" in upd:
                msg = upd["message"]
                if str(msg.get("chat", {}).get("id")) == settings.chat_id and msg.get("text"):
                    handle_message(client, settings, prefs, msg["text"], result)
        except (httpx.HTTPError, TelegramError) as exc:
            log.error("Error atendiendo al bot: %s", exc)
    return result


# --------------------------------------------------------------------------- #
# Escaneo y avisos
# --------------------------------------------------------------------------- #
@dataclass
class ScanResult:
    current: dict[str, Slot]
    failed: set[str]  # prefijos "fecha|tenant_id" que no se pudieron consultar
    queried: int
    blocked: bool = False


def scan(client: httpx.Client, settings: Settings, prefs: Prefs, state: dict, now: datetime) -> ScanResult:
    cache: dict[str, dict[str, str]] = state.setdefault("court_names", {})
    result = ScanResult(current={}, failed=set(), queried=0)
    days = target_days(now.date(), prefs)
    plan = [(club, d) for club in prefs.active_clubs for d in days]
    result.queried = len(plan)
    names_refreshed: set[str] = set()
    for i, (club, d) in enumerate(plan):
        names = cache.setdefault(club.tenant_id, {})
        if i and settings.request_delay:
            time.sleep(settings.request_delay)
        try:
            raw = fetch_availability(client, club, d)
        except Blocked as exc:
            # Insistir solo empeora el bloqueo: se para aquí y se reintenta más tarde.
            log.warning("%s. Se para la búsqueda y se reintentará más tarde.", exc)
            result.blocked = True
            result.failed |= {f"{dd.isoformat()}|{cc.tenant_id}" for cc, dd in plan[i:]}
            break
        except (httpx.HTTPError, ValueError) as exc:
            log.error("%s %s: fallo consultando: %s", club.name, day_label(d), exc)
            result.failed.add(f"{d.isoformat()}|{club.tenant_id}")
            continue
        # Nombres de pistas: solo se baja la web del club si aparece una pista desconocida.
        if club.tenant_id not in names_refreshed and any(r.get("resource_id") not in names for r in raw):
            names.update(fetch_court_names(client, club))
            names_refreshed.add(club.tenant_id)
        slots = filter_slots(raw, club, d, prefs, names, now, settings.api_tz)
        for s in slots:
            result.current[s.key] = s
        log.info("%s %s: %d turnos en franja", club.name, day_label(d), len(slots))
    return result


def notify(client: httpx.Client, settings: Settings, messages: list[str], dry_run: bool) -> bool:
    ok = True
    for msg in messages:
        if dry_run:
            print(msg, "\n" + "-" * 40)
            continue
        try:
            send_telegram(client, settings, msg)
        except (httpx.HTTPError, TelegramError) as exc:
            log.error("Error enviando a Telegram: %s", exc)
            ok = False
    return ok


def run_cycle(
    client: httpx.Client,
    settings: Settings,
    prefs: Prefs,
    state: dict,
    now: datetime,
    dry_run: bool = False,
    full_listing: bool = False,
) -> int:
    """Escanea y avisa. Devuelve 1 si no se pudo consultar nada (para que el cron lo marque).

    Anti-spam: el estado guarda los turnos libres vistos en la pasada anterior. Solo
    se avisa de los nuevos; si una pista se reserva y luego se libera de nuevo,
    vuelve a avisarse (es una oportunidad nueva). Con full_listing (/buscar) se
    envían todos los turnos libres actuales.
    """
    if prefs.paused and not full_listing:
        log.info("En pausa: no se escanea")
        return 0
    if not prefs.active_clubs:
        log.info("No hay clubes activos")
        if full_listing:
            notify(client, settings, ["🏟 No hay clubes activos. Actívalos en /menu → Clubes."], dry_run)
        return 0

    today = now.date().isoformat()
    previous: set[str] = set(state.get("available", []))
    res = scan(client, settings, prefs, state, now)

    if full_listing:
        slots = list(res.current.values())
        title = "📋 *Pistas libres ahora*"
        if not slots:
            messages = ["📋 Ahora mismo no hay pistas libres con tu configuración."]
        else:
            messages = format_messages(slots, prefs, title=title)
        new_keys = set(res.current)
    else:
        new_slots = [s for k, s in res.current.items() if k not in previous]
        messages = format_messages(new_slots, prefs) if new_slots else []
        new_keys = {s.key for s in new_slots}
        log.info("%d turnos nuevos", len(new_slots))
    if full_listing and res.blocked:
        messages.append("⚠️ Playtomic ha frenado las consultas; la lista puede estar incompleta. "
                        "Reintentaré en unos minutos.")
    sent_ok = notify(client, settings, messages, dry_run)

    # Si Telegram falla, no marcamos los nuevos como vistos (se reintenta la próxima vez).
    seen = set(res.current) if sent_ok else (set(res.current) - new_keys) | (previous & new_keys)
    # Lo que no se pudo consultar: conservamos lo anterior para no re-avisar.
    seen |= {k for k in previous if k.split("|", 2)[0] + "|" + k.split("|", 2)[1] in res.failed}
    seen = {k for k in seen if k.split("|", 1)[0] >= today}  # purga de días pasados
    state["available"] = sorted(seen)
    state["updated_at"] = now.isoformat()
    state["blocked"] = res.blocked
    return 1 if res.queried and len(res.failed) == res.queried else 0


# --------------------------------------------------------------------------- #
# Modos de ejecución
# --------------------------------------------------------------------------- #
def run_once(settings: Settings, client: httpx.Client, dry_run: bool = False, now: datetime | None = None) -> int:
    """Una pasada: atiende al bot (si hay mensajes pendientes), escanea y guarda."""
    now = now or datetime.now(TZ)
    prefs = load_prefs(settings.config_file)
    state = load_state(settings.state_file)
    bot = BotResult()
    if not dry_run:
        bot = process_updates(client, settings, prefs, state)
        if bot.prefs_changed:
            write_json(settings.config_file, prefs.to_dict())
    code = run_cycle(client, settings, prefs, state, now, dry_run, full_listing=bot.scan_requested)
    if not dry_run:
        write_json(settings.state_file, state)
    return code


def run_loop(settings: Settings, client: httpx.Client, dry_run: bool = False) -> None:
    """Daemon: escanea cada `interval` segundos y entre medias escucha al bot (long polling)."""
    log.info("Modo bucle cada %ss", settings.interval)
    state = load_state(settings.state_file)
    next_scan, full_listing = 0.0, False
    while True:
        try:
            prefs = load_prefs(settings.config_file)
            if time.monotonic() >= next_scan or full_listing:
                run_cycle(client, settings, prefs, state, datetime.now(TZ), dry_run, full_listing)
                delay = settings.interval
                if state.get("blocked"):
                    delay = max(settings.interval, BLOCKED_BACKOFF)
                    log.warning("Bloqueo de Playtomic: próxima búsqueda en %d s", delay)
                next_scan, full_listing = time.monotonic() + delay, False
            if dry_run:
                time.sleep(max(0.0, next_scan - time.monotonic()))
                continue
            wait = int(max(0, min(25, next_scan - time.monotonic())))
            bot = process_updates(client, settings, prefs, state, poll_timeout=wait)
            if bot.prefs_changed:
                write_json(settings.config_file, prefs.to_dict())
            full_listing = bot.scan_requested
            write_json(settings.state_file, state)
        except Exception:  # el daemon no debe morir por un fallo puntual
            log.exception("Error inesperado en el bucle")
            time.sleep(5)


def add_club_cli(settings: Settings, client: httpx.Client, url: str, name: str | None = None) -> int:
    prefs = load_prefs(settings.config_file)
    try:
        club = resolve_club(client, url)
    except (httpx.HTTPError, ValueError) as exc:
        log.error("No se pudo añadir el club: %s", exc)
        return 1
    if name:
        club.name = name
    existing = prefs.club(club.tenant_id)
    if existing:
        existing.enabled = True
        if name:
            existing.name = name
        print(f"Ya estaba: {existing.name} (activado)")
    else:
        prefs.clubs.append(club)
        print(f"Añadido: {club.name} · {club.tenant_id} · {club.page_url}")
    write_json(settings.config_file, prefs.to_dict())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="una sola pasada (por defecto)")
    mode.add_argument("--loop", action="store_true", help="bucle infinito")
    mode.add_argument("--add-club", metavar="URL", help="añade un club a partir de su enlace de Playtomic")
    parser.add_argument("--name", help="con --add-club: nombre con el que aparecerá el club")
    parser.add_argument("--interval", type=int, help="segundos entre escaneos en --loop")
    parser.add_argument("--dry-run", action="store_true", help="imprime en vez de enviar y no guarda estado")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # evita loguear la URL con el token

    load_dotenv(Path(__file__).with_name(".env"))
    settings = Settings.from_env()
    if args.interval:
        settings = Settings(**{**settings.__dict__, "interval": args.interval})
    if not args.dry_run and not args.add_club and not (settings.bot_token and settings.chat_id):
        log.error("Faltan TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID")
        return 2

    with httpx.Client(headers=HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        if args.add_club:
            return add_club_cli(settings, client, args.add_club, args.name)
        if args.loop:
            run_loop(settings, client, dry_run=args.dry_run)
            return 0
        return run_once(settings, client, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
