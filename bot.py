import os
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

BOT_TOKEN = os.getenv("BOT_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

bets = {}
pending_bets = {}


def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("Falta DATABASE_URL")

    return psycopg.connect(DATABASE_URL)


def test_database_connection():
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
            result = cursor.fetchone()

    print("✅ CONEXIÓN CON NEON CORRECTA:", result)


def create_database_tables():
    with get_db_connection() as conn:
        with conn.cursor() as cursor:

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    balance NUMERIC(18,2) NOT NULL DEFAULT 1000,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bets (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id),
                    sport TEXT NOT NULL,
                    competition TEXT,
                    event_id TEXT NOT NULL,
                    event_name TEXT,
                    match_date TIMESTAMPTZ,
                    selection TEXT NOT NULL,
                    odds NUMERIC(10,4) NOT NULL,
                    stake NUMERIC(18,2) NOT NULL,
                    potential_return NUMERIC(18,2) NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Pendiente',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

            cursor.execute("""
                ALTER TABLE bets
                ADD COLUMN IF NOT EXISTS match_date TIMESTAMPTZ;
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id),
                    type TEXT NOT NULL,
                    amount NUMERIC(18,2) NOT NULL,
                    balance_before NUMERIC(18,2) NOT NULL,
                    balance_after NUMERIC(18,2) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pending_bets (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id),
                    sport TEXT,
                    competition TEXT,
                    event_id TEXT NOT NULL,
                    home_team TEXT,
                    away_team TEXT,
                    selection TEXT NOT NULL,
                    odds NUMERIC(10,4) NOT NULL,
                    stake NUMERIC(18,2),
                    potential_return NUMERIC(18,2),
                    expires_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS unconfirmed_bets (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    event_id TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    sport TEXT NOT NULL,
                    competition TEXT,
                    selection TEXT NOT NULL,
                    odds NUMERIC(10,2) NOT NULL,
                    stake NUMERIC(18,2),
                    potential_return NUMERIC(10,2),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

        conn.commit()

    print("✅ TABLAS DE NEON CREADAS/VERIFICADAS")


def get_or_create_user(telegram_user):
    telegram_id = telegram_user.id
    username = telegram_user.username

    with get_db_connection() as conn:
        with conn.cursor() as cursor:

            cursor.execute("""
                SELECT id, balance
                FROM users
                WHERE telegram_id = %s
            """, (telegram_id,))

            user = cursor.fetchone()

            if user:
                cursor.execute("""
                    UPDATE users
                    SET username = %s,
                        updated_at = NOW()
                    WHERE telegram_id = %s
                """, (username, telegram_id))

                conn.commit()

                return {
                    "id": user[0],
                    "balance": float(user[1]),
                }

            cursor.execute("""
                INSERT INTO users (
                    telegram_id,
                    username,
                    balance
                )
                VALUES (%s, %s, 1000)
                RETURNING id, balance
            """, (telegram_id, username))

            new_user = cursor.fetchone()

        conn.commit()

    print("✅ USUARIO GUARDADO EN NEON:", telegram_id)

    return {
        "id": new_user[0],
        "balance": float(new_user[1]),
    }


def get_user_balance(telegram_id):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:

            cursor.execute("""
                SELECT balance
                FROM users
                WHERE telegram_id = %s
            """, (telegram_id,))

            result = cursor.fetchone()

    if not result:
        return None

    return float(result[0])


def get_sports():
    url = "https://api.the-odds-api.com/v4/sports/"

    params = {
        "apiKey": ODDS_API_KEY
    }

    response = requests.get(
        url,
        params=params,
        timeout=15
    )

    response.raise_for_status()

    return response.json()


def get_odds(sport_key):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }

    response = requests.get(
        url,
        params=params,
        timeout=15
    )

    response.raise_for_status()

    return response.json()


# ==========================================================
# RESULTADOS DE EVENTOS
# ==========================================================

def get_event_result(sport_key, event_id):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/"

    params = {
        "apiKey": ODDS_API_KEY,
        "daysFrom": 3,
        "eventIds": event_id,
        "dateFormat": "iso",
    }

    response = requests.get(
        url,
        params=params,
        timeout=15
    )

    response.raise_for_status()

    games = response.json()

    if not games:
        return None

    game = games[0]

    if not game.get("completed"):
        return None

    return {
        "event_id": game["id"],
        "sport_key": game.get("sport_key"),
        "home_team": game.get("home_team"),
        "away_team": game.get("away_team"),
        "completed": game.get("completed", False),
        "scores": game.get("scores") or [],
    }


# ==========================================================
# BUSCAR PARTIDOS TERMINADOS RECIENTES
# ==========================================================

def get_recent_completed_events(sport_key):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/"

    params = {
        "apiKey": ODDS_API_KEY,
        "daysFrom": 3,
        "dateFormat": "iso",
    }

    response = requests.get(
        url,
        params=params,
        timeout=15
    )

    response.raise_for_status()

    games = response.json()

    completed_games = []

    for game in games:
        if game.get("completed"):
            completed_games.append(game)

    return completed_games


# ==========================================================
# DETERMINAR RESULTADO H2H
# ==========================================================

def determine_h2h_result(
    home_team,
    away_team,
    scores
):
    home_score = None
    away_score = None

    for score in scores:
        name = score.get("name")
        value = score.get("score")

        if name == home_team:
            home_score = int(value)

        elif name == away_team:
            away_score = int(value)

    if home_score is None or away_score is None:
        return None

    if home_score > away_score:
        return "local"

    if away_score > home_score:
        return "visitante"

    return "empate"


def evaluate_h2h_selection(
    selection,
    home_team,
    away_team,
    scores
):
    result = determine_h2h_result(
        home_team,
        away_team,
        scores
    )

    if result is None:
        return None

    selection_normalized = selection.strip().lower()

    home_normalized = home_team.strip().lower()
    away_normalized = away_team.strip().lower()

    draw_names = {
        "draw",
        "empate",
        "tie",
    }

    if result == "local":
        if selection_normalized == home_normalized:
            return "Ganada"

        return "Perdida"

    if result == "visitante":
        if selection_normalized == away_normalized:
            return "Ganada"

        return "Perdida"

    if result == "empate":
        if selection_normalized in draw_names:
            return "Ganada"

        return "Perdida"

    return None

async def settle_bet(bet_id):
    """
    Liquida una apuesta individual de forma atómica.

    - Solo procesa apuestas con status = 'Pendiente'
    - Consulta el resultado real de The Odds API
    - Marca la apuesta como Ganada o Perdida
    - Si gana, acredita potential_return
    - Registra la transacción
    - Evita pagos duplicados
    """

    conn = None

    try:
        conn = psycopg.connect(DATABASE_URL)

        with conn.cursor() as cur:

            # 1. Bloquear la apuesta para evitar doble liquidación
            cur.execute(
                """
                SELECT
                    id,
                    user_id,
                    competition,
                    event_id,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    status
                FROM bets
                WHERE id = %s
                FOR UPDATE
                """,
                (bet_id,)
            )

            bet = cur.fetchone()

            if not bet:
                conn.rollback()
                return {
                    "success": False,
                    "message": "Apuesta no encontrada."
                }

            (
                db_bet_id,
                user_id,
                sport_key,
                event_id,
                selection,
                odds,
                stake,
                potential_return,
                status
            ) = bet

            # 2. Protección contra doble liquidación
            if status != "Pendiente":
                conn.rollback()

                return {
                    "success": False,
                    "message": (
                        f"La apuesta ya fue procesada.\n"
                        f"Estado actual: {status}"
                    )
                }

            # 3. Obtener resultado real
            result = get_event_result(sport_key, event_id)

            if not result:
                conn.rollback()

                return {
                    "success": False,
                    "message": (
                        "El partido todavía no tiene un resultado "
                        "final disponible."
                    )
                }

            home_team = result["home_team"]
            away_team = result["away_team"]
            scores = result["scores"]

            # 4. Evaluar la selección
            evaluation = evaluate_h2h_selection(
                selection,
                home_team,
                away_team,
                scores
            )

            if evaluation is None:
                conn.rollback()

                return {
                    "success": False,
                    "message": (
                        "No se pudo determinar el resultado "
                        "de la apuesta."
                    )
                }

            # -------------------------------------------------
            # APUESTA PERDIDA
            # -------------------------------------------------
            if evaluation == "Perdida":

                cur.execute(
                    """
                    UPDATE bets
                    SET status = 'Perdida'
                    WHERE id = %s
                      AND status = 'Pendiente'
                    """,
                    (db_bet_id,)
                )

                # Registramos el resultado de la apuesta.
                # amount = 0 porque no hay devolución.
                cur.execute(
                    """
                    SELECT balance
                    FROM users
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (user_id,)
                )

                user_row = cur.fetchone()

                if not user_row:
                    raise Exception("Usuario no encontrado.")

                balance_before = user_row[0]
                balance_after = balance_before

                cur.execute(
                    """
                    INSERT INTO transactions
                    (
                        user_id,
                        type,
                        amount,
                        balance_before,
                        balance_after
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        "bet_loss",
                        0,
                        balance_before,
                        balance_after
                    )
                )

                conn.commit()

                return {
                    "success": True,
                    "status": "Perdida",
                    "balance": balance_after,
                    "potential_return": 0,
                    "home_team": home_team,
                    "away_team": away_team,
                    "scores": scores
                }

            # -------------------------------------------------
            # APUESTA GANADA
            # -------------------------------------------------
            if evaluation == "Ganada":

                # Bloquear usuario antes de modificar saldo
                cur.execute(
                    """
                    SELECT balance
                    FROM users
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (user_id,)
                )

                user_row = cur.fetchone()

                if not user_row:
                    raise Exception("Usuario no encontrado.")

                balance_before = user_row[0]

                # El retorno total incluye stake + ganancia
                payout = potential_return

                balance_after = balance_before + payout

                # Actualizar saldo
                cur.execute(
                    """
                    UPDATE users
                    SET
                        balance = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        balance_after,
                        user_id
                    )
                )

                # Marcar apuesta como ganada
                cur.execute(
                    """
                    UPDATE bets
                    SET status = 'Ganada'
                    WHERE id = %s
                      AND status = 'Pendiente'
                    """,
                    (db_bet_id,)
                )

                # Registrar pago
                cur.execute(
                    """
                    INSERT INTO transactions
                    (
                        user_id,
                        type,
                        amount,
                        balance_before,
                        balance_after
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        "bet_win",
                        payout,
                        balance_before,
                        balance_after
                    )
                )

                conn.commit()

                return {
                    "success": True,
                    "status": "Ganada",
                    "balance": balance_after,
                    "potential_return": payout,
                    "home_team": home_team,
                    "away_team": away_team,
                    "scores": scores
                }

        return {
            "success": False,
            "message": "Resultado de liquidación desconocido."
        }

    except Exception as e:

        if conn:
            conn.rollback()

        print(f"ERROR LIQUIDANDO APUESTA {bet_id}: {e}")

        return {
            "success": False,
            "message": f"Error interno al liquidar la apuesta: {e}"
        }

    finally:

        if conn:
            conn.close()

async def test_settle(update, context):
    """
    Comando temporal para probar la liquidación de una apuesta.

    Uso:
    /liquidar ID_APUESTA
    """

    if not context.args:
        await update.message.reply_text(
            "Uso:\n"
            "/liquidar ID_APUESTA"
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
        f"⏳ Liquidando apuesta #{bet_id}..."
    )

    result = await settle_bet(bet_id)

    if not result["success"]:
        await update.message.reply_text(
            f"❌ NO SE LIQUIDÓ\n\n"
            f"{result['message']}"
        )
        return

    home_team = result["home_team"]
    away_team = result["away_team"]
    scores = result["scores"]

    score_text = ""

    for score in scores:
        score_text += (
            f"• {score.get('name')}: "
            f"{score.get('score')}\n"
        )

    status = result["status"]

    if status == "Ganada":
        await update.message.reply_text(
            f"✅ APUESTA LIQUIDADA\n\n"
            f"⚽ {home_team}\n"
            f"vs\n"
            f"⚽ {away_team}\n\n"
            f"📊 Marcador:\n"
            f"{score_text}\n"
            f"🏆 Resultado: Ganada\n\n"
            f"💰 Premio acreditado: "
            f"{result['potential_return']}\n"
            f"💳 Nuevo saldo: "
            f"{result['balance']}"
        )

    else:
        await update.message.reply_text(
            f"🔴 APUESTA LIQUIDADA\n\n"
            f"⚽ {home_team}\n"
            f"vs\n"
            f"⚽ {away_team}\n\n"
            f"📊 Marcador:\n"
            f"{score_text}\n"
            f"🏆 Resultado: Perdida\n\n"
            f"💳 Saldo: "
            f"{result['balance']}"
        )


# ==========================================================
# COMANDO TEMPORAL - BUSCAR PARTIDOS TERMINADOS
# ==========================================================

async def test_recent_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if len(context.args) != 1:
        await update.message.reply_text(
            "Uso correcto:\n\n"
            "/resultados SPORT_KEY\n\n"
            "Ejemplo:\n"
            "/resultados soccer_epl"
        )
        return

    sport_key = context.args[0]

    try:
        games = get_recent_completed_events(
            sport_key
        )

        if not games:
            await update.message.reply_text(
                "⏳ No encontré partidos terminados "
                "recientemente para este deporte."
            )
            return

        text = "✅ PARTIDOS TERMINADOS\n\n"

        for game in games[:5]:

            home = game.get(
                "home_team",
                "N/A"
            )

            away = game.get(
                "away_team",
                "N/A"
            )

            event_id = game.get(
                "id",
                "N/A"
            )

            scores = game.get(
                "scores"
            ) or []

            text += (
                f"🏟 {home}\n"
                f"vs\n"
                f"🏟 {away}\n\n"
                f"🆔 Event ID:\n"
                f"{event_id}\n\n"
                "📊 Marcador:\n"
            )

            if scores:

                for score in scores:
                    text += (
                        f"• {score.get('name')}: "
                        f"{score.get('score')}\n"
                    )

            else:
                text += "• No disponible\n"

            text += (
                "\n━━━━━━━━━━━━━━\n\n"
            )

        await update.message.reply_text(
            text
        )

    except Exception as e:
        print(
            "ERROR RECENT RESULTS:",
            e
        )

        await update.message.reply_text(
            "⚠️ Error consultando partidos terminados.\n\n"
            f"{e}"
        )


# ==========================================================
# COMANDO TEMPORAL - CONSULTAR RESULTADO
# ==========================================================

async def test_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if len(context.args) != 2:
        await update.message.reply_text(
            "Uso correcto:\n\n"
            "/resultado SPORT_KEY EVENT_ID\n\n"
            "Ejemplo:\n"
            "/resultado soccer_epl EVENT_ID"
        )
        return

    sport_key = context.args[0]
    event_id = context.args[1]

    try:
        result = get_event_result(
            sport_key,
            event_id
        )

        if result is None:
            await update.message.reply_text(
                "⏳ No hay resultado final disponible "
                "para este evento.\n\n"
                "Puede que el partido todavía no haya terminado "
                "o que el evento no esté disponible en el historial."
            )
            return

        text = (
            "✅ RESULTADO ENCONTRADO\n\n"
            f"🏟 {result['home_team']}\n"
            f"vs\n"
            f"🏟 {result['away_team']}\n\n"
            f"Event ID:\n"
            f"{result['event_id']}\n\n"
            f"Completed: {result['completed']}\n\n"
            "📊 Marcador:\n"
        )

        for score in result["scores"]:
            text += (
                f"• {score.get('name')}: "
                f"{score.get('score')}\n"
            )

        await update.message.reply_text(
            text
        )

    except Exception as e:
        print(
            "ERROR TEST RESULT:",
            e
        )

        await update.message.reply_text(
            "⚠️ Error consultando el resultado.\n\n"
            f"{e}"
        )


# ==========================================================
# COMANDO TEMPORAL - EVALUAR APUESTA
# ==========================================================

async def test_evaluate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if len(context.args) < 3:
        await update.message.reply_text(
            "Uso correcto:\n\n"
            "/evaluar SPORT_KEY EVENT_ID SELECCION\n\n"
            "Ejemplo:\n"
            "/evaluar soccer_germany_bundesliga EVENT_ID Bayer Leverkusen"
        )
        return

    sport_key = context.args[0]
    event_id = context.args[1]

    selection = " ".join(
        context.args[2:]
    )

    try:
        result = get_event_result(
            sport_key,
            event_id
        )

        if result is None:
            await update.message.reply_text(
                "⏳ No hay resultado final disponible "
                "para este evento."
            )
            return

        game_result = determine_h2h_result(
            result["home_team"],
            result["away_team"],
            result["scores"]
        )

        bet_result = evaluate_h2h_selection(
            selection,
            result["home_team"],
            result["away_team"],
            result["scores"]
        )

        if game_result == "local":
            winner_text = result["home_team"]

        elif game_result == "visitante":
            winner_text = result["away_team"]

        elif game_result == "empate":
            winner_text = "EMPATE"

        else:
            winner_text = "No determinado"

        text = (
            "🧪 EVALUACIÓN DE APUESTA\n\n"
            f"⚽ {result['home_team']}\n"
            f"vs\n"
            f"⚽ {result['away_team']}\n\n"
            "📊 Marcador:\n"
        )

        for score in result["scores"]:
            text += (
                f"• {score.get('name')}: "
                f"{score.get('score')}\n"
            )

        text += (
            "\n🏆 Resultado del partido:\n"
            f"{winner_text}\n\n"
            "🎯 Selección evaluada:\n"
            f"{selection}\n\n"
            "📌 Resultado de la apuesta:\n"
            f"{bet_result}\n\n"
            "ℹ️ Esta prueba NO modifica saldo, "
            "apuestas ni transacciones."
        )

        await update.message.reply_text(
            text
        )

    except Exception as e:
        print(
            "ERROR TEST EVALUATE:",
            e
        )

        await update.message.reply_text(
            "⚠️ Error evaluando la apuesta.\n\n"
            f"{e}"
        )


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    telegram_user = update.effective_user

    try:
        user = get_or_create_user(
            telegram_user
        )

        balance = user["balance"]

    except Exception as e:
        print(
            "ERROR USER:",
            e
        )

        await update.message.reply_text(
            "⚠️ No pude acceder a tu cuenta.\n\n"
            "Inténtalo nuevamente."
        )

        return

    keyboard = [
        [
            InlineKeyboardButton(
                "⚽ Fútbol",
                callback_data="football"
            ),
            InlineKeyboardButton(
                "⚾ Béisbol",
                callback_data="baseball"
            ),
        ],
        [
            InlineKeyboardButton(
                "💰 Mi saldo",
                callback_data="balance"
            ),
            InlineKeyboardButton(
                "🎯 Mis apuestas",
                callback_data="bets"
            ),
        ],
    ]

    await update.message.reply_text(
        "👋 ¡Bienvenido!\n\n"
        "🏆 Sports Bot\n\n"
        f"💵 Saldo virtual: "
        f"{balance:,.0f} créditos\n\n"
        "Selecciona una opción:",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


async def show_sports(query):
    try:
        sports = get_sports()

        football = [
            s for s in sports
            if s.get("group") == "Soccer"
            and s.get("active")
        ]

        keyboard = []

        for sport in football[:20]:
            keyboard.append([
                InlineKeyboardButton(
                    sport["title"],
                    callback_data=(
                        f"sport:{sport['key']}"
                    )
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                "⬅️ Volver",
                callback_data="home"
            )
        ])

        if not football:
            text = (
                "⚽ No hay ligas de fútbol "
                "disponibles en este momento."
            )

        else:
            text = (
                "⚽ FÚTBOL\n\n"
                "Selecciona una competición:"
            )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                keyboard
            ),
        )

    except Exception as e:
        print(
            "ERROR SPORTS:",
            e
        )

        await query.edit_message_text(
            "⚠️ No pude obtener las competiciones.\n\n"
            "Inténtalo nuevamente."
        )


async def show_games(
    query,
    sport_key
):
    try:
        games = get_odds(
            sport_key
        )

        if not games:
            await query.edit_message_text(
                "⚽ No hay partidos disponibles "
                "para esta competición."
            )
            return

        keyboard = []

        for game in games[:15]:

            home = game.get(
                "home_team",
                "Local"
            )

            away = game.get(
                "away_team",
                "Visitante"
            )

            keyboard.append([
                InlineKeyboardButton(
                    f"⚽ {home} vs {away}",
                    callback_data=(
                        f"game:{sport_key}:{game['id']}"
                    )
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                "⬅️ Competiciones",
                callback_data="football"
            )
        ])

        await query.edit_message_text(
            "⚽ PARTIDOS\n\n"
            f"Competición: {sport_key}\n\n"
            "Selecciona un partido:",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            ),
        )

    except Exception as e:
        print(
            "ERROR ODDS:",
            e
        )

        await query.edit_message_text(
            "⚠️ No pude obtener los partidos."
        )


async def show_game(
    query,
    sport_key,
    event_id
):
    try:
        games = get_odds(
            sport_key
        )

        game = next(
            (
                g for g in games
                if g["id"] == event_id
            ),
            None
        )

        if not game:
            await query.edit_message_text(
                "⚠️ Este partido ya no está disponible."
            )
            return

        home = game["home_team"]
        away = game["away_team"]

        unique = {}

        for bookmaker in game.get(
            "bookmakers",
            []
        ):

            for market in bookmaker.get(
                "markets",
                []
            ):

                if market["key"] == "h2h":

                    for outcome in market.get(
                        "outcomes",
                        []
                    ):

                        name = outcome["name"]
                        price = outcome["price"]

                        if name not in unique:
                            unique[name] = price

        keyboard = []

        for name, price in unique.items():

            user_id = query.from_user.id

            pick_id = (
                f"{user_id}_"
                f"{event_id}_"
                f"{len(pending_bets)}"
            )

            pending_bets[pick_id] = {
                "sport_key": sport_key,
                "event_id": event_id,
                "home": home,
                "away": away,
                "selection": name,
                "odds": price,
                "commence_time": game.get(
                    "commence_time"
                ),
            }

            keyboard.append([
                InlineKeyboardButton(
                    f"{name} — {price:.2f}",
                    callback_data=(
                        f"pick:{pick_id}"
                    )
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                "⬅️ Partidos",
                callback_data=(
                    f"sport:{sport_key}"
                )
            )
        ])

        text = (
            f"⚽ {home}\n"
            f"vs\n"
            f"⚽ {away}\n\n"
            "📊 CUOTAS\n\n"
        )

        for name, price in unique.items():
            text += (
                f"• {name}: {price:.2f}\n"
            )

        text += (
            "\n🎯 Selecciona tu apuesta:"
        )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                keyboard
            ),
        )

    except Exception as e:
        print(
            "ERROR GAME:",
            e
        )

        await query.edit_message_text(
            "⚠️ No pude obtener las cuotas."
        )


async def ask_amount(
    query,
    pick_id
):
    user_id = query.from_user.id

    pick = pending_bets.get(
        pick_id
    )

    if not pick:
        await query.edit_message_text(
            "⚠️ Esta selección ya no está disponible."
        )
        return

    try:
        balance = get_user_balance(
            user_id
        )

        if balance is None:
            await query.edit_message_text(
                "⚠️ No encontré tu cuenta."
            )
            return

    except Exception as e:
        print(
            "ERROR BALANCE:",
            e
        )

        await query.edit_message_text(
            "⚠️ No pude consultar tu saldo."
        )

        return

    pending_bets[
        f"active:{user_id}"
    ] = pick

    await query.edit_message_text(
        f"🎯 SELECCIÓN\n\n"
        f"⚽ {pick['home']} vs "
        f"{pick['away']}\n\n"
        f"Tu selección: "
        f"{pick['selection']}\n"
        f"📈 Cuota: "
        f"{pick['odds']:.2f}\n\n"
        f"💰 Saldo disponible: "
        f"{balance:,.0f} créditos\n\n"
        "💵 Escribe ahora el monto "
        "que deseas apostar.\n\n"
        "Ejemplo: 200"
    )


async def handle_amount(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    active_key = (
        f"active:{user_id}"
    )

    if active_key not in pending_bets:
        return

    text = update.message.text.strip()

    try:
        amount = int(text)

    except ValueError:
        await update.message.reply_text(
            "⚠️ Introduce solamente un número.\n\n"
            "Ejemplo: 200"
        )
        return

    try:
        balance = get_user_balance(
            user_id
        )

        if balance is None:
            await update.message.reply_text(
                "⚠️ No encontré tu cuenta."
            )
            return

    except Exception as e:
        print(
            "ERROR BALANCE:",
            e
        )

        await update.message.reply_text(
            "⚠️ No pude consultar tu saldo."
        )

        return

    if amount <= 0:
        await update.message.reply_text(
            "⚠️ El monto debe ser mayor que 0."
        )
        return

    if amount > balance:
        await update.message.reply_text(
            f"⚠️ No tienes suficientes créditos.\n\n"
            f"Saldo disponible: "
            f"{balance:,.0f} créditos."
        )
        return

    pick = pending_bets[
        active_key
    ]

    potential_return = (
        amount * pick["odds"]
    )

    pick["amount"] = amount
    pick["potential_return"] = (
        potential_return
    )

    # ==========================================================
    # GUARDAR APUESTA SIN CONFIRMAR EN NEON
    # ==========================================================

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:

                cursor.execute("""
                    SELECT id
                    FROM users
                    WHERE telegram_id = %s
                """, (user_id,))

                user = cursor.fetchone()

                if not user:
                    raise ValueError(
                        "Usuario no encontrado"
                    )

                db_user_id = user[0]

                cursor.execute("""
                    DELETE FROM unconfirmed_bets
                    WHERE user_id = %s
                """, (db_user_id,))

                event_name = (
                    f"{pick['home']} vs "
                    f"{pick['away']}"
                )

                cursor.execute("""
                    INSERT INTO unconfirmed_bets (
                        user_id,
                        event_id,
                        event_name,
                        sport,
                        competition,
                        selection,
                        odds,
                        stake,
                        potential_return
                    )
                    VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s
                    )
                    RETURNING id
                """, (
                    db_user_id,
                    pick["event_id"],
                    event_name,
                    "football",
                    pick.get("sport_key"),
                    pick["selection"],
                    pick["odds"],
                    amount,
                    potential_return,
                ))

                unconfirmed_id = (
                    cursor.fetchone()[0]
                )

        pick["unconfirmed_id"] = (
            unconfirmed_id
        )

        print(
            "✅ APUESTA SIN CONFIRMAR "
            "GUARDADA EN NEON: "
            f"id={unconfirmed_id}, "
            f"user_id={db_user_id}"
        )

    except Exception as e:
        print(
            "ERROR SAVE UNCONFIRMED:",
            e
        )

        await update.message.reply_text(
            "⚠️ No se pudo guardar "
            "la apuesta sin confirmar.\n\n"
            "Inténtalo nuevamente."
        )

        return

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Confirmar",
                callback_data=(
                    f"confirm:{user_id}"
                )
            ),
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data=(
                    f"cancel:{user_id}"
                )
            ),
        ]
    ]

    await update.message.reply_text(
        "🎯 CONFIRMAR APUESTA\n\n"
        f"⚽ {pick['home']} vs "
        f"{pick['away']}\n\n"
        f"🎯 Selección: "
        f"{pick['selection']}\n"
        f"📈 Cuota: "
        f"{pick['odds']:.2f}\n"
        f"💵 Apuesta: "
        f"{amount:,} créditos\n"
        f"💰 Posible retorno: "
        f"{potential_return:,.0f} créditos\n\n"
        "¿Confirmar apuesta?",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


async def confirm_bet(query):
    user_id = query.from_user.id

    active_key = (
        f"active:{user_id}"
    )

    pick = pending_bets.get(
        active_key
    )

    if not pick:
        await query.edit_message_text(
            "⚠️ No hay una apuesta sin confirmar."
        )
        return

    amount = pick["amount"]

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:

                cursor.execute("""
                    SELECT id, balance
                    FROM users
                    WHERE telegram_id = %s
                    FOR UPDATE
                """, (user_id,))

                user = cursor.fetchone()

                if not user:
                    raise ValueError(
                        "Usuario no encontrado"
                    )

                db_user_id = user[0]
                balance = user[1]

                if amount > balance:
                    await query.edit_message_text(
                        "⚠️ Ya no tienes saldo suficiente.\n\n"
                        f"Saldo disponible: "
                        f"{float(balance):,.0f} créditos."
                    )
                    return

                new_balance = (
                    balance - amount
                )

                event_name = (
                    f"{pick['home']} vs "
                    f"{pick['away']}"
                )

                cursor.execute("""
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
                        status
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                    RETURNING id
                """, (
                    db_user_id,
                    "football",
                    pick.get("sport_key"),
                    pick["event_id"],
                    event_name,
                    pick.get("commence_time"),
                    pick["selection"],
                    pick["odds"],
                    amount,
                    pick["potential_return"],
                    "Pendiente",
                ))

                bet_id = cursor.fetchone()[0]

                cursor.execute("""
                    UPDATE users
                    SET balance = %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (
                    new_balance,
                    db_user_id
                ))

                cursor.execute("""
                    INSERT INTO transactions (
                        user_id,
                        type,
                        amount,
                        balance_before,
                        balance_after
                    )
                    VALUES (
                        %s, %s, %s, %s, %s
                    )
                """, (
                    db_user_id,
                    "bet",
                    -amount,
                    balance,
                    new_balance,
                ))

                cursor.execute("""
                    DELETE FROM unconfirmed_bets
                    WHERE id = %s
                    AND user_id = %s
                """, (
                    pick.get("unconfirmed_id"),
                    db_user_id
                ))

        del pending_bets[
            active_key
        ]

        await query.edit_message_text(
            "✅ APUESTA REGISTRADA\n\n"
            f"⚽ {pick['home']} vs "
            f"{pick['away']}\n\n"
            f"🎯 Selección: "
            f"{pick['selection']}\n"
            f"📈 Cuota: "
            f"{pick['odds']:.2f}\n"
            f"💵 Apuesta: "
            f"{amount:,} créditos\n"
            f"💰 Posible retorno: "
            f"{pick['potential_return']:,.0f} créditos\n\n"
            f"💳 Nuevo saldo: "
            f"{float(new_balance):,.0f} créditos\n\n"
            "🎯 La apuesta queda registrada."
        )

        print(
            f"✅ APUESTA GUARDADA EN NEON: "
            f"bet_id={bet_id}, "
            f"user_id={db_user_id}"
        )

    except Exception as e:
        print(
            "ERROR CONFIRM BET:",
            e
        )

        await query.edit_message_text(
            "⚠️ No se pudo registrar "
            "la apuesta.\n\n"
            "No se descontaron créditos."
        )


async def cancel_bet(query):
    user_id = query.from_user.id

    active_key = (
        f"active:{user_id}"
    )

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:

                cursor.execute("""
                    DELETE FROM unconfirmed_bets
                    WHERE user_id = (
                        SELECT id
                        FROM users
                        WHERE telegram_id = %s
                    )
                """, (user_id,))

        if active_key in pending_bets:
            del pending_bets[
                active_key
            ]

    except Exception as e:
        print(
            "ERROR CANCEL UNCONFIRMED:",
            e
        )

        await query.edit_message_text(
            "⚠️ No se pudo cancelar correctamente "
            "la apuesta sin confirmar."
        )

        return

    await query.edit_message_text(
        "❌ APUESTA CANCELADA\n\n"
        "No se descontaron créditos.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🏠 Menú principal",
                    callback_data="home"
                )
            ]
        ])
    )


async def show_bets(query):
    user_id = query.from_user.id

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT
                        event_name,
                        selection,
                        odds,
                        stake,
                        status,
                        match_date
                    FROM bets
                    WHERE user_id = (
                        SELECT id
                        FROM users
                        WHERE telegram_id = %s
                    )
                    ORDER BY id DESC
                    """,
                    (user_id,)
                )

                user_bets = cursor.fetchall()

        if not user_bets:
            await query.edit_message_text(
                "🎯 MIS APUESTAS\n\n"
                "Todavía no tienes "
                "apuestas registradas.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Volver",
                            callback_data="home"
                        )
                    ]
                ])
            )
            return

        text = (
            "🎯 MIS APUESTAS\n\n"
        )

        for i, bet in enumerate(
            user_bets,
            1
        ):

            (
                event_name,
                selection,
                odds,
                stake,
                status,
                match_date
            ) = bet

            if match_date:

                if isinstance(
                    match_date,
                    str
                ):

                    try:
                        match_date = (
                            datetime.fromisoformat(
                                match_date.replace(
                                    "Z",
                                    "+00:00"
                                )
                            )
                        )

                    except Exception:
                        pass

                if isinstance(
                    match_date,
                    datetime
                ):

                    match_date_text = (
                        match_date.strftime(
                            "%d/%m/%Y %H:%M UTC"
                        )
                    )

                else:
                    match_date_text = (
                        str(match_date)
                    )

            else:
                match_date_text = (
                    "No disponible"
                )

            text += (
                f"#{i}\n"
                f"⚽ {event_name}\n"
                f"🎯 {selection}\n"
                f"📅 Partido: "
                f"{match_date_text}\n"
                f"📈 Cuota: "
                f"{odds:.2f}\n"
                f"💵 Apuesta: "
                f"{stake:,}\n"
                f"📌 Estado: "
                f"{status}\n\n"
            )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Volver",
                        callback_data="home"
                    )
                ]
            ])
        )

    except Exception as e:
        print(
            "❌ ERROR MIS APUESTAS:",
            e
        )

        await query.edit_message_text(
            "❌ No se pudieron cargar "
            "tus apuestas.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Volver",
                        callback_data="home"
                    )
                ]
            ])
        )


def home_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⚽ Fútbol",
                callback_data="football"
            ),
            InlineKeyboardButton(
                "⚾ Béisbol",
                callback_data="baseball"
            ),
        ],
        [
            InlineKeyboardButton(
                "💰 Mi saldo",
                callback_data="balance"
            ),
            InlineKeyboardButton(
                "🎯 Mis apuestas",
                callback_data="bets"
            ),
        ],
    ])


async def button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    data = query.data

    if data == "football":

        await show_sports(
            query
        )

    elif data == "baseball":

        await query.edit_message_text(
            "⚾ BÉISBOL\n\n"
            "Lo agregaremos más adelante."
        )

    elif data == "balance":

        user_id = query.from_user.id

        try:
            balance = get_user_balance(
                user_id
            )

            if balance is None:
                await query.edit_message_text(
                    "⚠️ No encontré tu cuenta."
                )
                return

        except Exception as e:

            print(
                "ERROR BALANCE:",
                e
            )

            await query.edit_message_text(
                "⚠️ No pude consultar tu saldo."
            )

            return

        await query.edit_message_text(
            "💰 MI SALDO\n\n"
            f"Créditos disponibles: "
            f"{balance:,.0f}\n\n"
            "Saldo completamente virtual.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Volver",
                        callback_data="home"
                    )
                ]
            ])
        )

    elif data == "bets":

        await show_bets(
            query
        )

    elif data == "home":

        user_id = query.from_user.id

        try:
            balance = get_user_balance(
                user_id
            )

            if balance is None:
                balance = 1000

        except Exception as e:

            print(
                "ERROR BALANCE:",
                e
            )

            balance = 1000

        await query.edit_message_text(
            "🏆 SPORTS BOT\n\n"
            f"💰 Saldo: "
            f"{balance:,.0f} créditos\n\n"
            "Selecciona una opción:",
            reply_markup=home_keyboard(),
        )

    elif data.startswith("sport:"):

        sport_key = data.split(
            ":",
            1
        )[1]

        await show_games(
            query,
            sport_key
        )

    elif data.startswith("game:"):

        _, sport_key, event_id = data.split(
            ":",
            2
        )

        await show_game(
            query,
            sport_key,
            event_id
        )

    elif data.startswith("pick:"):

        pick_id = data.split(
            ":",
            1
        )[1]

        await ask_amount(
            query,
            pick_id
        )

    elif data.startswith("confirm:"):

        await confirm_bet(
            query
        )

    elif data.startswith("cancel:"):

        await cancel_bet(
            query
        )


def main():

    if not BOT_TOKEN:
        raise ValueError(
            "Falta BOT_TOKEN"
        )

    if not ODDS_API_KEY:
        raise ValueError(
            "Falta ODDS_API_KEY"
        )

    if not DATABASE_URL:
        raise ValueError(
            "Falta DATABASE_URL"
        )

    test_database_connection()

    create_database_tables()

    app = Application.builder().token(
        BOT_TOKEN
    ).build()

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # ==========================================================
    # COMANDO TEMPORAL - BUSCAR PARTIDOS TERMINADOS
    # ==========================================================

    app.add_handler(
        CommandHandler(
            "resultados",
            test_recent_results
        )
    )
    
    app.add_handler(
        CommandHandler(
            "liquidar",
            test_settle
        )
    )

    # ==========================================================
    # COMANDO TEMPORAL - CONSULTAR RESULTADO
    # ==========================================================

    app.add_handler(
        CommandHandler(
            "resultado",
            test_result
        )
    )

    # ==========================================================
    # COMANDO TEMPORAL - EVALUAR APUESTA
    # ==========================================================

    app.add_handler(
        CommandHandler(
            "evaluar",
            test_evaluate
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_amount
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            button
        )
    )

    print(
        "Bot iniciado correctamente..."
    )

    app.run_polling()


if __name__ == "__main__":
    main()
