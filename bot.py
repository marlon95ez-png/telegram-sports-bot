import os
import requests
import psycopg
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
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
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")

bets = {}
pending_bets = {}


# ============================================================
# ADMIN
# ============================================================

def is_admin(user_id):
    if not ADMIN_TELEGRAM_ID:
        return False

    return str(user_id) == str(ADMIN_TELEGRAM_ID)


# ============================================================
# MANEJO DE ERRORES DE TELEGRAM
# ============================================================

async def telegram_error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Evita que el bot se detenga cuando Telegram devuelve:

        BadRequest: Message is not modified

    Esto ocurre cuando intentamos editar un mensaje con exactamente
    el mismo texto y/o teclado que ya tiene.

    El error es ignorado porque no afecta la lógica del bot.
    Los demás errores se muestran en los logs.
    """

    error = context.error

    if isinstance(error, BadRequest):

        error_text = str(error).lower()

        if "message is not modified" in error_text:

            print(
                "ℹ️ Telegram: el mensaje ya estaba actualizado. "
                "Se ignora Message is not modified."
            )

            return

    print(
        "❌ ERROR NO CONTROLADO DE TELEGRAM:",
        repr(error),
    )


# ============================================================
# BASE DE DATOS
# ============================================================

def get_db_connection():
    return psycopg.connect(DATABASE_URL)


def split_event_name(event_name):
    if not event_name:
        return "Local", "Visitante"

    if " vs " in event_name:
        return event_name.split(" vs ", 1)

    return event_name, ""


def parse_event_datetime(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)

        return parsed

    except Exception:
        return None


def migrate_legacy_pending_bets():
    """
    Migra las apuestas que pertenecían al sistema anterior y que
    todavía están guardadas en bets con estado Pendiente.

    Esto evita perder apuestas pendientes después del cambio
    hacia la nueva arquitectura:
        pending_bets -> apuestas pendientes
        bets         -> historial liquidado
    """

    migrated = 0
    removed = 0

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    user_id,
                    sport,
                    competition,
                    event_id,
                    event_name,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    created_at
                FROM bets
                WHERE status = 'Pendiente'
                ORDER BY id ASC
                """
            )

            rows = cur.fetchall()

            for row in rows:

                (
                    old_bet_id,
                    user_id,
                    sport,
                    competition,
                    event_id,
                    event_name,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    created_at,
                ) = row

                home_team, away_team = split_event_name(
                    event_name
                )

                cur.execute(
                    """
                    SELECT id
                    FROM pending_bets
                    WHERE user_id = %s
                      AND event_id = %s
                      AND selection = %s
                      AND stake = %s
                      AND created_at = %s
                    LIMIT 1
                    """,
                    (
                        user_id,
                        event_id,
                        selection,
                        stake,
                        created_at,
                    ),
                )

                existing = cur.fetchone()

                if not existing:

                    cur.execute(
                        """
                        INSERT INTO pending_bets (
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
                        )
                        VALUES (
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            NULL,
                            %s
                        )
                        """,
                        (
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
                            created_at,
                        ),
                    )

                    migrated += 1

                cur.execute(
                    """
                    DELETE FROM bets
                    WHERE id = %s
                    """,
                    (old_bet_id,),
                )

                removed += 1

    if migrated or removed:
        print(
            "MIGRACION APUESTAS PENDIENTES: "
            f"migradas={migrated}, eliminadas_de_bets={removed}"
        )


def init_db():

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    balance NUMERIC DEFAULT 1000,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bets (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    sport TEXT,
                    competition TEXT,
                    event_id TEXT,
                    event_name TEXT,
                    match_date TIMESTAMP,
                    selection TEXT,
                    odds NUMERIC,
                    stake NUMERIC,
                    potential_return NUMERIC,
                    status TEXT DEFAULT 'Pendiente',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    type TEXT,
                    amount NUMERIC,
                    balance_before NUMERIC,
                    balance_after NUMERIC,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_bets (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS unconfirmed_bets (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    event_id TEXT,
                    event_name TEXT,
                    sport TEXT,
                    competition TEXT,
                    selection TEXT,
                    odds NUMERIC,
                    stake NUMERIC,
                    potential_return NUMERIC,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            cur.execute(
                """
                ALTER TABLE unconfirmed_bets
                ADD COLUMN IF NOT EXISTS home_team TEXT
                """
            )

            cur.execute(
                """
                ALTER TABLE unconfirmed_bets
                ADD COLUMN IF NOT EXISTS away_team TEXT
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_pending_bets_event_id
                ON pending_bets(event_id)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_pending_bets_user_id
                ON pending_bets(user_id)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_bets_event_id
                ON bets(event_id)
                """
            )

    migrate_legacy_pending_bets()


# ============================================================
# USUARIOS
# ============================================================

def get_or_create_user(telegram_user):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id, telegram_id, username, balance
                FROM users
                WHERE telegram_id = %s
                """,
                (telegram_user.id,),
            )

            user = cur.fetchone()

            if user:

                cur.execute(
                    """
                    UPDATE users
                    SET username = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE telegram_id = %s
                    """,
                    (
                        telegram_user.username,
                        telegram_user.id,
                    ),
                )

                return user

            cur.execute(
                """
                INSERT INTO users (
                    telegram_id,
                    username,
                    balance
                )
                VALUES (%s, %s, 1000)
                RETURNING id, telegram_id, username, balance
                """,
                (
                    telegram_user.id,
                    telegram_user.username,
                ),
            )

            return cur.fetchone()


def get_user_balance(telegram_id):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT balance
                FROM users
                WHERE telegram_id = %s
                """,
                (telegram_id,),
            )

            row = cur.fetchone()

            if not row:
                return 0

            return row[0]


# ============================================================
# ODDS API
# ============================================================

def get_sports():

    url = "https://api.the-odds-api.com/v4/sports/"

    response = requests.get(
        url,
        params={
            "apiKey": ODDS_API_KEY,
        },
        timeout=20,
    )

    response.raise_for_status()

    return response.json()


def get_odds(sport_key):

    url = (
        f"https://api.the-odds-api.com/v4/"
        f"sports/{sport_key}/odds/"
    )

    response = requests.get(
        url,
        params={
            "apiKey": ODDS_API_KEY,
            "regions": "eu",
            "markets": "h2h",
            "oddsFormat": "decimal",
        },
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


def get_event_odds(sport_key, event_id):

    url = (
        f"https://api.the-odds-api.com/v4/sports/"
        f"{sport_key}/events/{event_id}/odds"
    )

    response = requests.get(
        url,
        params={
            "apiKey": ODDS_API_KEY,
            "regions": "eu",
            "markets": "h2h",
            "oddsFormat": "decimal",
        },
        timeout=30,
    )

    response.raise_for_status()

    print(
        "ODDS EVENT STATUS:",
        response.status_code,
    )

    print(
        "ODDS EVENT RESPONSE:",
        response.text[:3000],
    )

    return response.json()


# ============================================================
# RESULTADOS
# ============================================================

def get_event_result(sport_key, event_id):

    url = (
        f"https://api.the-odds-api.com/v4/"
        f"sports/{sport_key}/scores/"
    )

    params = {
        "apiKey": ODDS_API_KEY,
        "daysFrom": 3,
        "eventIds": event_id,
    }

    # --------------------------------------------------------
    # LOG DE DIAGNÓSTICO
    # --------------------------------------------------------

    print("========================================")
    print("SCORES REQUEST")
    print("SPORT:", sport_key)
    print("EVENT ID:", event_id)
    print(
        "PARAMS: daysFrom=3 eventIds=",
        event_id,
    )

    try:

        response = requests.get(
            url,
            params=params,
            timeout=30,
        )

    except Exception as e:

        print(
            "SCORES REQUEST ERROR:",
            repr(e),
        )

        raise

    print(
        "SCORES STATUS:",
        response.status_code,
    )

    print(
        "SCORES RESPONSE:",
        response.text[:5000],
    )

    print("========================================")

    response.raise_for_status()

    data = response.json()

    for event in data:

        if event.get("id") == event_id:

            print(
                "SCORES MATCH FOUND:",
                event_id,
            )

            print(
                "SCORES COMPLETED:",
                event.get("completed"),
            )

            print(
                "SCORES DATA:",
                event.get("scores"),
            )

            return event

    print(
        "SCORES EVENT NOT FOUND IN RESPONSE:",
        event_id,
    )

    return None


def get_recent_completed_events(sport_key):

    url = (
        f"https://api.the-odds-api.com/v4/"
        f"sports/{sport_key}/scores/"
    )

    response = requests.get(
        url,
        params={
            "apiKey": ODDS_API_KEY,
            "daysFrom": 3,
        },
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# RESULTADO H2H
# ============================================================

def determine_h2h_result(event):

    scores = event.get("scores")

    if not scores or len(scores) < 2:
        return None

    home_team = event.get("home_team")
    away_team = event.get("away_team")

    home_score = None
    away_score = None

    for score in scores:

        if score.get("name") == home_team:

            try:
                home_score = int(score.get("score"))
            except (TypeError, ValueError):
                return None

        elif score.get("name") == away_team:

            try:
                away_score = int(score.get("score"))
            except (TypeError, ValueError):
                return None

    if home_score is None or away_score is None:
        return None

    if home_score > away_score:
        return home_team

    if away_score > home_score:
        return away_team

    return "Draw"


def evaluate_h2h_selection(event, selection):

    result = determine_h2h_result(event)

    if result is None:
        return None

    return result == selection


# ============================================================
# UTILIDADES DE RESULTADO
# ============================================================

def get_event_score_text(event):

    home_team = event.get(
        "home_team",
        "Local",
    )

    away_team = event.get(
        "away_team",
        "Visitante",
    )

    scores = event.get("scores") or []

    home_score = "?"
    away_score = "?"

    for score in scores:

        if score.get("name") == home_team:
            home_score = score.get("score")

        elif score.get("name") == away_team:
            away_score = score.get("score")

    return (
        home_team,
        away_team,
        home_score,
        away_score,
    )


# ============================================================
# LIQUIDACIÓN DE FILAS PENDIENTES
# ============================================================

def _settle_pending_rows(cur, rows, event):

    result = determine_h2h_result(event)

    if result is None:
        return None

    home_team = event.get(
        "home_team",
        "Local",
    )

    away_team = event.get(
        "away_team",
        "Visitante",
    )

    event_name = (
        f"{home_team} vs {away_team}"
    )

    match_date = parse_event_datetime(
        event.get("commence_time")
    )

    users_bets = {}

    for row in rows:

        user_id = row[1]

        users_bets.setdefault(
            user_id,
            []
        ).append(row)

    total_bets = 0
    won_count = 0
    lost_count = 0
    total_paid = 0
    total_stake = 0
    users_affected = 0

    results = []

    for user_id in sorted(users_bets.keys()):

        cur.execute(
            """
            SELECT balance
            FROM users
            WHERE id = %s
            FOR UPDATE
            """,
            (user_id,),
        )

        user_row = cur.fetchone()

        if not user_row:
            raise ValueError(
                f"Usuario {user_id} no encontrado."
            )

        current_balance = user_row[0]
        starting_balance = current_balance

        user_had_win = False

        for row in users_bets[user_id]:

            (
                pending_id,
                row_user_id,
                sport,
                competition,
                event_id,
                row_home_team,
                row_away_team,
                selection,
                odds,
                stake,
                potential_return,
                expires_at,
                created_at,
            ) = row

            won = result == selection

            balance_before = current_balance

            if won:

                current_balance = (
                    current_balance
                    + potential_return
                )

                won_count += 1
                total_paid += potential_return
                user_had_win = True

                status = "Ganada"
                transaction_type = "bet_win"
                transaction_amount = potential_return

            else:

                status = "Perdida"
                transaction_type = "bet_loss"
                transaction_amount = 0

                lost_count += 1

            total_bets += 1
            total_stake += stake

            cur.execute(
                """
                INSERT INTO bets (
                    user_id,
                    sport,
                    competition,
                    event_id,
                    event_name,
                    match_date,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    status,
                    created_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    row_user_id,
                    sport,
                    competition,
                    event_id,
                    event_name,
                    match_date,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    status,
                    created_at,
                ),
            )

            cur.execute(
                """
                INSERT INTO transactions (
                    user_id,
                    type,
                    amount,
                    balance_before,
                    balance_after
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    row_user_id,
                    transaction_type,
                    transaction_amount,
                    balance_before,
                    current_balance,
                ),
            )

            cur.execute(
                """
                DELETE FROM pending_bets
                WHERE id = %s
                """,
                (pending_id,),
            )

            results.append(
                {
                    "pending_id": pending_id,
                    "user_id": row_user_id,
                    "selection": selection,
                    "won": won,
                    "status": status,
                    "potential_return": potential_return,
                    "balance_after": current_balance,
                }
            )

        cur.execute(
            """
            UPDATE users
            SET balance = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (
                current_balance,
                user_id,
            ),
        )

        if current_balance != starting_balance:
            users_affected += 1
        elif user_had_win:
            users_affected += 1

    return {
        "result": result,
        "home_team": home_team,
        "away_team": away_team,
        "event_name": event_name,
        "match_date": match_date,
        "total_bets": total_bets,
        "won_count": won_count,
        "lost_count": lost_count,
        "total_paid": total_paid,
        "total_stake": total_stake,
        "users_affected": users_affected,
        "results": results,
    }


# ============================================================
# LIQUIDAR TODAS LAS APUESTAS DE UN PARTIDO
# ============================================================

def settle_event(event_id):

    with get_db_connection() as conn:

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
                ORDER BY user_id ASC, id ASC
                """,
                (event_id,),
            )

            preview_rows = cur.fetchall()

    if not preview_rows:

        return {
            "success": False,
            "already_processed": True,
            "message": (
                "No hay apuestas pendientes para este partido."
            ),
        }

    sport_key = preview_rows[0][3]

    event = get_event_result(
        sport_key,
        event_id,
    )

    if not event:

        return {
            "success": False,
            "message": (
                "El resultado todavía no está disponible "
                "en The Odds API."
            ),
        }

    if not event.get("completed"):

        return {
            "success": False,
            "message": "El partido todavía no ha terminado.",
            "event": event,
        }

    result = determine_h2h_result(event)

    if result is None:

        return {
            "success": False,
            "message": (
                "No fue posible determinar el resultado "
                "final del partido."
            ),
            "event": event,
        }

    with psycopg.connect(DATABASE_URL) as conn:

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
                ORDER BY user_id ASC, id ASC
                FOR UPDATE
                """,
                (event_id,),
            )

            rows = cur.fetchall()

            if not rows:

                return {
                    "success": False,
                    "already_processed": True,
                    "message": (
                        "Las apuestas de este partido "
                        "ya fueron procesadas."
                    ),
                    "event": event,
                }

            summary = _settle_pending_rows(
                cur,
                rows,
                event,
            )

    summary["success"] = True
    summary["event"] = event

    return summary


# ============================================================
# LIQUIDAR UNA SOLA APUESTA
# RESPALDO /LIQUIDAR
# ============================================================

def settle_bet(bet_id):

    with get_db_connection() as conn:

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
                (bet_id,),
            )

            pending_row = cur.fetchone()

    if not pending_row:

        with get_db_connection() as conn:

            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT status
                    FROM bets
                    WHERE id = %s
                    """,
                    (bet_id,),
                )

                old_row = cur.fetchone()

        if old_row:

            return {
                "success": False,
                "already_processed": True,
                "status": old_row[0],
                "message": (
                    f"La apuesta ya fue procesada. "
                    f"Estado actual: {old_row[0]}."
                ),
            }

        return {
            "success": False,
            "message": "La apuesta no existe.",
        }

    sport_key = pending_row[3]
    event_id = pending_row[4]

    event = get_event_result(
        sport_key,
        event_id,
    )

    if not event:

        return {
            "success": False,
            "message": (
                "El resultado todavía no está disponible "
                "en The Odds API."
            ),
        }

    if not event.get("completed"):

        return {
            "success": False,
            "message": "El partido todavía no ha terminado.",
        }

    if determine_h2h_result(event) is None:

        return {
            "success": False,
            "message": (
                "No fue posible determinar el resultado "
                "final del partido."
            ),
        }

    with psycopg.connect(DATABASE_URL) as conn:

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
                FOR UPDATE
                """,
                (bet_id,),
            )

            row = cur.fetchone()

            if not row:

                return {
                    "success": False,
                    "already_processed": True,
                    "message": (
                        "La apuesta ya fue procesada."
                    ),
                    "event": event,
                }

            summary = _settle_pending_rows(
                cur,
                [row],
                event,
            )

            result_data = summary["results"][0]

    return {
        "success": True,
        "won": result_data["won"],
        "status": result_data["status"],
        "event": event,
        "result": summary["result"],
        "balance": result_data["balance_after"],
        "potential_return": result_data[
            "potential_return"
        ],
    }


# ============================================================
# COMANDO /LIQUIDAR
# SOLO ADMIN
# ============================================================

async def test_settle(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    telegram_user = update.effective_user

    if not is_admin(telegram_user.id):

        await update.message.reply_text(
            "⛔ No tienes permisos para utilizar esta función."
        )

        return

    if not context.args:

        await update.message.reply_text(
            "Uso:\n/liquidar ID_APUESTA"
        )

        return

    try:

        bet_id = int(context.args[0])

    except ValueError:

        await update.message.reply_text(
            "❌ El ID de la apuesta debe ser un número."
        )

        return

    await update.message.reply_text(
        f"⏳ Liquidando apuesta pendiente #{bet_id}..."
    )

    try:

        result = settle_bet(bet_id)

    except Exception as e:

        await update.message.reply_text(
            f"❌ Error liquidando la apuesta:\n{e}"
        )

        return

    if not result["success"]:

        await update.message.reply_text(
            result["message"]
        )

        return

    event = result["event"]

    (
        home_team,
        away_team,
        home_score,
        away_score,
    ) = get_event_score_text(event)

    if result["won"]:

        message = (
            "✅ APUESTA LIQUIDADA\n\n"
            f"🎟 Apuesta pendiente #{bet_id}\n\n"
            f"⚽ {home_team}\n"
            "vs\n"
            f"⚽ {away_team}\n\n"
            "📊 Marcador:\n"
            f"• {home_team}: {home_score}\n"
            f"• {away_team}: {away_score}\n\n"
            "🏆 Resultado: GANADA\n\n"
            f"💰 Premio: {result['potential_return']}\n"
            f"💳 Nuevo saldo: {result['balance']}"
        )

    else:

        message = (
            "❌ APUESTA LIQUIDADA\n\n"
            f"🎟 Apuesta pendiente #{bet_id}\n\n"
            f"⚽ {home_team}\n"
            "vs\n"
            f"⚽ {away_team}\n\n"
            "📊 Marcador:\n"
            f"• {home_team}: {home_score}\n"
            f"• {away_team}: {away_score}\n\n"
            "🏆 Resultado: PERDIDA\n\n"
            f"💳 Saldo actual: {result['balance']}"
        )

    await update.message.reply_text(message)


# ============================================================
# COMANDO RESULTADOS
# ============================================================

async def test_recent_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not context.args:

        await update.message.reply_text(
            "Uso:\n/resultados SPORT_KEY"
        )

        return

    sport_key = context.args[0]

    try:

        events = get_recent_completed_events(
            sport_key
        )

    except Exception as e:

        await update.message.reply_text(
            f"❌ Error:\n{e}"
        )

        return

    if not events:

        await update.message.reply_text(
            "No se encontraron resultados."
        )

        return

    text = "📊 RESULTADOS RECIENTES\n\n"

    for event in events[:15]:

        home = event.get("home_team")
        away = event.get("away_team")

        scores = event.get("scores") or []

        score_text = "Sin marcador"

        if scores:

            score_map = {}

            for score in scores:
                score_map[
                    score.get("name")
                ] = score.get("score")

            score_text = (
                f"{score_map.get(home, '?')} - "
                f"{score_map.get(away, '?')}"
            )

        text += (
            f"⚽ {home}\n"
            f"vs {away}\n"
            f"📊 {score_text}\n"
            f"🆔 {event.get('id')}\n\n"
        )

    await update.message.reply_text(text)


# ============================================================
# COMANDO RESULTADO
# ============================================================

async def test_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if len(context.args) < 2:

        await update.message.reply_text(
            "Uso:\n/resultado SPORT_KEY EVENT_ID"
        )

        return

    sport_key = context.args[0]
    event_id = context.args[1]

    try:

        event = get_event_result(
            sport_key,
            event_id,
        )

    except Exception as e:

        await update.message.reply_text(
            f"❌ Error:\n{e}"
        )

        return

    if not event:

        await update.message.reply_text(
            "No se encontró el evento."
        )

        return

    text = (
        f"🏟 {event.get('home_team')}\n"
        f"vs\n"
        f"{event.get('away_team')}\n\n"
        f"🆔 {event.get('id')}\n"
        f"🏁 Completado: {event.get('completed')}\n\n"
    )

    scores = event.get("scores")

    if scores:

        text += "📊 Marcador:\n"

        for score in scores:

            text += (
                f"• {score.get('name')}: "
                f"{score.get('score')}\n"
            )

    else:

        text += "📊 Marcador no disponible."

    await update.message.reply_text(text)


# ============================================================
# COMANDO EVALUAR
# ============================================================

async def test_evaluate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if len(context.args) < 3:

        await update.message.reply_text(
            "Uso:\n/evaluar SPORT_KEY EVENT_ID SELECCION"
        )

        return

    sport_key = context.args[0]
    event_id = context.args[1]
    selection = " ".join(context.args[2:])

    try:

        event = get_event_result(
            sport_key,
            event_id,
        )

        if not event:

            await update.message.reply_text(
                "Evento no encontrado."
            )

            return

        result = evaluate_h2h_selection(
            event,
            selection,
        )

        if result is None:

            await update.message.reply_text(
                "No fue posible evaluar la selección."
            )

            return

        if result:

            await update.message.reply_text(
                "✅ La selección es GANADORA."
            )

        else:

            await update.message.reply_text(
                "❌ La selección es PERDEDORA."
            )

    except Exception as e:

        await update.message.reply_text(
            f"❌ Error:\n{e}"
        )


# ============================================================
# TECLADO PRINCIPAL
# ============================================================

def home_keyboard(user_id=None):

    keyboard = [
        [
            InlineKeyboardButton(
                "⚽ Fútbol",
                callback_data="football",
            )
        ],
        [
            InlineKeyboardButton(
                "⚾ Béisbol",
                callback_data="baseball",
            )
        ],
        [
            InlineKeyboardButton(
                "💰 Mi saldo",
                callback_data="balance",
            ),
            InlineKeyboardButton(
                "🎟 Mis apuestas",
                callback_data="mybets",
            ),
        ],
    ]

    if (
        user_id is not None
        and is_admin(user_id)
    ):

        keyboard.append(
            [
                InlineKeyboardButton(
                    "⚙️ Administración",
                    callback_data="admin",
                )
            ]
        )

    return InlineKeyboardMarkup(keyboard)


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    telegram_user = update.effective_user

    get_or_create_user(telegram_user)

    await update.message.reply_text(
        "🏆 CUBA SPORTS\n\n"
        "Bienvenido.\n"
        "Selecciona una opción:",
        reply_markup=home_keyboard(
            telegram_user.id
        ),
    )


# ============================================================
# DEPORTES
# ============================================================

async def show_sports(query):

    keyboard = [
        [
            InlineKeyboardButton(
                "🇪🇸 LaLiga",
                callback_data=(
                    "league:soccer_spain_la_liga"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🏴 Premier League",
                callback_data=(
                    "league:soccer_epl"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🇮🇹 Serie A",
                callback_data=(
                    "league:soccer_italy_serie_a"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🇩🇪 Bundesliga",
                callback_data=(
                    "league:soccer_germany_bundesliga"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🇫🇷 Ligue 1",
                callback_data=(
                    "league:soccer_france_ligue_one"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🌍 UEFA Nations League",
                callback_data=(
                    "league:soccer_uefa_nations_league"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Volver",
                callback_data="home",
            )
        ],
    ]

    await query.edit_message_text(
        "⚽ Selecciona una competición:",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# MOSTRAR PARTIDOS
# ============================================================

async def show_games(query, sport_key):

    try:

        events = get_odds(sport_key)

    except requests.exceptions.HTTPError as e:

        response = getattr(e, "response", None)

        if response is not None:

            try:

                error_data = response.json()

                error_message = error_data.get(
                    "message",
                    response.text,
                )

            except Exception:

                error_message = response.text

            text = (
                "❌ Error de The Odds API\n\n"
                f"{error_message}"
            )

        else:

            text = (
                f"❌ Error obteniendo partidos:\n{e}"
            )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Volver",
                            callback_data="football",
                        )
                    ]
                ]
            ),
        )

        return

    except Exception as e:

        await query.edit_message_text(
            f"❌ Error obteniendo partidos:\n{e}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Volver",
                            callback_data="football",
                        )
                    ]
                ]
            ),
        )

        return

    if not events:

        await query.edit_message_text(
            "No hay partidos disponibles en este momento.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Volver",
                            callback_data="football",
                        )
                    ]
                ]
            ),
        )

        return

    keyboard = []

    for event in events[:15]:

        event_id = event.get("id")

        home = event.get(
            "home_team",
            "",
        )

        away = event.get(
            "away_team",
            "",
        )

        pending_bets[
            f"event:{sport_key}:{event_id}"
        ] = event

        keyboard.append(
            [
                InlineKeyboardButton(
                    f"⚽ {home} vs {away}",
                    callback_data=(
                        f"game:{sport_key}:{event_id}"
                    ),
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "⬅️ Volver",
                callback_data="football",
            )
        ]
    )

    await query.edit_message_text(
        "📅 Partidos disponibles:",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# MOSTRAR PARTIDO
# ============================================================

async def show_game(
    query,
    sport_key,
    event_id,
):

    try:

        event = get_event_odds(
            sport_key,
            event_id,
        )

    except requests.exceptions.HTTPError as e:

        response = getattr(e, "response", None)

        if response is not None:

            try:

                error_data = response.json()

                error_message = error_data.get(
                    "message",
                    response.text,
                )

            except Exception:

                error_message = response.text

            text = (
                "❌ No se pudieron obtener las cuotas.\n\n"
                "The Odds API respondió:\n"
                f"{error_message}"
            )

        else:

            text = (
                "❌ No se pudieron obtener las cuotas.\n\n"
                f"{e}"
            )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Intentar nuevamente",
                            callback_data=(
                                f"game:{sport_key}:{event_id}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ Volver",
                            callback_data=(
                                f"league:{sport_key}"
                            ),
                        )
                    ],
                ]
            ),
        )

        return

    except Exception as e:

        await query.edit_message_text(
            f"❌ Error obteniendo cuotas:\n{e}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Intentar nuevamente",
                            callback_data=(
                                f"game:{sport_key}:{event_id}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ Volver",
                            callback_data=(
                                f"league:{sport_key}"
                            ),
                        )
                    ],
                ]
            ),
        )

        return

    if not event:

        await query.edit_message_text(
            "❌ The Odds API no devolvió información "
            "para este partido.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Volver",
                            callback_data=(
                                f"league:{sport_key}"
                            ),
                        )
                    ]
                ]
            ),
        )

        return

    home = event.get(
        "home_team",
        "Local",
    )

    away = event.get(
        "away_team",
        "Visitante",
    )

    outcomes = {}

    # --------------------------------------------------------
    # TODAS LAS CASAS - TOMAR MAYOR CUOTA
    # --------------------------------------------------------

    for bookmaker in event.get(
        "bookmakers",
        []
    ):

        for market in bookmaker.get(
            "markets",
            []
        ):

            if market.get("key") != "h2h":
                continue

            for outcome in market.get(
                "outcomes",
                []
            ):

                name = outcome.get("name")
                price = outcome.get("price")

                if (
                    name is None
                    or price is None
                ):
                    continue

                try:
                    price = float(price)

                except (
                    TypeError,
                    ValueError,
                ):
                    continue

                if (
                    name not in outcomes
                    or price > outcomes[name]
                ):
                    outcomes[name] = price

    # --------------------------------------------------------
    # SIN CUOTAS
    # --------------------------------------------------------

    if not outcomes:

        bookmakers_count = len(
            event.get(
                "bookmakers",
                []
            )
        )

        await query.edit_message_text(
            f"⚽ {home}\n"
            f"vs\n"
            f"⚽ {away}\n\n"
            "❌ No hay cuotas h2h disponibles "
            "para este partido.\n\n"
            f"📚 Casas encontradas: {bookmakers_count}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Actualizar cuotas",
                            callback_data=(
                                f"game:{sport_key}:{event_id}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ Volver",
                            callback_data=(
                                f"league:{sport_key}"
                            ),
                        )
                    ],
                ]
            ),
        )

        return

    # --------------------------------------------------------
    # BOTONES DE SELECCIÓN
    # --------------------------------------------------------

    keyboard = []

    selection_index = 0

    for name, price in outcomes.items():

        pick_id = (
            f"pick:{event_id}:"
            f"{selection_index}"
        )

        pending_bets[pick_id] = {
            "sport_key": sport_key,
            "event_id": event_id,
            "home": home,
            "away": away,
            "selection": name,
            "odds": price,
        }

        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{name} — {price:.2f}",
                    callback_data=pick_id,
                )
            ]
        )

        selection_index += 1

    keyboard.append(
        [
            InlineKeyboardButton(
                "🔄 Actualizar cuotas",
                callback_data=(
                    f"game:{sport_key}:{event_id}"
                ),
            )
        ]
    )

    keyboard.append(
        [
            InlineKeyboardButton(
                "⬅️ Volver",
                callback_data=(
                    f"league:{sport_key}"
                ),
            )
        ]
    )

    await query.edit_message_text(
        f"⚽ {home}\n"
        f"vs\n"
        f"⚽ {away}\n\n"
        "📈 CUOTAS DISPONIBLES\n\n"
        "Selecciona tu apuesta:",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# PEDIR MONTO
# ============================================================

async def ask_amount(
    query,
    pick_id,
):

    user_id = query.from_user.id

    pick = pending_bets.get(pick_id)

    if not pick:

        await query.edit_message_text(
            "❌ Esta selección ya no está disponible.\n\n"
            "Vuelve a seleccionar el partido."
        )

        return

    balance = get_user_balance(
        user_id
    )

    pending_bets[
        f"active:{user_id}"
    ] = pick

    await query.edit_message_text(
        f"⚽ {pick['home']}\n"
        f"vs\n"
        f"⚽ {pick['away']}\n\n"
        f"🎯 Selección: {pick['selection']}\n"
        f"📈 Cuota: {float(pick['odds']):.2f}\n\n"
        f"💰 Saldo disponible: {balance}\n\n"
        "Escribe el monto que deseas apostar:"
    )


# ============================================================
# RECIBIR MONTO
# ============================================================

async def handle_amount(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    active = pending_bets.get(
        f"active:{user_id}"
    )

    if not active:
        return

    text = update.message.text.strip()

    try:

        stake = float(text)

    except ValueError:

        await update.message.reply_text(
            "❌ Escribe solamente un número.\n\n"
            "Ejemplo: 100"
        )

        return

    if stake <= 0:

        await update.message.reply_text(
            "❌ El monto debe ser mayor que 0."
        )

        return

    balance = get_user_balance(
        user_id
    )

    if stake > float(balance):

        await update.message.reply_text(
            "❌ Saldo insuficiente.\n\n"
            f"💰 Tu saldo: {balance}"
        )

        return

    potential_return = (
        stake * float(active["odds"])
    )

    event_name = (
        f"{active['home']} vs "
        f"{active['away']}"
    )

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO unconfirmed_bets (
                    user_id,
                    event_id,
                    event_name,
                    sport,
                    competition,
                    home_team,
                    away_team,
                    selection,
                    odds,
                    stake,
                    potential_return
                )
                VALUES (
                    (
                        SELECT id
                        FROM users
                        WHERE telegram_id = %s
                    ),
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                RETURNING id
                """,
                (
                    user_id,
                    active["event_id"],
                    event_name,
                    "football",
                    active["sport_key"],
                    active["home"],
                    active["away"],
                    active["selection"],
                    active["odds"],
                    stake,
                    potential_return,
                ),
            )

            unconfirmed_id = cur.fetchone()[0]

    pending_bets[
        f"unconfirmed:{user_id}"
    ] = {
        "id": unconfirmed_id,
        **active,
        "stake": stake,
        "potential_return": potential_return,
    }

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Confirmar apuesta",
                callback_data=(
                    f"confirm:{unconfirmed_id}"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data=(
                    f"cancel:{unconfirmed_id}"
                ),
            )
        ],
    ]

    await update.message.reply_text(
        "🎟 CONFIRMAR APUESTA\n\n"
        f"⚽ {active['home']}\n"
        f"vs\n"
        f"⚽ {active['away']}\n\n"
        f"🎯 Selección: {active['selection']}\n"
        f"📈 Cuota: {float(active['odds']):.2f}\n"
        f"💰 Apuesta: {stake}\n"
        f"🏆 Posible retorno: "
        f"{potential_return:.2f}\n\n"
        "¿Deseas confirmar?",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# CONFIRMAR APUESTA
# ============================================================

async def confirm_bet(
    query,
    unconfirmed_id,
):

    user_id = query.from_user.id

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    ub.id,
                    ub.event_id,
                    ub.event_name,
                    ub.sport,
                    ub.competition,
                    ub.home_team,
                    ub.away_team,
                    ub.selection,
                    ub.odds,
                    ub.stake,
                    ub.potential_return,
                    u.id,
                    u.balance
                FROM unconfirmed_bets ub
                JOIN users u
                    ON u.id = ub.user_id
                WHERE ub.id = %s
                  AND u.telegram_id = %s
                FOR UPDATE
                """,
                (
                    unconfirmed_id,
                    user_id,
                ),
            )

            row = cur.fetchone()

            if not row:

                await query.edit_message_text(
                    "❌ La apuesta ya no está disponible."
                )

                return

            (
                uc_id,
                event_id,
                event_name,
                sport,
                competition,
                home_team,
                away_team,
                selection,
                odds,
                stake,
                potential_return,
                db_user_id,
                balance,
            ) = row

            if balance < stake:

                await query.edit_message_text(
                    "❌ Saldo insuficiente."
                )

                return

            balance_before = balance
            balance_after = (
                balance - stake
            )

            cur.execute(
                """
                INSERT INTO pending_bets (
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
                    expires_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    NULL
                )
                RETURNING id
                """,
                (
                    db_user_id,
                    sport,
                    competition,
                    event_id,
                    home_team,
                    away_team,
                    selection,
                    odds,
                    stake,
                    potential_return,
                ),
            )

            pending_bet_id = (
                cur.fetchone()[0]
            )

            cur.execute(
                """
                UPDATE users
                SET balance = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (
                    balance_after,
                    db_user_id,
                ),
            )

            cur.execute(
                """
                INSERT INTO transactions (
                    user_id,
                    type,
                    amount,
                    balance_before,
                    balance_after
                )
                VALUES (
                    %s,
                    'bet_placed',
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    db_user_id,
                    -stake,
                    balance_before,
                    balance_after,
                ),
            )

            cur.execute(
                """
                DELETE FROM unconfirmed_bets
                WHERE id = %s
                """,
                (uc_id,),
            )

    pending_bets.pop(
        f"unconfirmed:{user_id}",
        None,
    )

    pending_bets.pop(
        f"active:{user_id}",
        None,
    )

    await query.edit_message_text(
        "✅ APUESTA CONFIRMADA\n\n"
        f"🎟 Apuesta pendiente #{pending_bet_id}\n\n"
        f"⚽ {event_name}\n\n"
        f"🎯 Selección: {selection}\n"
        f"📈 Cuota: {float(odds):.2f}\n"
        f"💰 Apostado: {stake}\n"
        f"🏆 Posible retorno: "
        f"{float(potential_return):.2f}\n\n"
        f"💳 Saldo restante: {balance_after}"
    )


# ============================================================
# CANCELAR APUESTA
# ============================================================

async def cancel_bet(
    query,
    unconfirmed_id,
):

    user_id = query.from_user.id

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                DELETE FROM unconfirmed_bets
                WHERE id = %s
                  AND user_id = (
                      SELECT id
                      FROM users
                      WHERE telegram_id = %s
                  )
                """,
                (
                    unconfirmed_id,
                    user_id,
                ),
            )

            deleted = cur.rowcount

    pending_bets.pop(
        f"unconfirmed:{user_id}",
        None,
    )

    pending_bets.pop(
        f"active:{user_id}",
        None,
    )

    if deleted:

        await query.edit_message_text(
            "❌ Apuesta cancelada."
        )

    else:

        await query.edit_message_text(
            "La apuesta ya había sido cancelada."
        )


# ============================================================
# MIS APUESTAS
# ============================================================

async def show_bets(query):

    user_id = query.from_user.id

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    pb.id,
                    pb.home_team,
                    pb.away_team,
                    pb.selection,
                    pb.odds,
                    pb.stake,
                    pb.potential_return,
                    pb.created_at
                FROM pending_bets pb
                JOIN users u
                    ON u.id = pb.user_id
                WHERE u.telegram_id = %s
                ORDER BY pb.created_at DESC
                LIMIT 20
                """,
                (user_id,),
            )

            pending_rows = cur.fetchall()

            cur.execute(
                """
                SELECT
                    b.id,
                    b.event_name,
                    b.selection,
                    b.odds,
                    b.stake,
                    b.potential_return,
                    b.status,
                    b.created_at
                FROM bets b
                JOIN users u
                    ON u.id = b.user_id
                WHERE u.telegram_id = %s
                ORDER BY b.created_at DESC
                LIMIT 20
                """,
                (user_id,),
            )

            settled_rows = cur.fetchall()

    combined = []

    for row in pending_rows:

        (
            bet_id,
            home,
            away,
            selection,
            odds,
            stake,
            potential_return,
            created_at,
        ) = row

        combined.append(
            (
                created_at,
                "pending",
                (
                    bet_id,
                    f"{home} vs {away}",
                    selection,
                    odds,
                    stake,
                    potential_return,
                    "Pendiente",
                ),
            )
        )

    for row in settled_rows:

        (
            bet_id,
            event_name,
            selection,
            odds,
            stake,
            potential_return,
            status,
            created_at,
        ) = row

        combined.append(
            (
                created_at,
                "settled",
                (
                    bet_id,
                    event_name,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    status,
                ),
            )
        )

    combined.sort(
        key=lambda item: (
            item[0] or datetime.min
        ),
        reverse=True,
    )

    combined = combined[:20]

    if not combined:

        keyboard = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="home",
                )
            ]
        ]

        await query.edit_message_text(
            "🎟 No tienes apuestas todavía.",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            ),
        )

        return

    text = "🎟 MIS APUESTAS\n\n"

    for (
        created_at,
        source,
        row,
    ) in combined:

        (
            bet_id,
            event_name,
            selection,
            odds,
            stake,
            potential_return,
            status,
        ) = row

        if status == "Pendiente":

            icon = "⏳"
            id_text = (
                f"Apuesta pendiente #{bet_id}"
            )

        elif status == "Ganada":

            icon = "✅"
            id_text = f"Apuesta #{bet_id}"

        else:

            icon = "❌"
            id_text = f"Apuesta #{bet_id}"

        text += (
            f"{icon} {id_text}\n"
            f"⚽ {event_name}\n"
            f"🎯 {selection}\n"
            f"📈 Cuota: {odds}\n"
            f"💰 Monto: {stake}\n"
            f"🏆 Posible retorno: "
            f"{potential_return}\n"
            f"📌 Estado: {status}\n\n"
        )

    keyboard = [
        [
            InlineKeyboardButton(
                "⬅️ Volver",
                callback_data="home",
            )
        ]
    ]

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# PANEL ADMIN
# AGRUPADO POR PARTIDO
# ============================================================

async def show_admin_bets(query):

    if not is_admin(query.from_user.id):

        await query.answer(
            "⛔ No tienes permisos.",
            show_alert=True,
        )

        return

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    event_id,
                    MAX(home_team),
                    MAX(away_team),
                    MAX(competition),
                    COUNT(*),
                    COALESCE(SUM(stake), 0),
                    MIN(created_at)
                FROM pending_bets
                GROUP BY event_id
                ORDER BY MIN(created_at) ASC
                """
            )

            rows = cur.fetchall()

    if not rows:

        keyboard = [
            [
                InlineKeyboardButton(
                    "🔄 Actualizar",
                    callback_data="admin",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="home",
                )
            ],
        ]

        await query.edit_message_text(
            "⚙️ ADMINISTRACIÓN\n\n"
            "✅ No hay partidos con apuestas pendientes.",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            ),
        )

        return

    keyboard = []

    for row in rows:

        (
            event_id,
            home_team,
            away_team,
            competition,
            bet_count,
            total_stake,
            created_at,
        ) = row

        keyboard.append(
            [
                InlineKeyboardButton(
                    (
                        f"⚽ {home_team} vs {away_team} "
                        f"• 🎟 {bet_count}"
                    ),
                    callback_data=(
                        f"adminevent:{event_id}"
                    ),
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "🔄 Actualizar",
                callback_data="admin",
            )
        ]
    )

    keyboard.append(
        [
            InlineKeyboardButton(
                "⬅️ Volver",
                callback_data="home",
            )
        ]
    )

    await query.edit_message_text(
        "⚙️ ADMINISTRACIÓN\n\n"
        f"🏟 Partidos pendientes: {len(rows)}\n\n"
        "Cada partido aparece una sola vez.\n"
        "Selecciona un partido para ver todas sus apuestas:",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# DETALLE DE PARTIDO PARA ADMIN
# ============================================================

async def show_admin_event(
    query,
    event_id,
):

    if not is_admin(query.from_user.id):

        await query.answer(
            "⛔ No tienes permisos.",
            show_alert=True,
        )

        return

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    pb.id,
                    u.username,
                    u.telegram_id,
                    pb.sport,
                    pb.competition,
                    pb.event_id,
                    pb.home_team,
                    pb.away_team,
                    pb.selection,
                    pb.odds,
                    pb.stake,
                    pb.potential_return,
                    pb.created_at
                FROM pending_bets pb
                JOIN users u
                    ON u.id = pb.user_id
                WHERE pb.event_id = %s
                ORDER BY pb.id ASC
                """,
                (event_id,),
            )

            rows = cur.fetchall()

    if not rows:

        await query.edit_message_text(
            "❌ No hay apuestas pendientes para este partido.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Volver",
                            callback_data="admin",
                        )
                    ]
                ]
            ),
        )

        return

    first = rows[0]

    home_team = first[6]
    away_team = first[7]
    competition = first[4]

    total_stake = sum(
        float(row[10])
        for row in rows
    )

    total_potential = sum(
        float(row[11])
        for row in rows
    )

    text = (
        "⚙️ PARTIDO PENDIENTE\n\n"
        f"⚽ {home_team}\n"
        f"vs\n"
        f"⚽ {away_team}\n\n"
        f"🏆 Competición: {competition}\n"
        f"🆔 Event ID: {event_id}\n\n"
        f"🎟 Apuestas: {len(rows)}\n"
        f"💰 Total apostado: {total_stake:.2f}\n"
        f"🏆 Retornos posibles: "
        f"{total_potential:.2f}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )

    max_display = 25

    for index, row in enumerate(
        rows[:max_display],
        start=1,
    ):

        (
            pending_id,
            username,
            telegram_id,
            sport,
            competition,
            event_id,
            home_team,
            away_team,
            selection,
            odds,
            stake,
            potential_return,
            created_at,
        ) = row

        display_user = (
            f"@{username}"
            if username
            else str(telegram_id)
        )

        text += (
            f"\n{index}. 👤 {display_user}\n"
            f"   🎟 #{pending_id}\n"
            f"   🎯 {selection}\n"
            f"   📈 Cuota: {odds}\n"
            f"   💰 Apuesta: {stake}\n"
            f"   🏆 Retorno: {potential_return}\n"
        )

    if len(rows) > max_display:

        text += (
            f"\n\n⚠️ Mostrando {max_display} "
            f"de {len(rows)} apuestas."
        )

    keyboard = [
        [
            InlineKeyboardButton(
                "💰 LIQUIDAR PARTIDO",
                callback_data=(
                    f"settleevent:{event_id}"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Actualizar",
                callback_data=(
                    f"adminevent:{event_id}"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Apuestas pendientes",
                callback_data="admin",
            )
        ],
    ]

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# LIQUIDAR PARTIDO DESDE ADMIN
# ============================================================

async def admin_settle_event(
    query,
    event_id,
):

    if not is_admin(query.from_user.id):

        await query.answer(
            "⛔ No tienes permisos.",
            show_alert=True,
        )

        return

    await query.edit_message_text(
        "⏳ Consultando resultado del partido...\n\n"
        "Esto hará una sola consulta a The Odds API "
        "y procesará todas las apuestas del evento."
    )

    try:

        result = settle_event(
            event_id
        )

    except requests.exceptions.HTTPError as e:

        response = getattr(
            e,
            "response",
            None,
        )

        if response is not None:

            try:

                error_data = response.json()

                error_message = error_data.get(
                    "message",
                    response.text,
                )

            except Exception:

                error_message = response.text

            message = (
                "❌ Error de The Odds API\n\n"
                f"{error_message}"
            )

        else:

            message = (
                "❌ Error consultando "
                f"The Odds API:\n{e}"
            )

        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Administración",
                            callback_data="admin",
                        )
                    ]
                ]
            ),
        )

        return

    except Exception as e:

        await query.edit_message_text(
            f"❌ Error liquidando el partido:\n{e}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Administración",
                            callback_data="admin",
                        )
                    ]
                ]
            ),
        )

        return

    if not result["success"]:

        keyboard = []

        if not result.get(
            "already_processed",
            False,
        ):

            keyboard.append(
                [
                    InlineKeyboardButton(
                        "🔄 Volver a intentar",
                        callback_data=(
                            f"adminevent:{event_id}"
                        ),
                    )
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton(
                    "⬅️ Administración",
                    callback_data="admin",
                )
            ]
        )

        await query.edit_message_text(
            "⚠️ NO SE PUDO LIQUIDAR\n\n"
            f"{result['message']}",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            ),
        )

        return

    event = result["event"]

    (
        home_team,
        away_team,
        home_score,
        away_score,
    ) = get_event_score_text(event)

    result_name = result["result"]

    text = (
        "✅ PARTIDO LIQUIDADO\n\n"
        f"⚽ {home_team}\n"
        "vs\n"
        f"⚽ {away_team}\n\n"
        "📊 MARCADOR\n"
        f"• {home_team}: {home_score}\n"
        f"• {away_team}: {away_score}\n\n"
        f"🏆 Resultado: {result_name}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📋 RESUMEN\n"
        f"🎟 Apuestas procesadas: "
        f"{result['total_bets']}\n"
        f"✅ Ganadas: {result['won_count']}\n"
        f"❌ Perdidas: {result['lost_count']}\n"
        f"💰 Total apostado: "
        f"{result['total_stake']}\n"
        f"🏆 Premios pagados: "
        f"{result['total_paid']}\n"
        f"👤 Usuarios afectados: "
        f"{result['users_affected']}\n\n"
        "Todas las apuestas fueron movidas "
        "a `bets` como historial."
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "⚙️ Ver partidos pendientes",
                callback_data="admin",
            )
        ],
        [
            InlineKeyboardButton(
                "🏠 Inicio",
                callback_data="home",
            )
        ],
    ]

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# COMPATIBILIDAD CON BOTONES ADMIN ANTIGUOS
# ============================================================

async def show_admin_bet(
    query,
    bet_id,
):

    if not is_admin(query.from_user.id):

        await query.answer(
            "⛔ No tienes permisos.",
            show_alert=True,
        )

        return

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT event_id
                FROM pending_bets
                WHERE id = %s
                """,
                (bet_id,),
            )

            row = cur.fetchone()

    if not row:

        await query.edit_message_text(
            "❌ Esa apuesta ya no está pendiente.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Administración",
                            callback_data="admin",
                        )
                    ]
                ]
            ),
        )

        return

    await show_admin_event(
        query,
        row[0],
    )


async def admin_settle_bet(
    query,
    bet_id,
):

    if not is_admin(query.from_user.id):

        await query.answer(
            "⛔ No tienes permisos.",
            show_alert=True,
        )

        return

    await query.edit_message_text(
        f"⏳ Liquidando apuesta pendiente #{bet_id}..."
    )

    try:

        result = settle_bet(
            bet_id
        )

    except Exception as e:

        await query.edit_message_text(
            f"❌ Error:\n{e}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Administración",
                            callback_data="admin",
                        )
                    ]
                ]
            ),
        )

        return

    if not result["success"]:

        await query.edit_message_text(
            f"⚠️ No se pudo liquidar.\n\n"
            f"{result['message']}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Administración",
                            callback_data="admin",
                        )
                    ]
                ]
            ),
        )

        return

    event = result["event"]

    (
        home_team,
        away_team,
        home_score,
        away_score,
    ) = get_event_score_text(event)

    if result["won"]:

        text = (
            "✅ APUESTA LIQUIDADA\n\n"
            f"🎟 Apuesta #{bet_id}\n\n"
            f"⚽ {home_team}\n"
            "vs\n"
            f"⚽ {away_team}\n\n"
            "📊 Marcador:\n"
            f"• {home_team}: {home_score}\n"
            f"• {away_team}: {away_score}\n\n"
            "🏆 Resultado: GANADA\n\n"
            f"💰 Premio pagado: "
            f"{result['potential_return']}\n"
            f"💳 Nuevo saldo: "
            f"{result['balance']}"
        )

    else:

        text = (
            "❌ APUESTA LIQUIDADA\n\n"
            f"🎟 Apuesta #{bet_id}\n\n"
            f"⚽ {home_team}\n"
            "vs\n"
            f"⚽ {away_team}\n\n"
            "📊 Marcador:\n"
            f"• {home_team}: {home_score}\n"
            f"• {away_team}: {away_score}\n\n"
            "🏆 Resultado: PERDIDA\n\n"
            f"💳 Saldo del usuario: "
            f"{result['balance']}"
        )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⚙️ Ver partidos pendientes",
                        callback_data="admin",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 Inicio",
                        callback_data="home",
                    )
                ],
            ]
        ),
    )


# ============================================================
# BOTONES
# ============================================================

async def button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = query.data

    if data == "home":

        await query.edit_message_text(
            "🏆 CUBA SPORTS\n\n"
            "Selecciona una opción:",
            reply_markup=home_keyboard(
                query.from_user.id
            ),
        )

        return

    if data == "football":

        await show_sports(query)

        return

    if data == "baseball":

        await query.edit_message_text(
            "⚾ Béisbol\n\n"
            "Esta sección estará disponible próximamente.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Volver",
                            callback_data="home",
                        )
                    ]
                ]
            ),
        )

        return

    if data == "balance":

        balance = get_user_balance(
            query.from_user.id
        )

        await query.edit_message_text(
            "💰 MI SALDO\n\n"
            f"💳 Saldo disponible: {balance}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Volver",
                            callback_data="home",
                        )
                    ]
                ]
            ),
        )

        return

    if data == "mybets":

        await show_bets(query)

        return

    if data == "admin":

        await show_admin_bets(query)

        return

    if data.startswith("adminevent:"):

        if not is_admin(
            query.from_user.id
        ):

            await query.answer(
                "⛔ No tienes permisos.",
                show_alert=True,
            )

            return

        event_id = data.split(
            ":",
            1,
        )[1]

        await show_admin_event(
            query,
            event_id,
        )

        return

    if data.startswith("settleevent:"):

        if not is_admin(
            query.from_user.id
        ):

            await query.answer(
                "⛔ No tienes permisos.",
                show_alert=True,
            )

            return

        event_id = data.split(
            ":",
            1,
        )[1]

        await admin_settle_event(
            query,
            event_id,
        )

        return

    if data.startswith("adminbet:"):

        if not is_admin(
            query.from_user.id
        ):

            await query.answer(
                "⛔ No tienes permisos.",
                show_alert=True,
            )

            return

        try:

            bet_id = int(
                data.split(
                    ":",
                    1,
                )[1]
            )

        except ValueError:

            await query.edit_message_text(
                "❌ ID de apuesta inválido."
            )

            return

        await show_admin_bet(
            query,
            bet_id,
        )

        return

    if data.startswith("settle:"):

        if not is_admin(
            query.from_user.id
        ):

            await query.answer(
                "⛔ No tienes permisos.",
                show_alert=True,
            )

            return

        try:

            bet_id = int(
                data.split(
                    ":",
                    1,
                )[1]
            )

        except ValueError:

            await query.edit_message_text(
                "❌ ID de apuesta inválido."
            )

            return

        await admin_settle_bet(
            query,
            bet_id,
        )

        return

    if data.startswith("confirm:"):

        try:

            unconfirmed_id = int(
                data.split(
                    ":",
                    1,
                )[1]
            )

        except ValueError:

            await query.edit_message_text(
                "❌ ID inválido."
            )

            return

        await confirm_bet(
            query,
            unconfirmed_id,
        )

        return

    if data.startswith("cancel:"):

        try:

            unconfirmed_id = int(
                data.split(
                    ":",
                    1,
                )[1]
            )

        except ValueError:

            await query.edit_message_text(
                "❌ ID inválido."
            )

            return

        await cancel_bet(
            query,
            unconfirmed_id,
        )

        return

    if data.startswith("league:"):

        sport_key = data.split(
            ":",
            1,
        )[1]

        await show_games(
            query,
            sport_key,
        )

        return

    if data.startswith("game:"):

        parts = data.split(
            ":",
            2,
        )

        if len(parts) != 3:

            await query.edit_message_text(
                "❌ Partido inválido."
            )

            return

        sport_key = parts[1]
        event_id = parts[2]

        await show_game(
            query,
            sport_key,
            event_id,
        )

        return

    if data.startswith("pick:"):

        await ask_amount(
            query,
            data,
        )

        return


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "Falta BOT_TOKEN en las variables de entorno."
        )

    if not ODDS_API_KEY:

        raise RuntimeError(
            "Falta ODDS_API_KEY en las variables de entorno."
        )

    if not DATABASE_URL:

        raise RuntimeError(
            "Falta DATABASE_URL en las variables de entorno."
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # MANEJADOR GLOBAL DE ERRORES
    # --------------------------------------------------------

    application.add_error_handler(
        telegram_error_handler
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "liquidar",
            test_settle,
        )
    )

    application.add_handler(
        CommandHandler(
            "resultados",
            test_recent_results,
        )
    )

    application.add_handler(
        CommandHandler(
            "resultado",
            test_result,
        )
    )

    application.add_handler(
        CommandHandler(
            "evaluar",
            test_evaluate,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            button
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_amount,
        )
    )

    print(
        "🏆 Cuba Sports iniciado correctamente."
    )

    application.run_polling()


if __name__ == "__main__":
    main()
