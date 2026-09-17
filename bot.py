import os
import requests
import psycopg

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
                    selection TEXT NOT NULL,
                    odds NUMERIC(10,4) NOT NULL,
                    stake NUMERIC(18,2) NOT NULL,
                    potential_return NUMERIC(18,2) NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Pendiente',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
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


def update_user_balance(telegram_id, new_balance):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:

            cursor.execute("""
                UPDATE users
                SET balance = %s,
                    updated_at = NOW()
                WHERE telegram_id = %s
            """, (new_balance, telegram_id))

        conn.commit()


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user = update.effective_user

    try:
        user = get_or_create_user(telegram_user)
        balance = user["balance"]

    except Exception as e:
        print("ERROR USER:", e)

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
        f"💵 Saldo virtual: {balance:,.0f} créditos\n\n"
        "Selecciona una opción:",
        reply_markup=InlineKeyboardMarkup(keyboard),
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
                    callback_data=f"sport:{sport['key']}"
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
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        print("ERROR SPORTS:", e)

        await query.edit_message_text(
            "⚠️ No pude obtener las competiciones.\n\n"
            "Inténtalo nuevamente."
        )


async def show_games(query, sport_key):
    try:
        games = get_odds(sport_key)

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
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        print("ERROR ODDS:", e)

        await query.edit_message_text(
            "⚠️ No pude obtener los partidos."
        )


async def show_game(query, sport_key, event_id):
    try:
        games = get_odds(sport_key)

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
            }

            keyboard.append([
                InlineKeyboardButton(
                    f"{name} — {price:.2f}",
                    callback_data=f"pick:{pick_id}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                "⬅️ Partidos",
                callback_data=f"sport:{sport_key}"
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
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        print("ERROR GAME:", e)

        await query.edit_message_text(
            "⚠️ No pude obtener las cuotas."
        )


async def ask_amount(query, pick_id):
    user_id = query.from_user.id

    pick = pending_bets.get(pick_id)

    if not pick:
        await query.edit_message_text(
            "⚠️ Esta selección ya no está disponible."
        )
        return

    try:
        balance = get_user_balance(user_id)

        if balance is None:
            await query.edit_message_text(
                "⚠️ No encontré tu cuenta."
            )
            return

    except Exception as e:
        print("ERROR BALANCE:", e)

        await query.edit_message_text(
            "⚠️ No pude consultar tu saldo."
        )
        return

    pending_bets[f"active:{user_id}"] = pick

    await query.edit_message_text(
        f"🎯 SELECCIÓN\n\n"
        f"⚽ {pick['home']} vs {pick['away']}\n\n"
        f"Tu selección: {pick['selection']}\n"
        f"📈 Cuota: {pick['odds']:.2f}\n\n"
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

    active_key = f"active:{user_id}"

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
        balance = get_user_balance(user_id)

        if balance is None:
            await update.message.reply_text(
                "⚠️ No encontré tu cuenta."
            )
            return

    except Exception as e:
        print("ERROR BALANCE:", e)

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

    pick = pending_bets[active_key]

    potential_return = amount * pick["odds"]

    pick["amount"] = amount
    pick["potential_return"] = potential_return

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Confirmar",
                callback_data=f"confirm:{user_id}"
            ),
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data=f"cancel:{user_id}"
            ),
        ]
    ]

    await update.message.reply_text(
        "🎯 CONFIRMAR APUESTA\n\n"
        f"⚽ {pick['home']} vs {pick['away']}\n\n"
        f"🎯 Selección: {pick['selection']}\n"
        f"📈 Cuota: {pick['odds']:.2f}\n"
        f"💵 Apuesta: {amount:,} créditos\n"
        f"💰 Posible retorno: "
        f"{potential_return:,.0f} créditos\n\n"
        "¿Confirmar apuesta?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def confirm_bet(query):
    user_id = query.from_user.id

    active_key = f"active:{user_id}"

    pick = pending_bets.get(active_key)

    if not pick:
        await query.edit_message_text(
            "⚠️ No hay una apuesta pendiente."
        )
        return

    amount = pick["amount"]

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:

                # Obtener usuario y bloquear su fila
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

                # Verificar saldo dentro de la transacción
                if amount > balance:
                    await query.edit_message_text(
                        "⚠️ Ya no tienes saldo suficiente.\n\n"
                        f"Saldo disponible: "
                        f"{float(balance):,.0f} créditos."
                    )
                    return

                new_balance = balance - amount

                # Descontar saldo
                cursor.execute("""
                    UPDATE users
                    SET balance = %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (
                    new_balance,
                    db_user_id
                ))

                # Registrar apuesta
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
                        selection,
                        odds,
                        stake,
                        potential_return,
                        status
                    )
                    VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                    RETURNING id
                """, (
                    db_user_id,
                    "football",
                    pick.get("sport_key"),
                    pick["event_id"],
                    event_name,
                    pick["selection"],
                    pick["odds"],
                    amount,
                    pick["potential_return"],
                    "Pendiente",
                ))

                bet_id = cursor.fetchone()[0]

                # Registrar movimiento de saldo
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

        # Solo después del COMMIT
        del pending_bets[active_key]

        await query.edit_message_text(
            "✅ APUESTA REGISTRADA\n\n"
            f"⚽ {pick['home']} vs {pick['away']}\n\n"
            f"🎯 Selección: {pick['selection']}\n"
            f"📈 Cuota: {pick['odds']:.2f}\n"
            f"💵 Apuesta: {amount:,} créditos\n"
            f"💰 Posible retorno: "
            f"{pick['potential_return']:,.0f} créditos\n\n"
            f"💳 Nuevo saldo: "
            f"{float(new_balance):,.0f} créditos\n\n"
            "🎯 La apuesta queda pendiente."
        )

        print(
            f"✅ APUESTA GUARDADA EN NEON: "
            f"bet_id={bet_id}, user_id={db_user_id}"
        )

    except Exception as e:
        print("ERROR CONFIRM BET:", e)

        await query.edit_message_text(
            "⚠️ No se pudo registrar la apuesta.\n\n"
            "No se descontaron créditos."
        )


async def cancel_bet(query):
    user_id = query.from_user.id

    active_key = f"active:{user_id}"

    if active_key in pending_bets:
        del pending_bets[active_key]

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
                        status
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
                "Todavía no tienes apuestas registradas.",
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

        text = "🎯 MIS APUESTAS\n\n"

        for i, bet in enumerate(user_bets, 1):
            event_name, selection, odds, stake, status = bet

            text += (
                f"#{i}\n"
                f"⚽ {event_name}\n"
                f"🎯 {selection}\n"
                f"📈 Cuota: {odds:.2f}\n"
                f"💵 Apuesta: {stake:,}\n"
                f"📌 Estado: {status}\n\n"
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
        print("❌ ERROR MIS APUESTAS:", e)

        await query.edit_message_text(
            "❌ No se pudieron cargar tus apuestas.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Volver",
                        callback_data="home"
                    )
                ]
            ])
        )


async def button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    data = query.data

    if data == "football":
        await show_sports(query)

    elif data == "baseball":
        await query.edit_message_text(
            "⚾ BÉISBOL\n\n"
            "Lo agregaremos más adelante."
        )

    elif data == "balance":
        user_id = query.from_user.id

        try:
            balance = get_user_balance(user_id)

            if balance is None:
                await query.edit_message_text(
                    "⚠️ No encontré tu cuenta."
                )
                return

        except Exception as e:
            print("ERROR BALANCE:", e)

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
        await show_bets(query)

    elif data == "home":
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

        user_id = query.from_user.id

        try:
            balance = get_user_balance(user_id)

            if balance is None:
                balance = 1000

        except Exception as e:
            print("ERROR BALANCE:", e)
            balance = 1000

        await query.edit_message_text(
            "🏆 SPORTS BOT\n\n"
            f"💰 Saldo: "
            f"{balance:,.0f} créditos\n\n"
            "Selecciona una opción:",
            reply_markup=InlineKeyboardMarkup(keyboard),
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
        await confirm_bet(query)

    elif data.startswith("cancel:"):
        await cancel_bet(query)


def main():
    if not BOT_TOKEN:
        raise ValueError("Falta BOT_TOKEN")

    if not ODDS_API_KEY:
        raise ValueError("Falta ODDS_API_KEY")

    if not DATABASE_URL:
        raise ValueError("Falta DATABASE_URL")

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
