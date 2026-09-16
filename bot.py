import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")

# Saldo virtual de cada usuario
balances = {}


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
        "🏆 Sports Bot\n"
        "Tu plataforma de apuestas deportivas.\n\n"
        "💵 Saldo inicial: 1,000 créditos virtuales\n\n"
        "Selecciona una opción:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if query.data == "balance":
        balance = balances.get(user_id, 1000)

        await query.edit_message_text(
            f"💰 Tu saldo\n\n"
            f"💵 Créditos disponibles: {balance:,.0f}\n\n"
            "Estos créditos son virtuales."
        )

    elif query.data == "football":
        keyboard = [
            [InlineKeyboardButton(
                "⚽ Equipo A — 2.00",
                callback_data="bet_football_a"
            )],
            [InlineKeyboardButton(
                "⚽ Equipo B — 2.50",
                callback_data="bet_football_b"
            )],
            [InlineKeyboardButton(
                "⬅️ Volver",
                callback_data="home"
            )],
        ]

        await query.edit_message_text(
            "⚽ FÚTBOL\n\n"
            "Partido de prueba\n"
            "Equipo A vs Equipo B\n\n"
            "Selecciona una opción:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif query.data == "baseball":
        await query.edit_message_text(
            "⚾ BÉISBOL\n\n"
            "Próximamente tendremos eventos disponibles."
        )

    elif query.data == "bets":
        await query.edit_message_text(
            "🎯 Mis apuestas\n\n"
            "Todavía no tienes apuestas registradas."
        )

    elif query.data.startswith("bet_"):
        await query.edit_message_text(
            "🎯 Apuesta seleccionada\n\n"
            "Esta es una apuesta de prueba.\n\n"
            "💵 En la siguiente versión podremos "
            "introducir el monto y registrar la apuesta."
        )

    elif query.data == "home":
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


def main():
    if not TOKEN:
        raise ValueError("Falta la variable BOT_TOKEN")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))

    print("Bot iniciado correctamente...")
    app.run_polling()


if __name__ == "__main__":
    main()
