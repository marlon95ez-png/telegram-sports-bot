import os
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

balances = {}
bets = {}


def get_sports():
    url = "https://api.the-odds-api.com/v4/sports/"
    params = {
        "apiKey": ODDS_API_KEY
    }

    response = requests.get(url, params=params, timeout=15)
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

    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()

    return response.json()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in balances:
        balances[user_id] = 1000

    keyboard = [
        [
            InlineKeyboardButton("⚽ Fútbol", callback_data="football"),
            InlineKeyboardButton("⚾ Béisbol", callback_data="baseball"),
        ],
        [
            InlineKeyboardButton("💰 Mi saldo", callback_data="balance"),
            InlineKeyboardButton("🎯 Mis apuestas", callback_data="bets"),
        ],
    ]

    await update.message.reply_text(
        "👋 ¡Bienvenido!\n\n"
        "🏆 Sports Bot\n\n"
        "💵 Saldo virtual: 1,000 créditos\n\n"
        "Selecciona una opción:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_sports(query):
    try:
        sports = get_sports()

        football = [
            s for s in sports
            if s.get("group") == "Soccer" and s.get("active")
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
            InlineKeyboardButton("⬅️ Volver", callback_data="home")
        ])

        if not football:
            text = "⚽ No hay ligas de fútbol disponibles en este momento."
        else:
            text = (
                "⚽ FÚTBOL\n\n"
                "Selecciona una competición:\n\n"
                f"Encontradas: {len(football)}"
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
                "⚽ No hay partidos disponibles para esta competición."
            )
            return

        keyboard = []

        for game in games[:15]:
            home = game.get("home_team", "Local")
            away = game.get("away_team", "Visitante")

            keyboard.append([
                InlineKeyboardButton(
                    f"⚽ {home} vs {away}",
                    callback_data=f"game:{sport_key}:{game['id']}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                "⬅️ Competiciones",
                callback_data="football"
            )
        ])

        await query.edit_message_text(
            f"⚽ PARTIDOS\n\n"
            f"Competición: {sport_key}\n\n"
            "Selecciona un partido:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        print("ERROR ODDS:", e)

        await query.edit_message_text(
            "⚠️ No pude obtener los partidos.\n\n"
            "Puede que esta competición no tenga cuotas disponibles "
            "en este momento."
        )


async def show_game(query, sport_key, event_id):
    try:
        games = get_odds(sport_key)

        game = next(
            (g for g in games if g["id"] == event_id),
            None
        )

        if not game:
            await query.edit_message_text(
                "⚠️ Este partido ya no está disponible."
            )
            return

        home = game["home_team"]
        away = game["away_team"]

        outcomes = []

        for bookmaker in game.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market["key"] == "h2h":
                    outcomes.extend(market.get("outcomes", []))

        # Evitamos repetir selecciones cuando hay varios bookmakers
        unique = {}

        for outcome in outcomes:
            name = outcome["name"]

            if name not in unique:
                unique[name] = outcome["price"]

        keyboard = []

        for name, price in unique.items():
            keyboard.append([
                InlineKeyboardButton(
                    f"{name} — {price:.2f}",
                    callback_data="selection"
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
            "📊 Cuotas disponibles:\n\n"
        )

        for name, price in unique.items():
            text += f"• {name}: {price:.2f}\n"

        text += "\n💡 Selecciona una opción para continuar."

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        print("ERROR GAME:", e)

        await query.edit_message_text(
            "⚠️ No pude obtener las cuotas de este partido."
        )


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "football":
        await show_sports(query)

    elif data == "baseball":
        await query.edit_message_text(
            "⚾ BÉISBOL\n\n"
            "La conexión con los eventos de béisbol "
            "la agregaremos en el siguiente paso."
        )

    elif data == "balance":
        user_id = query.from_user.id
        balance = balances.get(user_id, 1000)

        await query.edit_message_text(
            f"💰 MI SALDO\n\n"
            f"Créditos disponibles: {balance:,.0f}\n\n"
            "Saldo completamente virtual durante esta etapa.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Volver", callback_data="home")]
            ])
        )

    elif data == "bets":
        await query.edit_message_text(
            "🎯 MIS APUESTAS\n\n"
            "Todavía no tienes apuestas registradas.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Volver", callback_data="home")]
            ])
        )

    elif data == "home":
        keyboard = [
            [
                InlineKeyboardButton("⚽ Fútbol", callback_data="football"),
                InlineKeyboardButton("⚾ Béisbol", callback_data="baseball"),
            ],
            [
                InlineKeyboardButton("💰 Mi saldo", callback_data="balance"),
                InlineKeyboardButton("🎯 Mis apuestas", callback_data="bets"),
            ],
        ]

        await query.edit_message_text(
            "🏆 SPORTS BOT\n\n"
            "Selecciona una opción:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("sport:"):
        sport_key = data.split(":", 1)[1]
        await show_games(query, sport_key)

    elif data.startswith("game:"):
        _, sport_key, event_id = data.split(":", 2)
        await show_game(query, sport_key, event_id)

    elif data == "selection":
        await query.edit_message_text(
            "🎯 SELECCIÓN\n\n"
            "La selección de la cuota funciona correctamente.\n\n"
            "💵 En el siguiente paso agregaremos "
            "la entrada del monto y la confirmación de la apuesta."
        )


def main():
    if not BOT_TOKEN:
        raise ValueError("Falta BOT_TOKEN")

    if not ODDS_API_KEY:
        raise ValueError("Falta ODDS_API_KEY")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))

    print("Bot iniciado correctamente...")

    app.run_polling()


if __name__ == "__main__":
    main()
