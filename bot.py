import os
import re
import unicodedata
import requests
import psycopg

from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ============================================================
# CONFIGURACIÓN
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

ODDS_BASE_URL = "https://api.the-odds-api.com/v4"


# ============================================================
# VALIDACIÓN
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError("Falta BOT_TOKEN")

if not DATABASE_URL:
    raise RuntimeError("Falta DATABASE_URL")

if not ODDS_API_KEY:
    raise RuntimeError("Falta ODDS_API_KEY")


# ============================================================
# BASE DE DATOS
# ============================================================

def get_db():
    return psycopg.connect(DATABASE_URL)


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    balance NUMERIC DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS pending_bets (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    sport TEXT,
                    competition TEXT,
                    event_id TEXT,
                    home_team TEXT,
                    away_team TEXT,
                    selection TEXT,
                    odds NUMERIC,
                    stake NUMERIC,
                    potential_return NUMERIC,
                    expires_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bets (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    sport TEXT,
                    competition TEXT,
                    event_id TEXT,
                    home_team TEXT,
                    away_team TEXT,
                    selection TEXT,
                    odds NUMERIC,
                    stake NUMERIC,
                    potential_return NUMERIC,
                    result TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW(),
                    settled_at TIMESTAMP
                )
            """)

            conn.commit()

            # ------------------------------------------------
            # MIGRACIONES SEGURAS
            # ------------------------------------------------

            migrations = [
                "ALTER TABLE pending_bets ADD COLUMN IF NOT EXISTS sport TEXT",
                "ALTER TABLE pending_bets ADD COLUMN IF NOT EXISTS competition TEXT",
                "ALTER TABLE pending_bets ADD COLUMN IF NOT EXISTS event_id TEXT",
                "ALTER TABLE pending_bets ADD COLUMN IF NOT EXISTS home_team TEXT",
                "ALTER TABLE pending_bets ADD COLUMN IF NOT EXISTS away_team TEXT",
                "ALTER TABLE pending_bets ADD COLUMN IF NOT EXISTS selection TEXT",
                "ALTER TABLE pending_bets ADD COLUMN IF NOT EXISTS odds NUMERIC",
                "ALTER TABLE pending_bets ADD COLUMN IF NOT EXISTS stake NUMERIC",
                "ALTER TABLE pending_bets ADD COLUMN IF NOT EXISTS potential_return NUMERIC",
                "ALTER TABLE pending_bets ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP",
                "ALTER TABLE pending_bets ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()",
            ]

            for sql in migrations:
                try:
                    cur.execute(sql)
                except Exception:
                    conn.rollback()

            conn.commit()


# ============================================================
# USUARIOS
# ============================================================

def get_or_create_user(user_id, username=None):

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT user_id, username, balance FROM users WHERE user_id = %s",
                (user_id,)
            )

            row = cur.fetchone()

            if row:
                if username and row[1] != username:
                    cur.execute(
                        "UPDATE users SET username = %s WHERE user_id = %s",
                        (username, user_id)
                    )
                    conn.commit()

                return row

            cur.execute(
                """
                INSERT INTO users (user_id, username, balance)
                VALUES (%s, %s, 0)
                RETURNING user_id, username, balance
                """,
                (user_id, username)
            )

            row = cur.fetchone()
            conn.commit()

            return row


# ============================================================
# ODDS API
# ============================================================

def get_odds(sport_key):

    url = f"{ODDS_BASE_URL}/sports/{sport_key}/odds"

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
    }

    response = requests.get(url, params=params, timeout=30)

    response.raise_for_status()

    return response.json()


def get_event_odds(sport_key, event_id):

    try:
        events = get_odds(sport_key)

        for event in events:

            if str(event.get("id")) == str(event_id):
                return event

    except Exception as e:
        print("GET EVENT ODDS ERROR:", e)

    return None


# ============================================================
# NORMALIZACIÓN DE EQUIPOS
# ============================================================

def normalize_team_name(name):

    if not name:
        return ""

    name = str(name)

    # Eliminar acentos
    name = unicodedata.normalize("NFKD", name)

    name = "".join(
        c for c in name
        if not unicodedata.combining(c)
    )

    name = name.lower()

    # Reemplazos frecuentes
    replacements = {
        "football club": " ",
        "fc": " ",
        " cf ": " ",
        " cf": " ",
        "sc": " ",
        "afc": " ",
    }

    for old, new in replacements.items():
        name = name.replace(old, new)

    # Caracteres no alfanuméricos
    name = re.sub(r"[^a-z0-9]+", " ", name)

    # Espacios
    name = " ".join(name.split())

    return name.strip()


def teams_match(
    expected_home,
    expected_away,
    api_home,
    api_away
):

    eh = normalize_team_name(expected_home)
    ea = normalize_team_name(expected_away)

    ah = normalize_team_name(api_home)
    aa = normalize_team_name(api_away)

    if not eh or not ea or not ah or not aa:
        return False

    # Coincidencia exacta
    if eh == ah and ea == aa:
        return True

    # Coincidencia por inclusión segura
    home_match = (
        eh == ah
        or eh in ah
        or ah in eh
    )

    away_match = (
        ea == aa
        or ea in aa
        or aa in ea
    )

    if home_match and away_match:
        return True

    return False


# ============================================================
# OBTENER RESULTADOS RECIENTES
# ============================================================

def get_recent_completed_events(sport_key):

    url = f"{ODDS_BASE_URL}/sports/{sport_key}/scores"

    params = {
        "apiKey": ODDS_API_KEY,
        "daysFrom": 3,
    }

    print("=" * 40)
    print("RECENT SCORES REQUEST")
    print("SPORT:", sport_key)
    print("PARAMS: daysFrom=3")

    try:

        response = requests.get(
            url,
            params=params,
            timeout=30
        )

        print("RECENT SCORES STATUS:", response.status_code)

        if response.status_code != 200:
            print(
                "RECENT SCORES RESPONSE:",
                response.text[:2000]
            )

            return []

        data = response.json()

        print(
            "RECENT SCORES EVENTS:",
            len(data)
        )

        return data

    except Exception as e:

        print(
            "RECENT SCORES ERROR:",
            repr(e)
        )

        return []


# ============================================================
# BUSCAR EVENTO POR EQUIPOS
# ============================================================

def find_event_by_teams(
    sport_key,
    home_team,
    away_team
):

    print("=" * 40)
    print("TEAM FALLBACK SEARCH")
    print("SPORT:", sport_key)
    print("EXPECTED HOME:", home_team)
    print("EXPECTED AWAY:", away_team)

    events = get_recent_completed_events(
        sport_key
    )

    print(
        "TEAM FALLBACK EVENTS RECEIVED:",
        len(events)
    )

    for event in events:

        api_home = event.get("home_team")
        api_away = event.get("away_team")

        print(
            "CHECK EVENT:",
            api_home,
            "vs",
            api_away
        )

        if teams_match(
            home_team,
            away_team,
            api_home,
            api_away
        ):

            print(
                "TEAM FALLBACK MATCH FOUND:",
                api_home,
                "vs",
                api_away,
                "ID:",
                event.get("id")
            )

            return event

    print(
        "SCORES FALLBACK: PARTIDO NO ENCONTRADO POR EQUIPOS."
    )

    return None


# ============================================================
# RESULTADO DE EVENTO
# ============================================================

def get_event_result(
    sport_key,
    event_id,
    home_team=None,
    away_team=None
):

    url = f"{ODDS_BASE_URL}/sports/{sport_key}/scores"

    params = {
        "apiKey": ODDS_API_KEY,
        "daysFrom": 3,
        "eventIds": event_id,
    }

    print("=" * 40)
    print("SCORES REQUEST")
    print("SPORT:", sport_key)
    print("EVENT ID:", event_id)
    print("HOME TEAM:", home_team)
    print("AWAY TEAM:", away_team)
    print(
        "PARAMS: daysFrom=3 eventIds=",
        event_id
    )

    try:

        response = requests.get(
            url,
            params=params,
            timeout=30
        )

        print(
            "SCORES STATUS:",
            response.status_code
        )

        print(
            "SCORES RESPONSE:",
            response.text[:4000]
        )

        if response.status_code != 200:

            print(
                "SCORES REQUEST ERROR:",
                response.text[:1000]
            )

        else:

            data = response.json()

            if data:

                for event in data:

                    if str(event.get("id")) == str(event_id):

                        print(
                            "SCORES EVENT FOUND:",
                            event.get("home_team"),
                            "vs",
                            event.get("away_team")
                        )

                        return event

            else:

                print(
                    "SCORES EVENT NOT FOUND IN RESPONSE:",
                    event_id
                )

    except Exception as e:

        print(
            "SCORES REQUEST EXCEPTION:",
            repr(e)
        )

    # --------------------------------------------------------
    # FALLBACK POR EQUIPOS
    # --------------------------------------------------------

    if home_team and away_team:

        print(
            "SCORES FALLBACK: buscando por equipos..."
        )

        event = find_event_by_teams(
            sport_key,
            home_team,
            away_team
        )

        if event:

            return event

    else:

        print(
            "SCORES FALLBACK OMITIDO: faltan equipos."
        )

    return None


# ============================================================
# SPORT KEY DE APUESTA
# ============================================================

def get_pending_sport_key(row):

    # pending_bets:
    #
    # 0 id
    # 1 user_id
    # 2 sport
    # 3 competition
    # 4 event_id
    # 5 home_team
    # 6 away_team
    # 7 selection
    # 8 odds
    # 9 stake
    # 10 potential_return
    # 11 expires_at
    # 12 created_at

    competition = row[3]
    sport = row[2]

    if competition:
        return str(competition).strip()

    if sport:
        return str(sport).strip()

    return None


# ============================================================
# OBTENER MARCADOR
# ============================================================

def extract_scores(event):

    scores = event.get("scores")

    if not scores:
        return None, None

    home_score = None
    away_score = None

    home_team = event.get("home_team")
    away_team = event.get("away_team")

    for item in scores:

        name = item.get("name")
        score = item.get("score")

        if score is None:
            continue

        try:
            score = int(score)
        except Exception:
            continue

        if name == home_team:
            home_score = score

        elif name == away_team:
            away_score = score

    if home_score is None or away_score is None:
        return None, None

    return home_score, away_score


# ============================================================
# DETERMINAR RESULTADO
# ============================================================

def determine_bet_result(
    selection,
    home_team,
    away_team,
    home_score,
    away_score
):

    selection_normalized = normalize_team_name(
        selection
    )

    home_normalized = normalize_team_name(
        home_team
    )

    away_normalized = normalize_team_name(
        away_team
    )

    # EMPATE
    if (
        selection_normalized in
        ["draw", "empate", "tie", "x"]
    ):

        if home_score == away_score:
            return "win"

        return "lose"

    # HOME
    if (
        selection_normalized == home_normalized
        or selection_normalized in home_normalized
        or home_normalized in selection_normalized
    ):

        if home_score > away_score:
            return "win"

        return "lose"

    # AWAY
    if (
        selection_normalized == away_normalized
        or selection_normalized in away_normalized
        or away_normalized in selection_normalized
    ):

        if away_score > home_score:
            return "win"

        return "lose"

    return "lose"


# ============================================================
# LIQUIDAR APUESTAS
# ============================================================

def settle_pending_rows(
    rows,
    event
):

    if not rows:
        return 0

    home_team = event.get("home_team")
    away_team = event.get("away_team")

    home_score, away_score = extract_scores(
        event
    )

    if home_score is None or away_score is None:

        raise Exception(
            "El evento no contiene un marcador válido."
        )

    settled_count = 0

    with get_db() as conn:

        with conn.cursor() as cur:

            for row in rows:

                bet_id = row[0]
                user_id = row[1]
                selection = row[7]
                odds = row[8]
                stake = row[9]
                potential_return = row[10]

                result = determine_bet_result(
                    selection,
                    home_team,
                    away_team,
                    home_score,
                    away_score
                )

                if result == "win":

                    cur.execute(
                        """
                        UPDATE users
                        SET balance = balance + %s
                        WHERE user_id = %s
                        """,
                        (
                            potential_return,
                            user_id
                        )
                    )

                elif result == "lose":

                    pass

                cur.execute(
                    """
                    INSERT INTO bets (
                        user_id,
                        sport,
                        competition,
                        event_id,
                        home_team,
                        away_team,
                        selection,
                        odds,
                        stake,
                        potential_return,
                        result,
                        status,
                        created_at,
                        settled_at
                    )
                    SELECT
                        user_id,
                        sport,
                        competition,
                        event_id,
                        home_team,
                        away_team,
                        selection,
                        odds,
                        stake,
                        potential_return,
                        %s,
                        'settled',
                        created_at,
                        NOW()
                    FROM pending_bets
                    WHERE id = %s
                    """,
                    (
                        result,
                        bet_id
                    )
                )

                cur.execute(
                    """
                    DELETE FROM pending_bets
                    WHERE id = %s
                    """,
                    (bet_id,)
                )

                settled_count += 1

            conn.commit()

    return settled_count


# ============================================================
# LIQUIDAR UN EVENTO
# ============================================================

def settle_event(
    sport_key,
    event_id
):

    with get_db() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    user_id,
                    sport,
                    competition,
                    event_id,
                    home_team,
                    away_team,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    expires_at,
                    created_at
                FROM pending_bets
                WHERE event_id = %s
                """,
                (event_id,)
            )

            rows = cur.fetchall()

    if not rows:
        return None

    home_team = rows[0][5]
    away_team = rows[0][6]

    event = get_event_result(
        sport_key,
        event_id,
        home_team,
        away_team
    )

    if not event:

        raise Exception(
            "No se encontró el resultado del partido."
        )

    settled = settle_pending_rows(
        rows,
        event
    )

    return {
        "event": event,
        "settled": settled,
    }


# ============================================================
# LIQUIDAR APUESTA INDIVIDUAL
# ============================================================

def settle_bet(
    bet_id
):

    with get_db() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    user_id,
                    sport,
                    competition,
                    event_id,
                    home_team,
                    away_team,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    expires_at,
                    created_at
                FROM pending_bets
                WHERE id = %s
                """,
                (bet_id,)
            )

            row = cur.fetchone()

    if not row:

        raise Exception(
            f"No existe la apuesta pendiente #{bet_id}."
        )

    sport_key = get_pending_sport_key(row)

    event_id = row[4]
    home_team = row[5]
    away_team = row[6]

    print("=" * 40)
    print("SETTLE BET")
    print("BET ID:", bet_id)
    print("SPORT KEY:", sport_key)
    print("EVENT ID:", event_id)
    print("HOME:", home_team)
    print("AWAY:", away_team)
    print("=" * 40)

    if not sport_key:
        raise Exception(
            "La apuesta no tiene sport_key."
        )

    event = get_event_result(
        sport_key,
        event_id,
        home_team,
        away_team
    )

    if not event:

        print(
            "SCORES EVENT NOT FOUND:",
            event_id
        )

        raise Exception(
            "No se encontró el resultado del partido."
        )

    settled = settle_pending_rows(
        [row],
        event
    )

    if settled != 1:

        raise Exception(
            "No se pudo liquidar la apuesta."
        )

    home_score, away_score = extract_scores(
        event
    )

    result = determine_bet_result(
        row[7],
        event.get("home_team"),
        event.get("away_team"),
        home_score,
        away_score
    )

    return {
        "bet_id": bet_id,
        "home_team": event.get("home_team"),
        "away_team": event.get("away_team"),
        "home_score": home_score,
        "away_score": away_score,
        "selection": row[7],
        "odds": row[8],
        "stake": row[9],
        "potential_return": row[10],
        "result": result,
    }


# ============================================================
# FORMATO DE LIQUIDACIÓN
# ============================================================

def format_settlement(result):

    if result["result"] == "win":

        result_text = "✅ GANADORA"

    else:

        result_text = "❌ PERDEDORA"

    return (
        "✅ APUESTA LIQUIDADA\n\n"
        f"⚽ {result['home_team']}\n"
        "vs\n"
        f"⚽ {result['away_team']}\n\n"
        "📊 Marcador:\n"
        f"• {result['home_team']}: "
        f"{result['home_score']}\n"
        f"• {result['away_team']}: "
        f"{result['away_score']}\n\n"
        f"🎯 Selección: {result['selection']}\n"
        f"💰 Cuota: {result['odds']}\n"
        f"💵 Apuesta: {result['stake']}\n\n"
        f"🏆 Resultado: {result_text}"
    )


# ============================================================
# /LIQUIDAR
# ============================================================

async def liquidar_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:

        await update.message.reply_text(
            "❌ No tienes permiso para utilizar esta herramienta."
        )

        return

    if not context.args:

        await update.message.reply_text(
            "Uso:\n/liquidar <id>"
        )

        return

    try:

        bet_id = int(context.args[0])

    except ValueError:

        await update.message.reply_text(
            "❌ El ID de la apuesta debe ser numérico."
        )

        return

    await update.message.reply_text(
        f"⏳ Liquidando apuesta pendiente #{bet_id}..."
    )

    try:

        result = settle_bet(
            bet_id
        )

        await update.message.reply_text(
            format_settlement(result)
        )

    except Exception as e:

        print(
            "LIQUIDAR ERROR:",
            repr(e)
        )

        await update.message.reply_text(
            "❌ Error liquidando la apuesta:\n\n"
            f"{str(e)}"
        )


# ============================================================
# /PENDIENTES
# ============================================================

async def pendientes_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:

        await update.message.reply_text(
            "❌ No tienes permiso."
        )

        return

    with get_db() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    home_team,
                    away_team,
                    selection,
                    odds,
                    stake,
                    created_at
                FROM pending_bets
                ORDER BY id
                """
            )

            rows = cur.fetchall()

    if not rows:

        await update.message.reply_text(
            "📭 No hay apuestas pendientes."
        )

        return

    text = "📋 APUESTAS PENDIENTES\n\n"

    for row in rows:

        text += (
            f"#{row[0]}\n"
            f"⚽ {row[1]} vs {row[2]}\n"
            f"🎯 {row[3]}\n"
            f"📈 Cuota: {row[4]}\n"
            f"💰 Apuesta: {row[5]}\n\n"
        )

    await update.message.reply_text(
        text
    )


# ============================================================
# /RESULTADO
# ============================================================

async def resultado_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:

        await update.message.reply_text(
            "❌ No tienes permiso."
        )

        return

    if len(context.args) < 2:

        await update.message.reply_text(
            "Uso:\n"
            "/resultado <sport_key> <event_id>"
        )

        return

    sport_key = context.args[0]
    event_id = context.args[1]

    event = get_event_result(
        sport_key,
        event_id
    )

    if not event:

        await update.message.reply_text(
            "❌ Evento no encontrado."
        )

        return

    home_score, away_score = extract_scores(
        event
    )

    await update.message.reply_text(
        f"⚽ {event.get('home_team')}\n"
        f"vs\n"
        f"⚽ {event.get('away_team')}\n\n"
        f"📊 {home_score} - {away_score}\n\n"
        f"ID: {event.get('id')}"
    )


# ============================================================
# /RESULTADOS
# ============================================================

async def resultados_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:

        await update.message.reply_text(
            "❌ No tienes permiso."
        )

        return

    if not context.args:

        await update.message.reply_text(
            "Uso:\n/resultados <sport_key>"
        )

        return

    sport_key = context.args[0]

    events = get_recent_completed_events(
        sport_key
    )

    if not events:

        await update.message.reply_text(
            "❌ No se encontraron resultados."
        )

        return

    text = (
        f"📊 RESULTADOS RECIENTES\n"
        f"{sport_key}\n\n"
    )

    count = 0

    for event in events:

        if count >= 30:
            break

        home_score, away_score = extract_scores(
            event
        )

        if home_score is None:
            continue

        text += (
            f"⚽ {event.get('home_team')} "
            f"vs "
            f"{event.get('away_team')}\n"
            f"📊 {home_score} - {away_score}\n"
            f"ID: {event.get('id')}\n\n"
        )

        count += 1

    await update.message.reply_text(
        text[:4000]
    )


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    get_or_create_user(
        user.id,
        user.username
    )

    await update.message.reply_text(
        "🏆 Cuba Sports\n\n"
        "Bienvenido."
    )


# ============================================================
# HANDLER DE MENSAJES
# ============================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    return


# ============================================================
# CALLBACK
# ============================================================

async def callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    await query.answer()


# ============================================================
# MAIN
# ============================================================

def main():

    init_db()

    print("🏆 Cuba Sports iniciado correctamente.")
    print("=" * 40)

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    application.add_handler(
        CommandHandler(
            "liquidar",
            liquidar_command
        )
    )

    application.add_handler(
        CommandHandler(
            "pendientes",
            pendientes_command
        )
    )

    application.add_handler(
        CommandHandler(
            "resultado",
            resultado_command
        )
    )

    application.add_handler(
        CommandHandler(
            "resultados",
            resultados_command
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler
        )
    )

    application.run_polling()


if __name__ == "__main__":
    main()
