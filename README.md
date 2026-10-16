# agente-padel

Monitor de pistas de pádel libres en clubes de **Playtomic**, con avisos y un menú de botones en **Telegram**.

Por defecto vigila **Coslada Dreamfit**, de **lunes a viernes**, en los **próximos 12 días**, las pistas que **empiezan entre las 18:30 y las 21:00** y duran **90 minutos**. Todo eso se puede cambiar desde el bot.

## El bot de Telegram

| Qué | Cómo |
|---|---|
| Ver y cambiar la configuración | `/menu` → botones 🏟 Clubes · 📅 Días · 🕡 Franja · ⏱ Duración · ⏸ Pausar · 🔎 Buscar ahora |
| Añadir un club | En la app de Playtomic: entra en el club → **Compartir** → pega el enlace en el chat del bot |
| Activar, desactivar o borrar clubes | `/menu` → 🏟 Clubes |
| Cambiar la franja | Botones ◀ ▶ o `/franja 18:00 21:30` |
| Ver todo lo libre ahora | `/buscar` |
| Parar o reactivar los avisos | `/pausa` · `/reanudar` |

- **Quién puede usarlo:** el bot solo obedece al chat de `TELEGRAM_CHAT_ID`.
- **Dónde se guarda la configuración:** lo que cambias desde el bot va a `config.local.json`, que git ignora. La primera vez se parte de `config.json`, la configuración del repo. Así `git pull` nunca choca con tus cambios.
- **Cuánto tarda en responder:** en modo `--loop` (Mac, Docker) responde al instante.
- ⚠️ **Una sola copia a la vez.** No tengas el bot funcionando a la vez en dos sitios (por ejemplo, el Mac y una Raspberry Pi): los dos competirían por los mensajes de Telegram.

## Cómo funciona por dentro

- **Horas:** la API devuelve `start_time` en **UTC**. Se convierte a hora de Madrid con `zoneinfo`, que respeta el cambio de hora, y nunca sumando un número fijo de horas. Comprobado con datos reales: el turno de las 21:30 llega como 19:30 en horario de verano y como 20:30 en horario de invierno.
- **Nombres de las pistas:** "Padel 7" y similares se sacan cruzando el `resource_id` con la lista de pistas que trae la web de cada club, nunca por la posición en el array.
- **Enlace "Reservar":** usa el formato del botón *Compartir* de la app (`playtomic.com/tenant/<id>`). En el móvil abre la app de Playtomic en la ficha del club.
- **Anti-spam:** `state.json` guarda los turnos libres de la pasada anterior y solo se avisa de los nuevos. Si una pista se reserva y luego se vuelve a liberar, se avisa otra vez.

## Probar en local (macOS)

```bash
git clone https://github.com/alejandropascualdelcid/agente-padel.git
cd agente-padel
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env        # rellena TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID
pytest -q                   # tests con la API simulada

python monitor_padel.py --once --dry-run   # consulta real, imprime el mensaje sin enviarlo
python monitor_padel.py --once             # atiende al bot y envía avisos a Telegram
python monitor_padel.py --loop             # bot al instante + escaneo cada 60 s
python monitor_padel.py --add-club "https://playtomic.com/tenant/<id>"
```

## GitHub Actions: descartado

Lo probamos y no sirve: Cloudflare responde **403** a los servidores de GitHub desde la primera consulta, y además el cron de GitHub solo se lanzaba cada ~5 horas. El workflow queda solo con lanzamiento manual, por si algún día Playtomic deja de bloquear. Lo mismo pasará casi seguro con cualquier servidor en la nube: el agente tiene que funcionar **desde una conexión de casa** (tu Mac, una Raspberry Pi o un portátil viejo).

## Docker (Raspberry Pi, NAS, VPS…)

```bash
docker build -t agente-padel .
docker run -d --name padel --restart unless-stopped --env-file .env -v padel-data:/data agente-padel
docker logs -f padel
```
