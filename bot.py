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
# BASE DE DATOS
# ============================================================

def get_db_connection():
    return psycopg.connect(DATABASE_URL)


def init_db():
    with get_db_connection() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    balance NUMERIC DEFAULT 1000,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
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
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    type TEXT,
                    amount NUMERIC,
                    balance_before NUMERIC,
                    balance_after NUMERIC,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
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
            """)

            cur.execute("""
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
            """)


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
                    (telegram_user.username, telegram_user.id),
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
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"

    response = requests.get(
        url,
        params={
            "apiKey": ODDS_API_KEY,
            "regions": "us",
            "markets": "h2h",
            "oddsFormat": "decimal",
        },
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# RESULTADOS
# ============================================================

def get_event_result(sport_key, event_id):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/"

    response = requests.get(
        url,
        params={
            "apiKey": ODDS_API_KEY,
            "daysFrom": 3,
            "eventIds": event_id,
        },
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    for event in data:
        if event.get("id") == event_id:
            return event

    return None


def get_recent_completed_events(sport_key):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/"

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
            home_score = int(score.get("score"))
        elif score.get("name") == away_team:
            away_score = int(score.get("score"))

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
# LIQUIDACIÓN ATÓMICA
# ============================================================

def settle_bet(bet_id):

    with psycopg.connect(DATABASE_URL) as conn:

        try:

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
                        status
                    FROM bets
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (bet_id,),
                )

                bet = cur.fetchone()

                if not bet:
                    return {
                        "success": False,
                        "message": "La apuesta no existe.",
                    }

                (
                    bet_id,
                    user_id,
                    sport,
                    competition,
                    event_id,
                    event_name,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    status,
                ) = bet

                if status != "Pendiente":
                    return {
                        "success": False,
                        "already_processed": True,
                        "status": status,
                        "message": (
                            f"La apuesta ya fue procesada. "
                            f"Estado actual: {status}."
                        ),
                    }

                sport_key = competition

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

                result = determine_h2h_result(event)

                if result is None:
                    return {
                        "success": False,
                        "message": (
                            "No fue posible determinar el resultado "
                            "final del partido."
                        ),
                    }

                won = result == selection

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
                    conn.rollback()

                    return {
                        "success": False,
                        "message": "Usuario no encontrado.",
                    }

                current_balance = user_row[0]

                if won:

                    new_balance = current_balance + potential_return

                    cur.execute(
                        """
                        UPDATE users
                        SET balance = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                        """,
                        (
                            new_balance,
                            user_id,
                        ),
                    )

                    cur.execute(
                        """
                        UPDATE bets
                        SET status = 'Ganada'
                        WHERE id = %s
                        """,
                        (bet_id,),
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
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            user_id,
                            "bet_win",
                            potential_return,
                            current_balance,
                            new_balance,
                        ),
                    )

                    conn.commit()

                    return {
                        "success": True,
                        "won": True,
                        "status": "Ganada",
                        "event": event,
                        "result": result,
                        "balance": new_balance,
                        "potential_return": potential_return,
                    }

                else:

                    new_balance = current_balance

                    cur.execute(
                        """
                        UPDATE bets
                        SET status = 'Perdida'
                        WHERE id = %s
                        """,
                        (bet_id,),
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
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            user_id,
                            "bet_loss",
                            0,
                            current_balance,
                            new_balance,
                        ),
                    )

                    conn.commit()

                    return {
                        "success": True,
                        "won": False,
                        "status": "Perdida",
                        "event": event,
                        "result": result,
                        "balance": new_balance,
                        "potential_return": potential_return,
                    }

        except Exception:

            conn.rollback()

            raise


# ============================================================
# COMANDO DE PRUEBA /LIQUIDAR
# SOLO ADMIN
# ============================================================

async def test_settle(update: Update, context: ContextTypes.DEFAULT_TYPE):

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
        f"⏳ Liquidando apuesta #{bet_id}..."
    )

    result = settle_bet(bet_id)

    if not result["success"]:

        await update.message.reply_text(
            result["message"]
        )

        return

    event = result["event"]

    home_team = event.get("home_team", "Local")
    away_team = event.get("away_team", "Visitante")

    scores = event.get("scores", [])

    home_score = "?"
    away_score = "?"

    for score in scores:

        if score.get("name") == home_team:
            home_score = score.get("score")

        elif score.get("name") == away_team:
            away_score = score.get("score")

    if result["won"]:

        message = (
            "✅ APUESTA LIQUIDADA\n\n"
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
        events = get_recent_completed_events(sport_key)

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
                score_map[score.get("name")] = score.get("score")

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

    if user_id is not None and is_admin(user_id):

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
        reply_markup=home_keyboard(telegram_user.id),
    )


# ============================================================
# DEPORTES
# ============================================================

async def show_sports(query):

    keyboard = [
        [
            InlineKeyboardButton(
                "🇪🇸 LaLiga",
                callback_data="league:soccer_spain_la_liga",
            )
        ],
        [
            InlineKeyboardButton(
                "🏴 Premier League",
                callback_data="league:soccer_epl",
            )
        ],
        [
            InlineKeyboardButton(
                "🇮🇹 Serie A",
                callback_data="league:soccer_italy_serie_a",
            )
        ],
        [
            InlineKeyboardButton(
                "🇩🇪 Bundesliga",
                callback_data="league:soccer_germany_bundesliga",
            )
        ],
        [
            InlineKeyboardButton(
                "🇫🇷 Ligue 1",
                callback_data="league:soccer_france_ligue_one",
            )
        ],
        [
            InlineKeyboardButton(
                "🌍 UEFA Nations League",
                callback_data="league:soccer_uefa_nations_league",
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
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# MOSTRAR PARTIDOS
# ============================================================

async def show_games(query, sport_key):

    try:

        events = get_odds(sport_key)

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

        home = event.get("home_team", "")
        away = event.get("away_team", "")

        keyboard.append(
            [
                InlineKeyboardButton(
                    f"⚽ {home} vs {away}",
                    callback_data=f"game:{sport_key}:{event_id}",
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
        reply_markup=InlineKeyboardMarkup(keyboard),
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

        events = get_odds(sport_key)

    except Exception as e:

        await query.edit_message_text(
            f"❌ Error:\n{e}"
        )

        return

    event = None

    for item in events:

        if item.get("id") == event_id:

            event = item
            break

    if not event:

        await query.edit_message_text(
            "❌ Partido no encontrado."
        )

        return

    home = event.get("home_team")
    away = event.get("away_team")

    outcomes = {}

    for bookmaker in event.get("bookmakers", []):

        for market in bookmaker.get("markets", []):

            if market.get("key") != "h2h":
                continue

            for outcome in market.get("outcomes", []):

                name = outcome.get("name")
                price = outcome.get("price")

                if name not in outcomes:

                    outcomes[name] = price

    keyboard = []

    for name, price in outcomes.items():

        pick_id = (
            f"pick:{sport_key}:"
            f"{event_id}:"
            f"{name}"
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
                    f"{name} — {price}",
                    callback_data=pick_id,
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "⬅️ Volver",
                callback_data=f"league:{sport_key}",
            )
        ]
    )

    await query.edit_message_text(
        f"⚽ {home}\n"
        f"vs\n"
        f"⚽ {away}\n\n"
        "Selecciona tu apuesta:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# PEDIR MONTO
# ============================================================

async def ask_amount(query, pick_id):

    user_id = query.from_user.id

    pick = pending_bets.get(pick_id)

    if not pick:

        await query.edit_message_text(
            "❌ Esta selección ya no está disponible."
        )

        return

    balance = get_user_balance(user_id)

    pending_bets[f"active:{user_id}"] = pick

    await query.edit_message_text(
        f"⚽ {pick['home']}\n"
        f"vs\n"
        f"⚽ {pick['away']}\n\n"
        f"🎯 Selección: {pick['selection']}\n"
        f"📈 Cuota: {pick['odds']}\n\n"
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

    balance = get_user_balance(user_id)

    if stake > float(balance):

        await update.message.reply_text(
            f"❌ Saldo insuficiente.\n\n"
            f"💰 Tu saldo: {balance}"
        )

        return

    potential_return = stake * float(active["odds"])

    event_name = (
        f"{active['home']} vs {active['away']}"
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
                    active["selection"],
                    active["odds"],
                    stake,
                    potential_return,
                ),
            )

            unconfirmed_id = cur.fetchone()[0]

    pending_bets[f"unconfirmed:{user_id}"] = {
        "id": unconfirmed_id,
        **active,
        "stake": stake,
        "potential_return": potential_return,
    }

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Confirmar apuesta",
                callback_data=f"confirm:{unconfirmed_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data=f"cancel:{unconfirmed_id}",
            )
        ],
    ]

    await update.message.reply_text(
        "🎟 CONFIRMAR APUESTA\n\n"
        f"⚽ {active['home']}\n"
        f"vs\n"
        f"⚽ {active['away']}\n\n"
        f"🎯 Selección: {active['selection']}\n"
        f"📈 Cuota: {active['odds']}\n"
        f"💰 Apuesta: {stake}\n"
        f"🏆 Posible retorno: {potential_return:.2f}\n\n"
        "¿Deseas confirmar?",
        reply_markup=InlineKeyboardMarkup(keyboard),
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
            balance_after = balance - stake

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
                    status
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    NULL,
                    %s,
                    %s,
                    %s,
                    %s,
                    'Pendiente'
                )
                RETURNING id
                """,
                (
                    db_user_id,
                    sport,
                    competition,
                    event_id,
                    event_name,
                    selection,
                    odds,
                    stake,
                    potential_return,
                ),
            )

            bet_id = cur.fetchone()[0]

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
        f"🎟 Apuesta #{bet_id}\n\n"
        f"⚽ {event_name}\n\n"
        f"🎯 Selección: {selection}\n"
        f"📈 Cuota: {odds}\n"
        f"💰 Apostado: {stake}\n"
        f"🏆 Posible retorno: {potential_return:.2f}\n\n"
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
                    b.id,
                    b.event_name,
                    b.selection,
                    b.odds,
                    b.stake,
                    b.potential_return,
                    b.status
                FROM bets b
                JOIN users u
                    ON u.id = b.user_id
                WHERE u.telegram_id = %s
                ORDER BY b.id DESC
                LIMIT 20
                """,
                (user_id,),
            )

            rows = cur.fetchall()

    if not rows:

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
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        return

    text = "🎟 MIS APUESTAS\n\n"

    for row in rows:

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
        elif status == "Ganada":
            icon = "✅"
        else:
            icon = "❌"

        text += (
            f"{icon} Apuesta #{bet_id}\n"
            f"⚽ {event_name}\n"
            f"🎯 {selection}\n"
            f"📈 Cuota: {odds}\n"
            f"💰 Monto: {stake}\n"
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
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# PANEL DE ADMINISTRACIÓN
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
                    b.id,
                    u.username,
                    u.telegram_id,
                    b.event_name,
                    b.selection,
                    b.odds,
                    b.stake,
                    b.potential_return,
                    b.status
                FROM bets b
                JOIN users u
                    ON u.id = b.user_id
                WHERE b.status = 'Pendiente'
                ORDER BY b.id ASC
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
            "✅ No hay apuestas pendientes de liquidación.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        return

    keyboard = []

    for row in rows:

        (
            bet_id,
            username,
            telegram_id,
            event_name,
            selection,
            odds,
            stake,
            potential_return,
            status,
        ) = row

        display_user = (
            f"@{username}"
            if username
            else str(telegram_id)
        )

        keyboard.append(
            [
                InlineKeyboardButton(
                    f"#{bet_id} • {display_user} • {stake}",
                    callback_data=f"adminbet:{bet_id}",
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
        f"🎟 Apuestas pendientes: {len(rows)}\n\n"
        "Selecciona una apuesta para ver sus detalles:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# DETALLE DE APUESTA PARA ADMIN
# ============================================================

async def show_admin_bet(query, bet_id):

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
                    b.id,
                    u.username,
                    u.telegram_id,
                    b.sport,
                    b.competition,
                    b.event_id,
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
                WHERE b.id = %s
                """,
                (bet_id,),
            )

            row = cur.fetchone()

    if not row:

        await query.edit_message_text(
            "❌ La apuesta no existe.",
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

    (
        db_bet_id,
        username,
        telegram_id,
        sport,
        competition,
        event_id,
        event_name,
        selection,
        odds,
        stake,
        potential_return,
        status,
        created_at,
    ) = row

    display_user = (
        f"@{username}"
        if username
        else str(telegram_id)
    )

    text = (
        f"⚙️ APUESTA #{db_bet_id}\n\n"
        f"👤 Usuario: {display_user}\n"
        f"🆔 Telegram ID: {telegram_id}\n\n"
        f"⚽ Partido: {event_name}\n"
        f"🎯 Selección: {selection}\n"
        f"📈 Cuota: {odds}\n"
        f"💰 Apuesta: {stake}\n"
        f"🏆 Posible retorno: {potential_return}\n"
        f"📌 Estado: {status}\n"
        f"🆔 Event ID: {event_id}\n"
    )

    keyboard = []

    if status == "Pendiente":

        keyboard.append(
            [
                InlineKeyboardButton(
                    "💰 LIQUIDAR APUESTA",
                    callback_data=f"settle:{db_bet_id}",
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "⬅️ Volver a pendientes",
                callback_data="admin",
            )
        ]
    )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# LIQUIDACIÓN DESDE PANEL ADMIN
# ============================================================

async def admin_settle_bet(query, bet_id):

    if not is_admin(query.from_user.id):

        await query.answer(
            "⛔ No tienes permisos.",
            show_alert=True,
        )

        return

    await query.edit_message_text(
        f"⏳ Liquidando apuesta #{bet_id}..."
    )

    result = settle_bet(bet_id)

    if not result["success"]:

        keyboard = [
            [
                InlineKeyboardButton(
                    "🔄 Volver a intentar",
                    callback_data=f"adminbet:{bet_id}",
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
            f"⚠️ NO SE PUDO LIQUIDAR\n\n"
            f"🎟 Apuesta #{bet_id}\n\n"
            f"{result['message']}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        return

    event = result["event"]

    home_team = event.get(
        "home_team",
        "Local",
    )

    away_team = event.get(
        "away_team",
        "Visitante",
    )

    scores = event.get("scores", [])

    home_score = "?"
    away_score = "?"

    for score in scores:

        if score.get("name") == home_team:
            home_score = score.get("score")

        elif score.get("name") == away_team:
            away_score = score.get("score")

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
            f"💰 Premio pagado: {result['potential_return']}\n"
            f"💳 Nuevo saldo del usuario: {result['balance']}"
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
            f"💳 Saldo del usuario: {result['balance']}"
        )

    keyboard = [
        [
            InlineKeyboardButton(
                "⚙️ Ver apuestas pendientes",
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
        reply_markup=InlineKeyboardMarkup(keyboard),
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

    # --------------------------------------------------------
    # HOME
    # --------------------------------------------------------

    if data == "home":

        await query.edit_message_text(
            "🏆 CUBA SPORTS\n\n"
            "Selecciona una opción:",
            reply_markup=home_keyboard(
                query.from_user.id
            ),
        )

        return

    # --------------------------------------------------------
    # FÚTBOL
    # --------------------------------------------------------

    if data == "football":

        await show_sports(query)

        return

    # --------------------------------------------------------
    # BÉISBOL
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # SALDO
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # MIS APUESTAS
    # --------------------------------------------------------

    if data == "mybets":

        await show_bets(query)

        return

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------

    if data == "admin":

        await show_admin_bets(query)

        return

    # --------------------------------------------------------
    # DETALLE ADMIN
    # --------------------------------------------------------

    if data.startswith("adminbet:"):

        if not is_admin(query.from_user.id):

            await query.answer(
                "⛔ No tienes permisos.",
                show_alert=True,
            )

            return

        try:

            bet_id = int(
                data.split(":", 1)[1]
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

    # --------------------------------------------------------
    # LIQUIDAR ADMIN
    # --------------------------------------------------------

    if data.startswith("settle:"):

        if not is_admin(query.from_user.id):

            await query.answer(
                "⛔ No tienes permisos.",
                show_alert=True,
            )

            return

        try:

            bet_id = int(
                data.split(":", 1)[1]
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

    # --------------------------------------------------------
    # CONFIRMAR
    # --------------------------------------------------------

    if data.startswith("confirm:"):

        try:

            unconfirmed_id = int(
                data.split(":", 1)[1]
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

    # --------------------------------------------------------
    # CANCELAR
    # --------------------------------------------------------

    if data.startswith("cancel:"):

        try:

            unconfirmed_id = int(
                data.split(":", 1)[1]
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

    # --------------------------------------------------------
    # LIGA
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # PARTIDO
    # --------------------------------------------------------

    if data.startswith("game:"):

        parts = data.split(":", 2)

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

    # --------------------------------------------------------
    # SELECCIÓN
    # --------------------------------------------------------

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

    # Comandos
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

    # Botones
    application.add_handler(
        CallbackQueryHandler(
            button
        )
    )

    # Mensajes de texto
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_amount,
        )
    )

    print("🏆 Cuba Sports iniciado correctamente.")

    application.run_polling()


if __name__ == "__main__":
    main()
