import os
import requests
import psycopg

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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

# Selecciones temporales mientras el usuario está escogiendo
# una apuesta. Las apuestas reales viven en Neon.
active_picks = {}


# ============================================================
# CONSTANTES
# ============================================================

MONEY_PLACES = Decimal("0.01")

STATUS_PENDING = "pending"
STATUS_SETTLED = "settled"
STATUS_CANCELLED = "cancelled"

RESULT_WON = "won"
RESULT_LOST = "lost"
RESULT_VOID = "void"

TRANSACTION_BET = "bet"
TRANSACTION_WIN = "win"
TRANSACTION_DEPOSIT = "deposit"
TRANSACTION_REFUND = "refund"


# ============================================================
# BASE DE DATOS
# ============================================================

def get_db():
    return psycopg.connect(DATABASE_URL)


# ============================================================
# UTILIDADES
# ============================================================

def money(value):
    """
    Convierte un valor a Decimal con 2 decimales.
    """
    try:
        return Decimal(str(value)).quantize(
            MONEY_PLACES,
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


def is_admin(user_id):
    if not ADMIN_TELEGRAM_ID:
        return False

    return str(user_id) == str(ADMIN_TELEGRAM_ID)


def format_money(value):
    return f"{money(value):.2f}"


# ============================================================
# USUARIOS
# ============================================================

def get_or_create_user(telegram_user):

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    balance
                FROM users
                WHERE telegram_id = %s
                """,
                (telegram_user.id,),
            )

            row = cur.fetchone()

            if row:
                return row

            cur.execute(
                """
                INSERT INTO users (
                    telegram_id,
                    username
                )
                VALUES (
                    %s,
                    %s
                )
                RETURNING
                    id,
                    balance
                """,
                (
                    telegram_user.id,
                    telegram_user.username,
                ),
            )

            row = cur.fetchone()

        conn.commit()

    return row


# ============================================================
# MENÚ PRINCIPAL
# ============================================================

def main_keyboard(user_id):

    keyboard = [
        [
            InlineKeyboardButton(
                "🇪🇸 LaLiga",
                callback_data="sport:soccer_spain_la_liga",
            )
        ],
        [
            InlineKeyboardButton(
                "🏴 Premier League",
                callback_data="sport:soccer_epl",
            )
        ],
        [
            InlineKeyboardButton(
                "🇮🇹 Serie A",
                callback_data="sport:soccer_italy_serie_a",
            )
        ],
        [
            InlineKeyboardButton(
                "🇩🇪 Bundesliga",
                callback_data="sport:soccer_germany_bundesliga",
            )
        ],
        [
            InlineKeyboardButton(
                "🇫🇷 Ligue 1",
                callback_data="sport:soccer_france_ligue_one",
            )
        ],
        [
            InlineKeyboardButton(
                "🏆 UEFA Nations League",
                callback_data="sport:soccer_uefa_nations_league",
            )
        ],
    ]

    if is_admin(user_id):
        keyboard.append([
            InlineKeyboardButton(
                "🛠 ADMIN - APUESTAS PENDIENTES",
                callback_data="admin_bets",
            )
        ])

    return keyboard


async def show_main_menu(query):

    await query.edit_message_text(
        "🏆 CUBA SPORTS\n\n"
        "Selecciona una competición:",
        reply_markup=InlineKeyboardMarkup(
            main_keyboard(query.from_user.id)
        ),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    get_or_create_user(update.effective_user)

    await update.message.reply_text(
        "🏆 CUBA SPORTS\n\n"
        "Selecciona una competición:",
        reply_markup=InlineKeyboardMarkup(
            main_keyboard(update.effective_user.id)
        ),
    )


# ============================================================
# ODDS API
# ============================================================

def get_odds(sport_key):

    url = (
        "https://api.the-odds-api.com/v4/"
        f"sports/{sport_key}/odds/"
    )

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }

    response = requests.get(
        url,
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


def get_event_odds(sport_key, event_id):

    url = (
        "https://api.the-odds-api.com/v4/"
        f"sports/{sport_key}/events/{event_id}/odds/"
    )

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }

    response = requests.get(
        url,
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# NORMALIZACIÓN DE EQUIPOS
# ============================================================

def normalize_team_name(name):

    if not name:
        return ""

    value = str(name).lower().strip()

    replacements = {
        " fc": "",
        " cf": "",
        " sc": "",
        " afc": "",
        ".": "",
        ",": "",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    return " ".join(value.split())


def teams_match(
    expected_home,
    expected_away,
    api_home,
    api_away,
):

    eh = normalize_team_name(expected_home)
    ea = normalize_team_name(expected_away)

    ah = normalize_team_name(api_home)
    aa = normalize_team_name(api_away)

    return eh == ah and ea == aa


# ============================================================
# RESULTADOS — THE ODDS API
# ============================================================

def get_odds_api_score_event(
    sport_key,
    event_id,
):

    url = (
        "https://api.the-odds-api.com/v4/"
        f"sports/{sport_key}/scores/"
    )

    params = {
        "apiKey": ODDS_API_KEY,
        "daysFrom": 3,
        "eventIds": event_id,
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=30,
        )

    except Exception as e:

        print(
            "THE ODDS API SCORE ERROR:",
            repr(e),
        )

        return None

    print(
        "THE ODDS API SCORE STATUS:",
        response.status_code,
    )

    print(
        "THE ODDS API SCORE RESPONSE:",
        response.text[:3000],
    )

    if response.status_code != 200:
        return None

    try:
        events = response.json()
    except Exception:
        return None

    if not isinstance(events, list):
        return None

    for event in events:

        if str(event.get("id")) == str(event_id):
            return event

    return None


# ============================================================
# ESPN — MAPEO DE COMPETICIONES
# ============================================================

def get_espn_league(sport_key):

    mapping = {
        "soccer_epl": "eng.1",
        "soccer_spain_la_liga": "esp.1",
        "soccer_italy_serie_a": "ita.1",
        "soccer_germany_bundesliga": "ger.1",
        "soccer_france_ligue_one": "fra.1",
        "soccer_uefa_nations_league": "uefa.nations",
    }

    return mapping.get(
        str(sport_key or "").strip().lower()
    )


# ============================================================
# ESPN — NORMALIZAR EQUIPOS
# ============================================================

def normalize_espn_team_name(name):

    if not name:
        return ""

    value = str(name).lower().strip()

    value = " ".join(value.split())

    aliases = {
        "rep ireland": "ireland",
        "republic of ireland": "ireland",
        "wales": "wales",
        "portugal": "portugal",
    }

    if value in aliases:
        value = aliases[value]

    return value


def espn_teams_match(
    expected_home,
    expected_away,
    api_home,
    api_away,
):

    eh = normalize_espn_team_name(
        expected_home
    )

    ea = normalize_espn_team_name(
        expected_away
    )

    ah = normalize_espn_team_name(
        api_home
    )

    aa = normalize_espn_team_name(
        api_away
    )

    return eh == ah and ea == aa


# ============================================================
# ESPN — CONVERTIR EVENTO
# ============================================================

def convert_espn_event(event):

    competitions = event.get(
        "competitions",
        [],
    )

    if not competitions:
        return None

    competition = competitions[0]

    competitors = competition.get(
        "competitors",
        [],
    )

    if len(competitors) < 2:
        return None

    home = None
    away = None

    for competitor in competitors:

        team = competitor.get(
            "team",
            {},
        )

        team_name = (
            team.get("displayName")
            or team.get("shortDisplayName")
            or team.get("name")
        )

        entry = {
            "name": team_name,
            "score": competitor.get("score"),
            "homeAway": competitor.get("homeAway"),
        }

        if competitor.get("homeAway") == "home":
            home = entry

        elif competitor.get("homeAway") == "away":
            away = entry

    if not home or not away:
        return None

    status = competition.get(
        "status",
        {},
    )

    status_type = status.get(
        "type",
        {},
    )

    completed = bool(
        status_type.get("completed")
    )

    return {
        "id": f"espn_{event.get('id')}",
        "home_team": home["name"],
        "away_team": away["name"],
        "completed": completed,
        "scores": [
            {
                "name": home["name"],
                "score": str(
                    home["score"]
                    if home["score"] is not None
                    else ""
                ),
            },
            {
                "name": away["name"],
                "score": str(
                    away["score"]
                    if away["score"] is not None
                    else ""
                ),
            },
        ],
        "source": "ESPN",
        "espn_event_id": event.get("id"),
        "status_detail": status_type.get(
            "detail"
        ),
    }


# ============================================================
# ESPN — BUSCAR PARTIDO
# ============================================================

def get_espn_score_event(
    sport_key,
    home_team,
    away_team,
):

    league = get_espn_league(
        sport_key
    )

    if not league:
        print(
            "ESPN FALLBACK OMITIDO:",
            sport_key,
        )
        return None

    base_url = (
        "https://site.api.espn.com/apis/site/v2/"
        f"sports/soccer/{league}/scoreboard"
    )

    now = datetime.now(
        timezone.utc
    )

    dates_to_try = [
        now.strftime("%Y%m%d"),
        (now - timedelta(days=1)).strftime("%Y%m%d"),
    ]

    for date_value in dates_to_try:

        print(
            "ESPN REQUEST DATE:",
            date_value,
        )

        try:

            response = requests.get(
                base_url,
                params={
                    "dates": date_value,
                },
                timeout=30,
            )

        except Exception as e:

            print(
                "ESPN REQUEST ERROR:",
                repr(e),
            )

            continue

        if response.status_code != 200:

            print(
                "ESPN STATUS:",
                response.status_code,
            )

            continue

        try:
            data = response.json()
        except Exception:
            continue

        events = data.get(
            "events",
            [],
        )

        for raw_event in events:

            converted = convert_espn_event(
                raw_event
            )

            if not converted:
                continue

            api_home = converted.get(
                "home_team"
            )

            api_away = converted.get(
                "away_team"
            )

            if not espn_teams_match(
                home_team,
                away_team,
                api_home,
                api_away,
            ):
                continue

            print(
                "ESPN MATCH FOUND:",
                api_home,
                "vs",
                api_away,
            )

            return converted

    print(
        "ESPN: partido no encontrado."
    )

    return None


# ============================================================
# OBTENER RESULTADO
#
# IMPORTANTE:
# 1. Primero The Odds API.
# 2. Si no encuentra el evento, ESPN.
#
# No hacemos una segunda consulta amplia de Scores a
# The Odds API, para evitar gastar créditos innecesariamente.
# ============================================================

def get_event_result(
    sport_key,
    event_id,
    home_team,
    away_team,
):

    print("========================================")
    print("VERIFICANDO RESULTADO")
    print("SPORT:", sport_key)
    print("EVENT ID:", event_id)
    print("HOME:", home_team)
    print("AWAY:", away_team)
    print("========================================")

    # --------------------------------------------------------
    # 1. THE ODDS API
    # --------------------------------------------------------

    odds_event = get_odds_api_score_event(
        sport_key,
        event_id,
    )

    if odds_event:

        print(
            "The Odds API: evento encontrado"
        )

        return {
            "event": odds_event,
            "source": "The Odds API",
        }

    print(
        "The Odds API: evento no encontrado"
    )

    # --------------------------------------------------------
    # 2. ESPN
    # --------------------------------------------------------

    espn_event = get_espn_score_event(
        sport_key,
        home_team,
        away_team,
    )

    if espn_event:

        print(
            "ESPN: partido encontrado"
        )

        return {
            "event": espn_event,
            "source": "ESPN",
        }

    print(
        "ESPN: partido no encontrado"
    )

    return None


# ============================================================
# RESULTADO H2H
# ============================================================

def determine_h2h_result(event):

    scores = event.get(
        "scores"
    )

    if not scores:
        return None

    home_team = event.get(
        "home_team"
    )

    away_team = event.get(
        "away_team"
    )

    home_score = None
    away_score = None

    for score in scores:

        name = score.get(
            "name"
        )

        value = score.get(
            "score"
        )

        try:
            value = int(value)
        except (ValueError, TypeError):
            continue

        if name == home_team:
            home_score = value

        elif name == away_team:
            away_score = value

    if home_score is None or away_score is None:
        return None

    if home_score > away_score:
        return "HOME"

    if away_score > home_score:
        return "AWAY"

    return "DRAW"


def evaluate_h2h_selection(
    event,
    selection,
):

    result = determine_h2h_result(
        event
    )

    if result is None:
        return False

    home_team = event.get(
        "home_team"
    )

    away_team = event.get(
        "away_team"
    )

    selection_normalized = (
        str(selection)
        .strip()
        .lower()
    )

    home_normalized = (
        str(home_team)
        .strip()
        .lower()
    )

    away_normalized = (
        str(away_team)
        .strip()
        .lower()
    )

    if result == "HOME":
        return (
            selection_normalized
            == home_normalized
        )

    if result == "AWAY":
        return (
            selection_normalized
            == away_normalized
        )

    if result == "DRAW":
        return selection_normalized in [
            "draw",
            "empate",
        ]

    return False


# ============================================================
# FORMATEAR RESULTADO PARA ADMIN
# ============================================================

def get_score_text(event):

    scores = event.get(
        "scores",
        [],
    )

    home = event.get(
        "home_team",
        "?",
    )

    away = event.get(
        "away_team",
        "?",
    )

    home_score = "?"
    away_score = "?"

    for score in scores:

        if score.get("name") == home:
            home_score = score.get(
                "score",
                "?",
            )

        elif score.get("name") == away:
            away_score = score.get(
                "score",
                "?",
            )

    return (
        f"⚽ {home} {home_score}\n"
        f"⚽ {away} {away_score}"
    )


def get_result_status_text(event):

    if event.get("source") == "ESPN":

        if event.get("completed"):
            return "FINAL"

        return (
            event.get(
                "status_detail"
            )
            or "EN JUEGO"
        )

    raw_event = event.get(
        "event",
        {}
    )

    if raw_event.get("completed"):
        return "FINAL"

    return "NO FINALIZADO"


# ============================================================
# VERIFICAR APUESTA PARA LIQUIDACIÓN
# ============================================================

def prepare_bet_settlement(bet_id):

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    user_id,
                    sport_key,
                    event_id,
                    home_team,
                    away_team,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    status,
                    result,
                    created_at,
                    settled_at
                FROM bets
                WHERE id = %s
                """,
                (bet_id,),
            )

            row = cur.fetchone()

    if not row:

        return {
            "success": False,
            "message": (
                f"No existe la apuesta #{bet_id}."
            ),
        }

    (
        db_bet_id,
        user_id,
        sport_key,
        event_id,
        home_team,
        away_team,
        selection,
        odds,
        stake,
        potential_return,
        status,
        result,
        created_at,
        settled_at,
    ) = row

    # --------------------------------------------------------
    # SEGURO CONTRA DOBLE LIQUIDACIÓN
    # --------------------------------------------------------

    if status != STATUS_PENDING:

        return {
            "success": False,
            "already_settled": (
                status == STATUS_SETTLED
            ),
            "message": (
                f"La apuesta #{bet_id} "
                f"ya no está pendiente.\n\n"
                f"Estado: {status}"
            ),
        }

    verification = get_event_result(
        sport_key,
        event_id,
        home_team,
        away_team,
    )

    if not verification:

        return {
            "success": False,
            "message": (
                "No se encontró el resultado "
                "del partido."
            ),
        }

    event = verification["event"]
    source = verification["source"]

    if source == "ESPN":

        if not event.get("completed"):

            return {
                "success": False,
                "message": (
                    "ESPN encontró el partido, "
                    "pero todavía no aparece "
                    "como FINAL."
                ),
            }

    else:

        if not event.get("completed"):

            return {
                "success": False,
                "message": (
                    "The Odds API encontró el "
                    "partido, pero todavía no "
                    "aparece como finalizado."
                ),
            }

    result = determine_h2h_result(
        event
    )

    if result is None:

        return {
            "success": False,
            "message": (
                "No fue posible determinar "
                "el resultado."
            ),
        }

    won = evaluate_h2h_selection(
        event,
        selection,
    )

    return {
        "success": True,
        "bet_id": db_bet_id,
        "user_id": user_id,
        "sport_key": sport_key,
        "event_id": event_id,
        "home_team": event.get(
            "home_team",
            home_team,
        ),
        "away_team": event.get(
            "away_team",
            away_team,
        ),
        "selection": selection,
        "odds": odds,
        "stake": stake,
        "potential_return": potential_return,
        "status": status,
        "result": result,
        "won": won,
        "source": source,
        "event": event,
    }


# ============================================================
# MOSTRAR PREVISUALIZACIÓN DE LIQUIDACIÓN
# ============================================================

async def show_settlement_preview(
    query,
    bet_id,
    result,
):

    source = result["source"]
    event = result["event"]

    score_text = get_score_text(
        event
    )

    status_text = get_result_status_text(
        result
    )

    if source == "The Odds API":

        source_text = (
            "The Odds API: "
            "evento encontrado"
        )

    else:

        source_text = (
            "The Odds API: "
            "evento no encontrado\n"
            "ESPN: partido encontrado"
        )

    keyboard = [
        [
            InlineKeyboardButton(
                f"✅ SÍ, LIQUIDAR #{bet_id}",
                callback_data=(
                    f"settle_confirm_bet:{bet_id}"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "❌ CANCELAR",
                callback_data=(
                    f"settle_cancel:{bet_id}"
                ),
            )
        ],
    ]

    await query.edit_message_text(
        "🔎 Verificando resultado...\n\n"
        f"{source_text}\n\n"
        f"{score_text}\n\n"
        f"Estado: {status_text}\n\n"
        f"¿Liquidar apuesta #{bet_id}?",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# LIQUIDAR APUESTA — PROTECCIÓN ATÓMICA
# ============================================================

def settle_bet_transaction(
    bet_id,
    expected_result,
):

    with get_db() as conn:

        with conn.cursor() as cur:

            # ==================================================
            # BLOQUEO DE LA APUESTA
            #
            # FOR UPDATE impide que dos procesos puedan
            # liquidar la misma apuesta simultáneamente.
            # ==================================================

            cur.execute(
                """
                SELECT
                    id,
                    user_id,
                    home_team,
                    away_team,
                    selection,
                    stake,
                    potential_return,
                    status,
                    result
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
                    "message": (
                        f"No existe la apuesta #{bet_id}."
                    ),
                }

            (
                db_bet_id,
                user_id,
                home_team,
                away_team,
                selection,
                stake,
                potential_return,
                status,
                existing_result,
            ) = bet

            # ==================================================
            # SEGUNDO SEGURO CONTRA DOBLE LIQUIDACIÓN
            # ==================================================

            if status != STATUS_PENDING:

                return {
                    "success": False,
                    "already_settled": (
                        status == STATUS_SETTLED
                    ),
                    "message": (
                        f"La apuesta #{bet_id} "
                        "ya fue procesada.\n\n"
                        f"Estado: {status}"
                    ),
                }

            # ==================================================
            # BLOQUEAR SALDO DEL USUARIO
            # ==================================================

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

                return {
                    "success": False,
                    "message": (
                        "No se encontró el usuario "
                        "de la apuesta."
                    ),
                }

            current_balance = money(
                user_row[0]
            )

            # ==================================================
            # DETERMINAR PREMIO
            # ==================================================

            if expected_result == RESULT_WON:

                new_balance = money(
                    current_balance
                    + money(potential_return)
                )

                transaction_type = (
                    TRANSACTION_WIN
                )

                transaction_amount = money(
                    potential_return
                )

                description = (
                    f"Premio apuesta #{bet_id} "
                    f"{home_team} vs {away_team}"
                )

            elif expected_result == RESULT_VOID:

                new_balance = money(
                    current_balance
                    + money(stake)
                )

                transaction_type = (
                    TRANSACTION_REFUND
                )

                transaction_amount = money(
                    stake
                )

                description = (
                    f"Reembolso apuesta #{bet_id} "
                    f"{home_team} vs {away_team}"
                )

            else:

                new_balance = current_balance

                transaction_type = None
                transaction_amount = None
                description = None

            # ==================================================
            # ACTUALIZAR SALDO
            # ==================================================

            if new_balance != current_balance:

                cur.execute(
                    """
                    UPDATE users
                    SET
                        balance = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        new_balance,
                        user_id,
                    ),
                )

            # ==================================================
            # REGISTRAR TRANSACCIÓN DE PREMIO
            # ==================================================

            if transaction_type:

                cur.execute(
                    """
                    INSERT INTO transactions (
                        user_id,
                        type,
                        amount,
                        balance_before,
                        balance_after,
                        bet_id,
                        description
                    )
                    VALUES (
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
                        user_id,
                        transaction_type,
                        transaction_amount,
                        current_balance,
                        new_balance,
                        bet_id,
                        description,
                    ),
                )

            # ==================================================
            # MARCAR APUESTA COMO LIQUIDADA
            #
            # La condición status = pending es otra protección.
            # ==================================================

            cur.execute(
                """
                UPDATE bets
                SET
                    status = 'settled',
                    result = %s,
                    settled_at = CURRENT_TIMESTAMP
                WHERE id = %s
                AND status = 'pending'
                """,
                (
                    expected_result,
                    bet_id,
                ),
            )

            if cur.rowcount != 1:

                # Si por cualquier motivo no se actualizó
                # exactamente una fila, hacemos rollback.
                conn.rollback()

                return {
                    "success": False,
                    "message": (
                        "La apuesta no pudo marcarse "
                        "como liquidada. No se realizó "
                        "ningún pago."
                    ),
                }

        # ======================================================
        # COMMIT ATÓMICO
        #
        # Saldo + transacción + estado de apuesta
        # quedan confirmados juntos.
        # ======================================================

        conn.commit()

    return {
        "success": True,
        "bet_id": bet_id,
        "won": expected_result == RESULT_WON,
        "result": expected_result,
        "new_balance": new_balance,
        "home_team": home_team,
        "away_team": away_team,
        "selection": selection,
        "potential_return": potential_return,
    }


# ============================================================
# LIQUIDAR APUESTA DESDE COMANDO
# ============================================================

async def test_settle(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.effective_user:
        return

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "⛔ No autorizado."
        )

        return

    if not context.args:

        await update.message.reply_text(
            "Uso:\n"
            "/liquidar ID_APUESTA\n\n"
            "Ejemplo:\n"
            "/liquidar 10"
        )

        return

    try:

        bet_id = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ El ID de apuesta no es válido."
        )

        return

    await update.message.reply_text(
        "🔎 Verificando resultado..."
    )

    try:

        result = prepare_bet_settlement(
            bet_id
        )

        if not result.get("success"):

            await update.message.reply_text(
                "❌ No se pudo verificar la apuesta:\n\n"
                + result.get(
                    "message",
                    "Error desconocido.",
                )
            )

            return

        source = result["source"]

        if source == "The Odds API":

            source_text = (
                "The Odds API: "
                "evento encontrado"
            )

        else:

            source_text = (
                "The Odds API: "
                "evento no encontrado\n"
                "ESPN: partido encontrado"
            )

        event = result["event"]

        score_text = get_score_text(
            event
        )

        status_text = get_result_status_text(
            result
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    f"✅ SÍ, LIQUIDAR #{bet_id}",
                    callback_data=(
                        f"settle_confirm_bet:{bet_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ CANCELAR",
                    callback_data=(
                        f"settle_cancel:{bet_id}"
                    ),
                )
            ],
        ]

        await update.message.reply_text(
            "🔎 Verificando resultado...\n\n"
            f"{source_text}\n\n"
            f"{score_text}\n\n"
            f"Estado: {status_text}\n\n"
            f"¿Liquidar apuesta #{bet_id}?",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            ),
        )

    except Exception as e:

        print(
            "ERROR EN /LIQUIDAR:",
            repr(e),
        )

        await update.message.reply_text(
            "❌ Error verificando la apuesta:\n\n"
            f"{e}"
        )


# ============================================================
# CONFIRMAR LIQUIDACIÓN
# ============================================================

async def confirm_settlement(
    query,
    bet_id,
):

    if not is_admin(
        query.from_user.id
    ):

        await query.answer(
            "No autorizado.",
            show_alert=True,
        )

        return

    await query.answer(
        "Procesando liquidación..."
    )

    try:

        # ------------------------------------------------------
        # VOLVEMOS A LEER EL ESTADO REAL DE LA APUESTA.
        #
        # Si alguien ya la liquidó, esta función NO pagará.
        # ------------------------------------------------------

        with get_db() as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT
                        id,
                        sport_key,
                        event_id,
                        home_team,
                        away_team,
                        selection,
                        status
                    FROM bets
                    WHERE id = %s
                    """,
                    (bet_id,),
                )

                row = cur.fetchone()

        if not row:

            await query.edit_message_text(
                f"❌ La apuesta #{bet_id} no existe."
            )

            return

        (
            db_bet_id,
            sport_key,
            event_id,
            home_team,
            away_team,
            selection,
            status,
        ) = row

        if status != STATUS_PENDING:

            await query.edit_message_text(
                "🛑 ESTA APUESTA YA FUE PROCESADA\n\n"
                f"Apuesta #{bet_id}\n"
                f"Estado: {status}\n\n"
                "No se realizó ningún pago."
            )

            return

        # ------------------------------------------------------
        # VERIFICAMOS NUEVAMENTE EL RESULTADO.
        #
        # Si durante la espera cambió el resultado/fuente,
        # utilizamos el resultado actual.
        # ------------------------------------------------------

        verification = prepare_bet_settlement(
            bet_id
        )

        if not verification.get("success"):

            await query.edit_message_text(
                "❌ No se pudo confirmar la liquidación:\n\n"
                + verification.get(
                    "message",
                    "Error desconocido.",
                )
            )

            return

        if verification["won"]:

            final_result = RESULT_WON

        else:

            final_result = RESULT_LOST

        # ------------------------------------------------------
        # TRANSACCIÓN ATÓMICA
        # ------------------------------------------------------

        settlement = settle_bet_transaction(
            bet_id,
            final_result,
        )

        if not settlement.get("success"):

            if settlement.get(
                "already_settled"
            ):

                await query.edit_message_text(
                    "🛑 ESTA APUESTA YA FUE PROCESADA\n\n"
                    f"Apuesta #{bet_id}\n\n"
                    "No se realizó ningún pago."
                )

            else:

                await query.edit_message_text(
                    "❌ No se pudo liquidar:\n\n"
                    + settlement.get(
                        "message",
                        "Error desconocido.",
                    )
                )

            return

        status_text = (
            "✅ GANADA"
            if settlement["won"]
            else "❌ PERDIDA"
        )

        await query.edit_message_text(
            "🏆 APUESTA LIQUIDADA\n\n"
            f"⚽ {settlement['home_team']}\n"
            "vs\n"
            f"⚽ {settlement['away_team']}\n\n"
            f"📊 Resultado: {settlement['result']}\n\n"
            f"🎯 Estado: {status_text}\n"
            f"💰 Pagado: "
            f"{format_money(settlement['potential_return'])}"
        )

    except Exception as e:

        print(
            "ERROR CONFIRM SETTLEMENT:",
            repr(e),
        )

        await query.edit_message_text(
            "❌ Error liquidando la apuesta:\n\n"
            f"{e}"
        )


# ============================================================
# CANCELAR CONFIRMACIÓN DE LIQUIDACIÓN
# ============================================================

async def cancel_settlement(
    query,
    bet_id,
):

    if not is_admin(
        query.from_user.id
    ):

        await query.answer(
            "No autorizado.",
            show_alert=True,
        )

        return

    await query.answer(
        "Liquidación cancelada."
    )

    await query.edit_message_text(
        "❌ LIQUIDACIÓN CANCELADA\n\n"
        f"La apuesta #{bet_id} "
        "continúa pendiente."
    )


# ============================================================
# MOSTRAR APUESTAS PENDIENTES AL ADMIN
# ============================================================

async def show_admin_bets(query):

    if not is_admin(
        query.from_user.id
    ):

        await query.answer(
            "No autorizado.",
            show_alert=True,
        )

        return

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    event_id,
                    home_team,
                    away_team,
                    COUNT(*)
                FROM bets
                WHERE status = 'pending'
                GROUP BY
                    event_id,
                    home_team,
                    away_team
                ORDER BY
                    MIN(created_at)
                """
            )

            rows = cur.fetchall()

    if not rows:

        await query.edit_message_text(
            "📭 No hay apuestas pendientes.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Volver",
                        callback_data="back_main",
                    )
                ]
            ]),
        )

        return

    keyboard = []

    for (
        event_id,
        home,
        away,
        count,
    ) in rows:

        keyboard.append([
            InlineKeyboardButton(
                (
                    f"⚽ {home} vs {away} "
                    f"({count})"
                ),
                callback_data=(
                    f"admin_event:{event_id}"
                ),
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "🔙 Volver",
            callback_data="back_main",
        )
    ])

    await query.edit_message_text(
        "🧾 APUESTAS PENDIENTES\n\n"
        "Selecciona un partido:",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# MOSTRAR EVENTO ADMIN
# ============================================================

async def show_admin_event(
    query,
    event_id,
):

    if not is_admin(
        query.from_user.id
    ):

        await query.answer(
            "No autorizado.",
            show_alert=True,
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
                    potential_return
                FROM bets
                WHERE event_id = %s
                AND status = 'pending'
                ORDER BY id
                """,
                (event_id,),
            )

            rows = cur.fetchall()

    if not rows:

        await query.edit_message_text(
            "📭 No hay apuestas pendientes "
            "para este partido.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Volver",
                        callback_data="admin_bets",
                    )
                ]
            ]),
        )

        return

    home = rows[0][1]
    away = rows[0][2]

    text = (
        "⚽ PARTIDO\n\n"
        f"{home}\n"
        "vs\n"
        f"{away}\n\n"
        "🧾 APUESTAS:\n\n"
    )

    for row in rows:

        (
            bet_id,
            row_home,
            row_away,
            selection,
            odds,
            stake,
            potential_return,
        ) = row

        text += (
            f"#{bet_id} | "
            f"{selection} | "
            f"Cuota {odds} | "
            f"Apuesta {format_money(stake)}\n"
        )

    keyboard = [
        [
            InlineKeyboardButton(
                "🔎 VERIFICAR RESULTADO",
                callback_data=(
                    f"admin_verify_event:{event_id}"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 Volver",
                callback_data="admin_bets",
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
# VERIFICAR EVENTO COMPLETO
# ============================================================

async def verify_event_for_settlement(
    query,
    event_id,
):

    if not is_admin(
        query.from_user.id
    ):

        await query.answer(
            "No autorizado.",
            show_alert=True,
        )

        return

    await query.answer(
        "Verificando resultado..."
    )

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    sport_key,
                    event_id,
                    home_team,
                    away_team
                FROM bets
                WHERE event_id = %s
                AND status = 'pending'
                ORDER BY id
                LIMIT 1
                """,
                (event_id,),
            )

            row = cur.fetchone()

    if not row:

        await query.edit_message_text(
            "📭 No hay apuestas pendientes "
            "para este partido."
        )

        return

    (
        bet_id,
        sport_key,
        db_event_id,
        home_team,
        away_team,
    ) = row

    verification = get_event_result(
        sport_key,
        db_event_id,
        home_team,
        away_team,
    )

    if not verification:

        await query.edit_message_text(
            "❌ No se encontró el resultado "
            "del partido.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Volver",
                        callback_data=(
                            f"admin_event:{event_id}"
                        ),
                    )
                ]
            ]),
        )

        return

    event = verification["event"]

    if not event.get("completed"):

        await query.edit_message_text(
            "⏳ EL PARTIDO TODAVÍA NO ESTÁ FINALIZADO\n\n"
            f"⚽ {home_team}\n"
            f"vs\n"
            f"⚽ {away_team}\n\n"
            f"Fuente: {verification['source']}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Volver",
                        callback_data=(
                            f"admin_event:{event_id}"
                        ),
                    )
                ]
            ]),
        )

        return

    result = determine_h2h_result(
        event
    )

    if not result:

        await query.edit_message_text(
            "❌ No se pudo determinar "
            "el resultado.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Volver",
                        callback_data=(
                            f"admin_event:{event_id}"
                        ),
                    )
                ]
            ]),
        )

        return

    score_text = get_score_text(
        event
    )

    if verification["source"] == "The Odds API":

        source_text = (
            "The Odds API: "
            "evento encontrado"
        )

    else:

        source_text = (
            "The Odds API: "
            "evento no encontrado\n"
            "ESPN: partido encontrado"
        )

    keyboard = [
        [
            InlineKeyboardButton(
                "💰 LIQUIDAR PARTIDO",
                callback_data=(
                    f"settle_confirm_event:{event_id}"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "❌ CANCELAR",
                callback_data=(
                    f"admin_event:{event_id}"
                ),
            )
        ],
    ]

    await query.edit_message_text(
        "🔎 Verificando resultado...\n\n"
        f"{source_text}\n\n"
        f"{score_text}\n\n"
        "Estado: FINAL\n\n"
        "¿Liquidar todas las apuestas "
        "de este partido?",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# LIQUIDAR TODO EL EVENTO
# ============================================================

async def confirm_settlement_event(
    query,
    event_id,
):

    if not is_admin(
        query.from_user.id
    ):

        await query.answer(
            "No autorizado.",
            show_alert=True,
        )

        return

    await query.answer(
        "Procesando partido..."
    )

    # --------------------------------------------------------
    # Obtenemos las apuestas pendientes.
    # Cada una será bloqueada individualmente.
    # --------------------------------------------------------

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id
                FROM bets
                WHERE event_id = %s
                AND status = 'pending'
                ORDER BY id
                """,
                (event_id,),
            )

            bet_rows = cur.fetchall()

    if not bet_rows:

        await query.edit_message_text(
            "🛑 No quedan apuestas pendientes "
            "para este partido.\n\n"
            "Es posible que ya hayan sido liquidadas."
        )

        return

    # --------------------------------------------------------
    # Verificamos el resultado una vez.
    # --------------------------------------------------------

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    sport_key,
                    event_id,
                    home_team,
                    away_team
                FROM bets
                WHERE event_id = %s
                AND status = 'pending'
                ORDER BY id
                LIMIT 1
                """,
                (event_id,),
            )

            event_row = cur.fetchone()

    if not event_row:

        await query.edit_message_text(
            "📭 No quedan apuestas pendientes."
        )

        return

    (
        sport_key,
        db_event_id,
        home_team,
        away_team,
    ) = event_row

    verification = get_event_result(
        sport_key,
        db_event_id,
        home_team,
        away_team,
    )

    if not verification:

        await query.edit_message_text(
            "❌ No se encontró el resultado."
        )

        return

    event = verification["event"]

    if not event.get("completed"):

        await query.edit_message_text(
            "⏳ El partido todavía no aparece "
            "como FINAL."
        )

        return

    result = determine_h2h_result(
        event
    )

    if result is None:

        await query.edit_message_text(
            "❌ No se pudo determinar "
            "el resultado."
        )

        return

    # --------------------------------------------------------
    # Procesamos cada apuesta.
    # Cada liquidación tiene su propio bloqueo.
    # --------------------------------------------------------

    won_count = 0
    lost_count = 0
    total_paid = Decimal("0.00")
    already_count = 0
    error_count = 0

    for row in bet_rows:

        bet_id = row[0]

        with get_db() as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT
                        selection,
                        status,
                        user_id,
                        potential_return,
                        home_team,
                        away_team
                    FROM bets
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (bet_id,),
                )

                bet = cur.fetchone()

                if not bet:
                    error_count += 1
                    continue

                (
                    selection,
                    status,
                    user_id,
                    potential_return,
                    row_home,
                    row_away,
                ) = bet

                if status != STATUS_PENDING:
                    already_count += 1
                    continue

                won = evaluate_h2h_selection(
                    event,
                    selection,
                )

                final_result = (
                    RESULT_WON
                    if won
                    else RESULT_LOST
                )

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
                    error_count += 1
                    continue

                current_balance = money(
                    user_row[0]
                )

                if won:

                    new_balance = money(
                        current_balance
                        + money(
                            potential_return
                        )
                    )

                    cur.execute(
                        """
                        UPDATE users
                        SET
                            balance = %s,
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
                        INSERT INTO transactions (
                            user_id,
                            type,
                            amount,
                            balance_before,
                            balance_after,
                            bet_id,
                            description
                        )
                        VALUES (
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
                            user_id,
                            TRANSACTION_WIN,
                            money(
                                potential_return
                            ),
                            current_balance,
                            new_balance,
                            bet_id,
                            (
                                f"Premio apuesta #{bet_id} "
                                f"{row_home} vs {row_away}"
                            ),
                        ),
                    )

                    total_paid += money(
                        potential_return
                    )

                    won_count += 1

                else:

                    lost_count += 1

                cur.execute(
                    """
                    UPDATE bets
                    SET
                        status = 'settled',
                        result = %s,
                        settled_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    AND status = 'pending'
                    """,
                    (
                        final_result,
                        bet_id,
                    ),
                )

                if cur.rowcount != 1:
                    conn.rollback()
                    error_count += 1
                    continue

            conn.commit()

    await query.edit_message_text(
        "🏆 PARTIDO LIQUIDADO\n\n"
        f"⚽ {event.get('home_team')}\n"
        "vs\n"
        f"⚽ {event.get('away_team')}\n\n"
        f"📊 Resultado: {result}\n\n"
        f"✅ Ganadas: {won_count}\n"
        f"❌ Perdidas: {lost_count}\n"
        f"💰 Total pagado: "
        f"{format_money(total_paid)}\n"
        f"🛑 Ya procesadas: {already_count}\n"
        f"⚠️ Errores: {error_count}"
    )


# ============================================================
# MOSTRAR PARTIDOS
# ============================================================

async def show_games(
    query,
    sport_key,
):

    try:

        events = get_odds(
            sport_key
        )

    except Exception as e:

        await query.edit_message_text(
            "❌ Error obteniendo partidos:\n\n"
            f"{e}"
        )

        return

    if not events:

        await query.edit_message_text(
            "📭 No hay partidos disponibles."
        )

        return

    keyboard = []

    for event in events:

        event_id = event.get(
            "id"
        )

        home = event.get(
            "home_team"
        )

        away = event.get(
            "away_team"
        )

        if not event_id:
            continue

        keyboard.append([
            InlineKeyboardButton(
                f"⚽ {home} vs {away}",
                callback_data=(
                    f"game:{sport_key}:{event_id}"
                ),
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "🔙 Volver",
            callback_data="back_main",
        )
    ])

    await query.edit_message_text(
        "⚽ PARTIDOS DISPONIBLES\n\n"
        "Selecciona un partido:",
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

    except Exception as e:

        await query.edit_message_text(
            "❌ Error obteniendo las cuotas:\n\n"
            f"{e}"
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

    bookmakers = event.get(
        "bookmakers",
        [],
    )

    keyboard = []

    # --------------------------------------------------------
    # Utilizamos el primer bookmaker que tenga H2H.
    # --------------------------------------------------------

    for bookmaker in bookmakers:

        markets = bookmaker.get(
            "markets",
            [],
        )

        for market in markets:

            if market.get("key") != "h2h":
                continue

            for outcome in market.get(
                "outcomes",
                [],
            ):

                name = outcome.get(
                    "name"
                )

                price = outcome.get(
                    "price"
                )

                if name is None or price is None:
                    continue

                pick_id = (
                    f"{event_id}:{name}"
                )

                active_picks[pick_id] = {
                    "sport_key": sport_key,
                    "event_id": event_id,
                    "home": home,
                    "away": away,
                    "selection": name,
                    "odds": price,
                }

                keyboard.append([
                    InlineKeyboardButton(
                        f"{name} @ {price}",
                        callback_data=(
                            f"pick:{pick_id}"
                        ),
                    )
                ])

            break

        if keyboard:
            break

    if not keyboard:

        await query.edit_message_text(
            "❌ No hay cuotas H2H disponibles "
            "para este partido."
        )

        return

    keyboard.append([
        InlineKeyboardButton(
            "🔙 Volver",
            callback_data=f"sport:{sport_key}",
        )
    ])

    await query.edit_message_text(
        (
            f"⚽ {home}\n"
            f"vs\n"
            f"⚽ {away}\n\n"
            "Selecciona tu apuesta:"
        ),
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# CANTIDAD DE APUESTA
# ============================================================

async def handle_amount(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.effective_user:
        return

    active = context.user_data.get(
        "active_bet"
    )

    if not active:
        return

    try:

        amount = money(
            update.message.text
            .replace(",", ".")
            .strip()
        )

    except Exception:

        await update.message.reply_text(
            "❌ Introduce una cantidad válida."
        )

        return

    if amount <= 0:

        await update.message.reply_text(
            "❌ La cantidad debe ser mayor que 0."
        )

        return

    user_id, balance = get_or_create_user(
        update.effective_user
    )

    balance = money(balance)

    if balance < amount:

        await update.message.reply_text(
            "❌ Saldo insuficiente.\n\n"
            f"💰 Saldo actual: "
            f"{format_money(balance)}"
        )

        return

    odds = Decimal(
        str(active["odds"])
    )

    potential_return = money(
        amount * odds
    )

    # --------------------------------------------------------
    # IMPORTANTE:
    # Ya no guardamos una "unconfirmed_bet" en Neon.
    #
    # La selección vive temporalmente en context.user_data.
    # La apuesta real se crea solamente al confirmar.
    # --------------------------------------------------------

    context.user_data["confirmation_bet"] = {
        "sport_key": active["sport_key"],
        "event_id": active["event_id"],
        "home_team": active["home"],
        "away_team": active["away"],
        "selection": active["selection"],
        "odds": odds,
        "stake": amount,
        "potential_return": potential_return,
    }

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ CONFIRMAR APUESTA",
                callback_data="confirm_bet",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ CANCELAR",
                callback_data="cancel_bet",
            )
        ],
    ]

    await update.message.reply_text(
        "🧾 CONFIRMAR APUESTA\n\n"
        f"⚽ {active['home']}\n"
        f"vs\n"
        f"⚽ {active['away']}\n\n"
        f"🎯 Selección: "
        f"{active['selection']}\n"
        f"📈 Cuota: {odds}\n"
        f"💰 Apuesta: "
        f"{format_money(amount)}\n"
        f"🏆 Retorno potencial: "
        f"{format_money(potential_return)}",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# CONFIRMAR APUESTA
# ============================================================

async def confirm_bet(
    query,
    context,
):

    pending = context.user_data.get(
        "confirmation_bet"
    )

    if not pending:

        await query.edit_message_text(
            "❌ La confirmación ya no está disponible."
        )

        return

    user_id, _ = get_or_create_user(
        query.from_user
    )

    with get_db() as conn:

        with conn.cursor() as cur:

            # ==================================================
            # BLOQUEAR USUARIO
            # ==================================================

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

                await query.edit_message_text(
                    "❌ Usuario no encontrado."
                )

                return

            balance = money(
                user_row[0]
            )

            stake = money(
                pending["stake"]
            )

            if balance < stake:

                await query.edit_message_text(
                    "❌ Saldo insuficiente.\n\n"
                    f"💰 Saldo actual: "
                    f"{format_money(balance)}"
                )

                return

            # ==================================================
            # CREAR APUESTA
            # ==================================================

            new_balance = money(
                balance - stake
            )

            cur.execute(
                """
                INSERT INTO bets (
                    user_id,
                    sport_key,
                    event_id,
                    home_team,
                    away_team,
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
                    %s,
                    %s,
                    %s,
                    %s,
                    'pending'
                )
                RETURNING id
                """,
                (
                    user_id,
                    pending["sport_key"],
                    pending["event_id"],
                    pending["home_team"],
                    pending["away_team"],
                    pending["selection"],
                    pending["odds"],
                    stake,
                    pending["potential_return"],
                ),
            )

            bet_id = cur.fetchone()[0]

            # ==================================================
            # DESCONTAR SALDO
            # ==================================================

            cur.execute(
                """
                UPDATE users
                SET
                    balance = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (
                    new_balance,
                    user_id,
                ),
            )

            # ==================================================
            # REGISTRAR TRANSACCIÓN
            # ==================================================

            cur.execute(
                """
                INSERT INTO transactions (
                    user_id,
                    type,
                    amount,
                    balance_before,
                    balance_after,
                    bet_id,
                    description
                )
                VALUES (
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
                    user_id,
                    TRANSACTION_BET,
                    -stake,
                    balance,
                    new_balance,
                    bet_id,
                    (
                        f"Apuesta #{bet_id} "
                        f"{pending['home_team']} "
                        f"vs "
                        f"{pending['away_team']} "
                        f"- "
                        f"{pending['selection']}"
                    ),
                ),
            )

        # ======================================================
        # APUESTA + SALDO + TRANSACCIÓN
        # TODO EN UNA SOLA TRANSACCIÓN.
        # ======================================================

        conn.commit()

    # --------------------------------------------------------
    # Limpiar selección temporal
    # --------------------------------------------------------

    context.user_data.pop(
        "confirmation_bet",
        None,
    )

    await query.edit_message_text(
        "✅ APUESTA CONFIRMADA\n\n"
        f"🧾 Apuesta #{bet_id}\n\n"
        f"⚽ {pending['home_team']}\n"
        f"vs\n"
        f"⚽ {pending['away_team']}\n\n"
        f"🎯 {pending['selection']}\n"
        f"📈 Cuota: {pending['odds']}\n"
        f"💰 Apuesta: "
        f"{format_money(pending['stake'])}\n"
        f"🏆 Retorno potencial: "
        f"{format_money(pending['potential_return'])}\n\n"
        "⏳ La apuesta queda pendiente "
        "hasta la liquidación del partido."
    )


# ============================================================
# CANCELAR APUESTA
# ============================================================

async def cancel_bet(
    query,
    context,
):

    context.user_data.pop(
        "confirmation_bet",
        None,
    )

    await query.answer(
        "Apuesta cancelada."
    )

    await query.edit_message_text(
        "❌ Apuesta cancelada."
    )


# ============================================================
# CALLBACK PRINCIPAL
# ============================================================

async def button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    data = query.data or ""

    # ========================================================
    # CONFIRMAR APUESTA DEL USUARIO
    # ========================================================

    if data == "confirm_bet":

        await query.answer()

        await confirm_bet(
            query,
            context,
        )

        return

    # ========================================================
    # CANCELAR APUESTA DEL USUARIO
    # ========================================================

    if data == "cancel_bet":

        await cancel_bet(
            query,
            context,
        )

        return

    # ========================================================
    # CONFIRMAR LIQUIDACIÓN INDIVIDUAL
    # ========================================================

    if data.startswith(
        "settle_confirm_bet:"
    ):

        bet_id = int(
            data.split(":", 1)[1]
        )

        await confirm_settlement(
            query,
            bet_id,
        )

        return

    # ========================================================
    # CANCELAR LIQUIDACIÓN
    # ========================================================

    if data.startswith(
        "settle_cancel:"
    ):

        bet_id = int(
            data.split(":", 1)[1]
        )

        await cancel_settlement(
            query,
            bet_id,
        )

        return

    # ========================================================
    # LIQUIDAR EVENTO COMPLETO
    # ========================================================

    if data.startswith(
        "settle_confirm_event:"
    ):

        event_id = data.split(
            ":", 1
        )[1]

        await confirm_settlement_event(
            query,
            event_id,
        )

        return

    # ========================================================
    # ADMIN
    # ========================================================

    if data == "admin_bets":

        await query.answer()

        await show_admin_bets(
            query
        )

        return

    if data.startswith(
        "admin_event:"
    ):

        await query.answer()

        event_id = data.split(
            ":", 1
        )[1]

        await show_admin_event(
            query,
            event_id,
        )

        return

    if data.startswith(
        "admin_verify_event:"
    ):

        event_id = data.split(
            ":", 1
        )[1]

        await verify_event_for_settlement(
            query,
            event_id,
        )

        return

    # ========================================================
    # DEPORTES
    # ========================================================

    if data.startswith(
        "sport:"
    ):

        await query.answer()

        sport_key = data.split(
            ":", 1
        )[1]

        await show_games(
            query,
            sport_key,
        )

        return

    # ========================================================
    # PARTIDO
    # ========================================================

    if data.startswith(
        "game:"
    ):

        await query.answer()

        parts = data.split(
            ":",
            2,
        )

        if len(parts) != 3:
            return

        sport_key = parts[1]
        event_id = parts[2]

        await show_game(
            query,
            sport_key,
            event_id,
        )

        return

    # ========================================================
    # SELECCIÓN
    # ========================================================

    if data.startswith(
        "pick:"
    ):

        await query.answer()

        pick_id = data.split(
            ":",
            1,
        )[1]

        active = active_picks.get(
            pick_id
        )

        if not active:

            await query.edit_message_text(
                "❌ Esta selección ya no está disponible."
            )

            return

        context.user_data[
            "active_bet"
        ] = active

        await query.edit_message_text(
            "💰 ¿Cuánto quieres apostar?\n\n"
            f"🎯 {active['selection']}\n"
            f"📈 Cuota: {active['odds']}"
        )

        return

    # ========================================================
    # VOLVER AL MENÚ
    # ========================================================

    if data == "back_main":

        await query.answer()

        await show_main_menu(
            query
        )

        return


# ============================================================
# RESULTADOS RECIENTES
# ============================================================

async def test_recent_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "⛔ No autorizado."
        )

        return

    if not context.args:

        await update.message.reply_text(
            "Uso:\n"
            "/resultados sport_key\n\n"
            "Ejemplo:\n"
            "/resultados soccer_uefa_nations_league"
        )

        return

    sport_key = context.args[0]

    try:

        url = (
            "https://api.the-odds-api.com/v4/"
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

        events = response.json()

        if not events:

            await update.message.reply_text(
                "📭 No se encontraron resultados."
            )

            return

        text = (
            "📊 RESULTADOS RECIENTES\n\n"
        )

        for event in events[:15]:

            home = event.get(
                "home_team",
                "?",
            )

            away = event.get(
                "away_team",
                "?",
            )

            completed = event.get(
                "completed"
            )

            text += (
                f"⚽ {home} vs {away}\n"
                f"ID: {event.get('id')}\n"
                f"Finalizado: {completed}\n\n"
            )

        await update.message.reply_text(
            text[:4000]
        )

    except Exception as e:

        await update.message.reply_text(
            f"❌ Error:\n{e}"
        )


# ============================================================
# RESULTADO INDIVIDUAL
# ============================================================

async def test_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "⛔ No autorizado."
        )

        return

    if len(context.args) < 2:

        await update.message.reply_text(
            "Uso:\n"
            "/resultado sport_key event_id"
        )

        return

    sport_key = context.args[0]
    event_id = context.args[1]

    try:

        event = get_odds_api_score_event(
            sport_key,
            event_id,
        )

        if not event:

            await update.message.reply_text(
                "The Odds API no encontró "
                "el evento."
            )

            return

        await update.message.reply_text(
            f"⚽ {event.get('home_team')}\n"
            f"vs\n"
            f"⚽ {event.get('away_team')}\n\n"
            f"🏁 Completado: "
            f"{event.get('completed')}\n\n"
            f"📊 Scores:\n"
            f"{event.get('scores')}"
        )

    except Exception as e:

        await update.message.reply_text(
            f"❌ Error:\n{e}"
        )


# ============================================================
# EVALUAR RESULTADO
# ============================================================

async def test_evaluate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "⛔ No autorizado."
        )

        return

    if len(context.args) < 3:

        await update.message.reply_text(
            "Uso:\n"
            "/evaluar sport_key event_id selección"
        )

        return

    sport_key = context.args[0]
    event_id = context.args[1]
    selection = " ".join(
        context.args[2:]
    )

    try:

        event = get_odds_api_score_event(
            sport_key,
            event_id,
        )

        if not event:

            await update.message.reply_text(
                "❌ Evento no encontrado "
                "en The Odds API."
            )

            return

        result = determine_h2h_result(
            event
        )

        won = evaluate_h2h_selection(
            event,
            selection,
        )

        await update.message.reply_text(
            f"📊 Resultado: {result}\n"
            f"🎯 Selección: {selection}\n"
            f"🏆 Ganada: {won}"
        )

    except Exception as e:

        await update.message.reply_text(
            f"❌ Error:\n{e}"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "Falta BOT_TOKEN"
        )

    if not ODDS_API_KEY:
        raise RuntimeError(
            "Falta ODDS_API_KEY"
        )

    if not DATABASE_URL:
        raise RuntimeError(
            "Falta DATABASE_URL"
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
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
