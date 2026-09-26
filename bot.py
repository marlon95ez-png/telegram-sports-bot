import os
import requests
import psycopg

from datetime import datetime, timedelta, timezone

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
# CONEXIÓN BASE DE DATOS
# ============================================================

def get_db():
    return psycopg.connect(DATABASE_URL)


# ============================================================
# ADMIN
# ============================================================

def is_admin(user_id):
    if not ADMIN_TELEGRAM_ID:
        return False

    return str(user_id) == str(ADMIN_TELEGRAM_ID)


# ============================================================
# INICIALIZAR BASE DE DATOS
# ============================================================

def init_db():

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    balance NUMERIC DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bets (
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
                    result TEXT,
                    status TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

                        # ========================================================
            # MIGRACIÓN DE LA TABLA BETS
            # ========================================================

            cur.execute("""
                ALTER TABLE bets
                ADD COLUMN IF NOT EXISTS event_id TEXT
            """)

            cur.execute("""
                ALTER TABLE bets
                ADD COLUMN IF NOT EXISTS event_name TEXT
            """)

            cur.execute("""
                ALTER TABLE bets
                ADD COLUMN IF NOT EXISTS sport TEXT
            """)

            cur.execute("""
                ALTER TABLE bets
                ADD COLUMN IF NOT EXISTS competition TEXT
            """)

            cur.execute("""
                ALTER TABLE bets
                ADD COLUMN IF NOT EXISTS selection TEXT
            """)

            cur.execute("""
                ALTER TABLE bets
                ADD COLUMN IF NOT EXISTS odds NUMERIC
            """)

            cur.execute("""
                ALTER TABLE bets
                ADD COLUMN IF NOT EXISTS stake NUMERIC
            """)

            cur.execute("""
                ALTER TABLE bets
                ADD COLUMN IF NOT EXISTS potential_return NUMERIC
            """)

            cur.execute("""
                ALTER TABLE bets
                ADD COLUMN IF NOT EXISTS result TEXT
            """)

            cur.execute("""
                ALTER TABLE bets
                ADD COLUMN IF NOT EXISTS status TEXT
            """)

            cur.execute("""
                ALTER TABLE bets
                ADD COLUMN IF NOT EXISTS created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    amount NUMERIC,
                    type TEXT,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ========================================================
            # MIGRACIÓN DE LA TABLA TRANSACTIONS
            # ========================================================

            cur.execute("""
                ALTER TABLE transactions
                ADD COLUMN IF NOT EXISTS user_id INTEGER
            """)

            cur.execute("""
                ALTER TABLE transactions
                ADD COLUMN IF NOT EXISTS amount NUMERIC
            """)

            cur.execute("""
                ALTER TABLE transactions
                ADD COLUMN IF NOT EXISTS type TEXT
            """)

            cur.execute("""
                ALTER TABLE transactions
                ADD COLUMN IF NOT EXISTS description TEXT
            """)

            cur.execute("""
                ALTER TABLE transactions
                ADD COLUMN IF NOT EXISTS created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    home_team TEXT,
                    away_team TEXT
                )
            """)

        conn.commit()


# ============================================================
# MIGRAR APUESTAS ANTIGUAS
# ============================================================

def migrate_legacy_pending_bets():

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    id,
                    user_id,
                    event_id,
                    event_name,
                    sport,
                    competition,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    created_at
                FROM bets
                WHERE status = 'Pendiente'
            """)

            rows = cur.fetchall()

            for row in rows:

                (
                    bet_id,
                    user_id,
                    event_id,
                    event_name,
                    sport,
                    competition,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    created_at,
                ) = row

                cur.execute("""
                    SELECT id
                    FROM pending_bets
                    WHERE user_id = %s
                    AND event_id = %s
                    AND selection = %s
                    AND stake = %s
                    LIMIT 1
                """, (
                    user_id,
                    event_id,
                    selection,
                    stake,
                ))

                exists = cur.fetchone()

                if exists:
                    continue

                home_team = ""
                away_team = ""

                if event_name and " vs " in event_name:
                    parts = event_name.split(" vs ", 1)
                    home_team = parts[0].strip()
                    away_team = parts[1].strip()

                cur.execute("""
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
                        created_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                """, (
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
                ))

                cur.execute("""
                    DELETE FROM bets
                    WHERE id = %s
                """, (bet_id,))

        conn.commit()


# ============================================================
# USUARIOS
# ============================================================

def get_or_create_user(telegram_user):

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT id, balance
                FROM users
                WHERE telegram_id = %s
            """, (telegram_user.id,))

            row = cur.fetchone()

            if row:
                return row

            cur.execute("""
                INSERT INTO users (
                    telegram_id,
                    username,
                    first_name,
                    balance
                )
                VALUES (%s, %s, %s, %s)
                RETURNING id, balance
            """, (
                telegram_user.id,
                telegram_user.username,
                telegram_user.first_name,
                0,
            ))

            row = cur.fetchone()

        conn.commit()

    return row


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
# EQUIPOS
# ============================================================

def normalize_team_name(name):

    if not name:
        return ""

    name = str(name).lower().strip()

    replacements = {
        " fc": "",
        " cf": "",
        " sc": "",
        " afc": "",
        "  ": " ",
    }

    for old, new in replacements.items():
        name = name.replace(old, new)

    return name.strip()


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

    if eh == ah and ea == aa:
        return True

    return False


# ============================================================
# RESULTADOS RECIENTES — THE ODDS API
# ============================================================

def get_recent_completed_events(sport_key):

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

    return response.json()


# ============================================================
# ESPN — MAPEO DE COMPETICIONES
# ============================================================

def get_espn_league(sport_key):

    sport_key = str(
        sport_key or ""
    ).strip().lower()

    mapping = {
        "soccer_uefa_nations_league": "uefa.nations",
    }

    return mapping.get(
        sport_key
    )


# ============================================================
# ESPN — NORMALIZAR NOMBRE DE EQUIPO
# ============================================================

def normalize_espn_team_name(name):

    if not name:
        return ""

    value = str(name).lower().strip()

    replacements = {
        "rep ireland": "republic of ireland",
        "republic of ireland": "ireland",
        "northern ireland": "northern ireland",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    value = " ".join(
        value.split()
    )

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

    if eh == ah and ea == aa:
        return True

    return False


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
            {}
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
        {}
    )

    status_type = status.get(
        "type",
        {}
    )

    completed = bool(
        status_type.get("completed")
    )

    converted = {
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

    return converted


# ============================================================
# ESPN — BUSCAR RESULTADO POR EQUIPOS
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

    print("========================================")
    print("ESPN SCORE FALLBACK")
    print("SPORT KEY:", sport_key)
    print("LEAGUE:", league)
    print("HOME:", home_team)
    print("AWAY:", away_team)

    base_url = (
        "https://site.api.espn.com/apis/site/v2/"
        f"sports/soccer/{league}/scoreboard"
    )

    # --------------------------------------------------------
    # Probamos hoy y ayer.
    #
    # El partido de Georgia vs Northern Ireland
    # se jugó hoy, por lo que la primera consulta
    # normalmente será suficiente.
    # --------------------------------------------------------

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

        print(
            "ESPN STATUS:",
            response.status_code,
        )

        if response.status_code != 200:

            print(
                "ESPN RESPONSE:",
                response.text[:2000],
            )

            continue

        try:

            data = response.json()

        except Exception as e:

            print(
                "ESPN JSON ERROR:",
                repr(e),
            )

            continue

        events = data.get(
            "events",
            []
        )

        print(
            "ESPN EVENTS:",
            len(events),
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

            print(
                "ESPN CHECK:",
                api_home,
                "vs",
                api_away,
                "| COMPLETED:",
                converted.get("completed"),
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

            print(
                "ESPN RESULT:",
                converted.get("scores"),
            )

            if not converted.get(
                "completed"
            ):

                print(
                    "ESPN MATCH FOUND "
                    "PERO AÚN NO ESTÁ COMPLETADO."
                )

                return converted

            return converted

    print(
        "ESPN FALLBACK: "
        "PARTIDO NO ENCONTRADO."
    )

    return None


# ============================================================
# RESULTADO DE UN EVENTO
# ============================================================

def get_event_result(
    sport_key,
    event_id,
    home_team=None,
    away_team=None,
):

    if not sport_key:
        print(
            "SCORES ERROR: SPORT KEY VACÍO"
        )
        return None

    sport_key = str(
        sport_key
    ).strip()

    event_id = str(
        event_id
    ).strip()

    url = (
        "https://api.the-odds-api.com/v4/"
        f"sports/{sport_key}/scores/"
    )

    params = {
        "apiKey": ODDS_API_KEY,
        "daysFrom": 3,
        "eventIds": event_id,
    }

    print("========================================")
    print("SCORES REQUEST")
    print("SPORT:", sport_key)
    print("EVENT ID:", event_id)
    print("HOME TEAM:", home_team)
    print("AWAY TEAM:", away_team)
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

        response = None

    # --------------------------------------------------------
    # THE ODDS API
    # --------------------------------------------------------

    if response is not None:

        print(
            "SCORES STATUS:",
            response.status_code,
        )

        print(
            "SCORES RESPONSE:",
            response.text[:5000],
        )

        if response.status_code == 404:

            print(
                "SCORES FILTERED REQUEST "
                "DEVOLVIÓ 404."
            )

            data = []

        elif response.status_code == 200:

            data = response.json()

        else:

            print(
                "SCORES REQUEST ERROR:",
                response.text[:2000],
            )

            data = []

    else:

        data = []

    # --------------------------------------------------------
    # BUSCAR POR EVENT ID
    # --------------------------------------------------------

    if data:

        for event in data:

            if str(
                event.get("id")
            ) == event_id:

                print(
                    "SCORES EVENT FOUND:",
                    event_id,
                )

                return event

    print(
        "SCORES EVENT NOT FOUND "
        "IN RESPONSE:",
        event_id,
    )

    # --------------------------------------------------------
    # FALLBACK POR EQUIPOS — ODDS API
    # --------------------------------------------------------

    if home_team and away_team:

        print(
            "SCORES FALLBACK: "
            "buscando por equipos..."
        )

        try:

            fallback_events = (
                get_recent_completed_events(
                    sport_key
                )
            )

        except Exception as e:

            print(
                "SCORES FALLBACK ERROR:",
                repr(e),
            )

            fallback_events = []

        for event in fallback_events:

            api_home = event.get(
                "home_team"
            )

            api_away = event.get(
                "away_team"
            )

            if teams_match(
                home_team,
                away_team,
                api_home,
                api_away,
            ):

                print(
                    "SCORES FALLBACK FOUND:",
                    event.get("id"),
                )

                return event

        print(
            "SCORES FALLBACK: "
            "PARTIDO NO ENCONTRADO "
            "POR EQUIPOS."
        )

    else:

        print(
            "SCORES FALLBACK OMITIDO: "
            "faltan equipos."
        )

    # ========================================================
    # NUEVO FALLBACK — ESPN
    # ========================================================

    if home_team and away_team:

        print(
            "SCORES FALLBACK 2: "
            "consultando ESPN..."
        )

        espn_event = get_espn_score_event(
            sport_key,
            home_team,
            away_team,
        )

        if espn_event:

            print(
                "SCORES FALLBACK 2 FOUND "
                "USANDO ESPN."
            )

            return espn_event

    return None


# ============================================================
# OBTENER SPORT KEY CORRECTO DE PENDING_BETS
# ============================================================

def get_pending_sport_key(row):

    """
    Estructura de pending_bets:

    [0] id
    [1] user_id
    [2] sport
    [3] competition
    [4] event_id
    [5] home_team
    [6] away_team
    [7] selection
    [8] odds
    [9] stake
    [10] potential_return
    [11] expires_at
    [12] created_at

    competition contiene el sport_key real
    de The Odds API.

    Ejemplo:

    soccer_uefa_nations_league
    """

    competition = row[3]
    sport = row[2]

    if competition:

        return str(
            competition
        ).strip()

    if sport:

        return str(
            sport
        ).strip()

    return None


# ============================================================
# RESULTADO H2H
# ============================================================

def determine_h2h_result(event):

    scores = event.get("scores")

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
        except:
            continue

        if name == home_team:

            home_score = value

        elif name == away_team:

            away_score = value

    if (
        home_score is None
        or away_score is None
    ):

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

        return (
            selection_normalized
            in [
                "draw",
                "empate",
            ]
        )

    return False


# ============================================================
# LIQUIDAR FILAS PENDIENTES
# ============================================================

def _settle_pending_rows(
    pending_rows,
    event,
):

    result = determine_h2h_result(
        event
    )

    if result is None:

        return {
            "success": False,
            "message": (
                "No fue posible determinar "
                "el resultado del partido."
            ),
        }

    event_id = event.get(
        "id"
    )

    home_team = event.get(
        "home_team"
    )

    away_team = event.get(
        "away_team"
    )

    users_affected = 0
    bets_won = 0
    bets_lost = 0
    total_paid = 0

    with get_db() as conn:

        with conn.cursor() as cur:

            # ------------------------------------------------
            # BLOQUEAR APUESTAS
            # ------------------------------------------------

            ids = [
                row[0]
                for row in pending_rows
            ]

            if not ids:

                return {
                    "success": False,
                    "message": (
                        "No hay apuestas "
                        "para liquidar."
                    ),
                }

            cur.execute("""
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
                WHERE id = ANY(%s)
                FOR UPDATE
            """, (ids,))

            locked_rows = cur.fetchall()

            if not locked_rows:

                return {
                    "success": False,
                    "message": (
                        "Las apuestas ya fueron "
                        "liquidadas."
                    ),
                }

            # ------------------------------------------------
            # PROCESAR APUESTAS
            # ------------------------------------------------

            for row in locked_rows:

                (
                    bet_id,
                    user_id,
                    sport,
                    competition,
                    row_event_id,
                    row_home,
                    row_away,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    expires_at,
                    created_at,
                ) = row

                won = evaluate_h2h_selection(
                    event,
                    selection,
                )

                cur.execute("""
                    SELECT balance
                    FROM users
                    WHERE id = %s
                    FOR UPDATE
                """, (user_id,))

                user_row = cur.fetchone()

                if not user_row:
                    continue

                current_balance = (
                    user_row[0] or 0
                )

                starting_balance = (
                    current_balance
                )

                if won:

                    new_balance = (
                        current_balance
                        + potential_return
                    )

                    bets_won += 1

                    total_paid += float(
                        potential_return
                    )

                    result_text = "Ganada"

                    cur.execute("""
                        UPDATE users
                        SET balance = %s
                        WHERE id = %s
                    """, (
                        new_balance,
                        user_id,
                    ))

                    cur.execute("""
                        INSERT INTO transactions (
                            user_id,
                            amount,
                            type,
                            description
                        )
                        VALUES (
                            %s,
                            %s,
                            %s,
                            %s
                        )
                    """, (
                        user_id,
                        potential_return,
                        "win",
                        (
                            f"Premio apuesta #{bet_id} "
                            f"{home_team} vs {away_team}"
                        ),
                    ))

                else:

                    new_balance = (
                        current_balance
                    )

                    bets_lost += 1

                    result_text = "Perdida"

                if (
                    current_balance
                    != starting_balance
                ):

                    users_affected += 1

                elif won:

                    users_affected += 1

                # ------------------------------------------------
                # GUARDAR HISTORIAL
                # ------------------------------------------------

                cur.execute("""
                    INSERT INTO bets (
                        user_id,
                        event_id,
                        event_name,
                        sport,
                        competition,
                        selection,
                        odds,
                        stake,
                        potential_return,
                        result,
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
                """, (
                    user_id,
                    row_event_id,
                    (
                        f"{row_home} vs "
                        f"{row_away}"
                    ),
                    sport,
                    competition,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    result_text,
                    "Liquidada",
                    created_at,
                ))

                # ------------------------------------------------
                # ELIMINAR DE PENDIENTES
                # ------------------------------------------------

                cur.execute("""
                    DELETE FROM pending_bets
                    WHERE id = %s
                """, (bet_id,))

        conn.commit()

    return {
        "success": True,
        "bets_won": bets_won,
        "bets_lost": bets_lost,
        "users_affected": users_affected,
        "total_paid": total_paid,
        "result": result,
        "home_team": home_team,
        "away_team": away_team,
    }


# ============================================================
# LIQUIDAR EVENTO COMPLETO
# ============================================================

def settle_event(event_id):

    with get_db() as conn:

        with conn.cursor() as cur:

            cur.execute("""
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
                ORDER BY id
            """, (event_id,))

            preview_rows = cur.fetchall()

    if not preview_rows:

        return {
            "success": False,
            "message": (
                "No hay apuestas pendientes "
                "para este partido."
            ),
        }

    sport_key = get_pending_sport_key(
        preview_rows[0]
    )

    if not sport_key:

        return {
            "success": False,
            "message": (
                "La apuesta no tiene un "
                "sport key válido."
            ),
        }

    home_team = preview_rows[0][5]
    away_team = preview_rows[0][6]

    print("========================================")
    print("SETTLE EVENT")
    print("EVENT ID:", event_id)
    print("SPORT KEY:", sport_key)
    print("HOME:", home_team)
    print("AWAY:", away_team)

    event = get_event_result(
        sport_key,
        event_id,
        home_team,
        away_team,
    )

    if not event:

        return {
            "success": False,
            "message": (
                "No se encontró el resultado "
                "del partido."
            ),
        }

    if not event.get("completed"):

        return {
            "success": False,
            "message": (
                "El partido todavía no aparece "
                "como terminado."
            ),
        }

    result = _settle_pending_rows(
        preview_rows,
        event,
    )

    return result


# ============================================================
# LIQUIDAR APUESTA INDIVIDUAL
# ============================================================

def settle_bet(bet_id):

    with get_db() as conn:

        with conn.cursor() as cur:

            cur.execute("""
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
            """, (bet_id,))

            pending_row = cur.fetchone()

    if not pending_row:

        return {
            "success": False,
            "message": (
                f"No existe la apuesta "
                f"pendiente #{bet_id}."
            ),
        }

    sport_key = get_pending_sport_key(
        pending_row
    )

    if not sport_key:

        return {
            "success": False,
            "message": (
                "La apuesta no tiene un "
                "sport key válido."
            ),
        }

    event_id = pending_row[4]
    home_team = pending_row[5]
    away_team = pending_row[6]

    print("========================================")
    print("SETTLE BET")
    print("BET ID:", bet_id)
    print("SPORT KEY:", sport_key)
    print("EVENT ID:", event_id)
    print("HOME:", home_team)
    print("AWAY:", away_team)
    print("========================================")

    event = get_event_result(
        sport_key,
        event_id,
        home_team,
        away_team,
    )

    if not event:

        return {
            "success": False,
            "message": (
                "No se encontró el resultado "
                "del partido."
            ),
        }

    if not event.get("completed"):

        return {
            "success": False,
            "message": (
                "El partido todavía no aparece "
                "como terminado."
            ),
        }

    result = _settle_pending_rows(
        [pending_row],
        event,
    )

    if not result.get("success"):

        return result

    won = (
        result["bets_won"] == 1
    )

    return {
        "success": True,
        "won": won,
        "result": result["result"],
        "home_team": result["home_team"],
        "away_team": result["away_team"],
        "total_paid": result["total_paid"],
    }


# ============================================================
# /LIQUIDAR
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
            "/liquidar 6"
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
        f"⏳ Liquidando apuesta pendiente "
        f"#{bet_id}..."
    )

    try:

        result = settle_bet(
            bet_id
        )

        if not result.get("success"):

            await update.message.reply_text(
                "❌ Error liquidando la apuesta:\n\n"
                + result.get(
                    "message",
                    "Error desconocido.",
                )
            )

            return

        status = (
            "✅ GANADA"
            if result["won"]
            else "❌ PERDIDA"
        )

        await update.message.reply_text(
            "🏆 APUESTA LIQUIDADA\n\n"
            f"⚽ {result['home_team']}\n"
            "vs\n"
            f"⚽ {result['away_team']}\n\n"
            f"📊 Resultado: "
            f"{result['result']}\n\n"
            f"🎯 Estado: {status}\n"
            f"💰 Pagado: "
            f"{result['total_paid']}"
        )

    except Exception as e:

        print(
            "ERROR EN /LIQUIDAR:",
            repr(e),
        )

        await update.message.reply_text(
            "❌ Error liquidando la apuesta:\n\n"
            f"{e}"
        )


# ============================================================
# MOSTRAR APUESTAS DEL ADMIN
# ============================================================

async def show_admin_bets(
    query,
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

            cur.execute("""
                SELECT
                    event_id,
                    home_team,
                    away_team,
                    COUNT(*)
                FROM pending_bets
                GROUP BY
                    event_id,
                    home_team,
                    away_team
                ORDER BY MIN(created_at)
            """)

            rows = cur.fetchall()

    if not rows:

        await query.edit_message_text(
            "📭 No hay apuestas pendientes."
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

            cur.execute("""
                SELECT
                    id,
                    user_id,
                    home_team,
                    away_team,
                    selection,
                    odds,
                    stake,
                    potential_return
                FROM pending_bets
                WHERE event_id = %s
                ORDER BY id
            """, (event_id,))

            rows = cur.fetchall()

    if not rows:

        await query.edit_message_text(
            "📭 No hay apuestas pendientes "
            "para este partido."
        )

        return

    home = rows[0][2]
    away = rows[0][3]

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
            user_id,
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
            f"Apuesta {stake}\n"
        )

    keyboard = [

        [
            InlineKeyboardButton(
                "💰 LIQUIDAR PARTIDO",
                callback_data=(
                    f"admin_settle_event:{event_id}"
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
# MOSTRAR APUESTA ADMIN
# ============================================================

async def show_admin_bet(
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

    with get_db() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    id,
                    event_id,
                    home_team,
                    away_team,
                    selection,
                    odds,
                    stake,
                    potential_return
                FROM pending_bets
                WHERE id = %s
            """, (bet_id,))

            row = cur.fetchone()

    if not row:

        await query.edit_message_text(
            "❌ Apuesta no encontrada."
        )

        return

    (
        bet_id,
        event_id,
        home,
        away,
        selection,
        odds,
        stake,
        potential_return,
    ) = row

    keyboard = [

        [
            InlineKeyboardButton(
                "💰 LIQUIDAR APUESTA",
                callback_data=(
                    f"admin_settle_bet:{bet_id}"
                ),
            )
        ],

        [
            InlineKeyboardButton(
                "🔙 Volver",
                callback_data=(
                    f"admin_event:{event_id}"
                ),
            )
        ],
    ]

    await query.edit_message_text(
        (
            f"🧾 APUESTA #{bet_id}\n\n"
            f"⚽ {home}\n"
            f"vs\n"
            f"⚽ {away}\n\n"
            f"🎯 Selección: {selection}\n"
            f"📈 Cuota: {odds}\n"
            f"💰 Apuesta: {stake}\n"
            f"🏆 Retorno: {potential_return}"
        ),
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# LIQUIDAR EVENTO DESDE BOTÓN
# ============================================================

async def admin_settle_event(
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
        "Liquidando partido..."
    )

    try:

        result = settle_event(
            event_id
        )

        if not result.get("success"):

            await query.edit_message_text(
                "❌ No se pudo liquidar:\n\n"
                + result.get(
                    "message",
                    "Error desconocido.",
                )
            )

            return

        await query.edit_message_text(
            "🏆 PARTIDO LIQUIDADO\n\n"
            f"⚽ {result['home_team']}\n"
            "vs\n"
            f"⚽ {result['away_team']}\n\n"
            f"📊 Resultado: "
            f"{result['result']}\n\n"
            f"✅ Ganadas: "
            f"{result['bets_won']}\n"
            f"❌ Perdidas: "
            f"{result['bets_lost']}\n"
            f"💰 Total pagado: "
            f"{result['total_paid']}"
        )

    except Exception as e:

        print(
            "ERROR ADMIN SETTLE EVENT:",
            repr(e),
        )

        await query.edit_message_text(
            "❌ Error liquidando el partido:\n\n"
            f"{e}"
        )


# ============================================================
# LIQUIDAR APUESTA DESDE BOTÓN
# ============================================================

async def admin_settle_bet(
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
        "Liquidando apuesta..."
    )

    try:

        result = settle_bet(
            bet_id
        )

        if not result.get("success"):

            await query.edit_message_text(
                "❌ No se pudo liquidar:\n\n"
                + result.get(
                    "message",
                    "Error desconocido.",
                )
            )

            return

        status = (
            "✅ GANADA"
            if result["won"]
            else "❌ PERDIDA"
        )

        await query.edit_message_text(
            "🏆 APUESTA LIQUIDADA\n\n"
            f"⚽ {result['home_team']}\n"
            "vs\n"
            f"⚽ {result['away_team']}\n\n"
            f"📊 Resultado: "
            f"{result['result']}\n\n"
            f"🎯 {status}\n"
            f"💰 Pagado: "
            f"{result['total_paid']}"
        )

    except Exception as e:

        print(
            "ERROR ADMIN SETTLE BET:",
            repr(e),
        )

        await query.edit_message_text(
            "❌ Error:\n\n"
            f"{e}"
        )


# ============================================================
# MENÚ PRINCIPAL
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.effective_user:
        return

    get_or_create_user(
        update.effective_user
    )

    keyboard = [

        [
            InlineKeyboardButton(
                "🇪🇸 LaLiga",
                callback_data=(
                    "sport:soccer_spain_la_liga"
                ),
            )
        ],

        [
            InlineKeyboardButton(
                "🏴 Premier League",
                callback_data=(
                    "sport:soccer_epl"
                ),
            )
        ],

        [
            InlineKeyboardButton(
                "🇮🇹 Serie A",
                callback_data=(
                    "sport:soccer_italy_serie_a"
                ),
            )
        ],

        [
            InlineKeyboardButton(
                "🇩🇪 Bundesliga",
                callback_data=(
                    "sport:soccer_germany_bundesliga"
                ),
            )
        ],

        [
            InlineKeyboardButton(
                "🇫🇷 Ligue 1",
                callback_data=(
                    "sport:soccer_france_ligue_one"
                ),
            )
        ],

        [
            InlineKeyboardButton(
                "🏆 UEFA Nations League",
                callback_data=(
                    "sport:soccer_uefa_nations_league"
                ),
            )
        ],
    ]

    if is_admin(
        update.effective_user.id
    ):

        keyboard.append([
            InlineKeyboardButton(
                "🛠 ADMIN - APUESTAS PENDIENTES",
                callback_data="admin_bets",
            )
        ])

    await update.message.reply_text(
        "🏆 CUBA SPORTS\n\n"
        "Selecciona una competición:",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
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

    for bookmaker in bookmakers:

        markets = bookmaker.get(
            "markets",
            [],
        )

        for market in markets:

            if market.get(
                "key"
            ) != "h2h":

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

                if (
                    name is None
                    or price is None
                ):

                    continue

                pick_id = (
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

    keyboard.append([
        InlineKeyboardButton(
            "🔙 Volver",
            callback_data=(
                f"sport:{sport_key}"
            ),
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
# MANEJAR CALLBACKS
# ============================================================

async def button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = query.data or ""

    # --------------------------------------------------------
    # DEPORTE
    # --------------------------------------------------------

    if data.startswith(
        "sport:"
    ):

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

    if data.startswith(
        "game:"
    ):

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

    # --------------------------------------------------------
    # PICK
    # --------------------------------------------------------

    if data.startswith(
        "pick:"
    ):

        pick_id = data.split(
            ":",
            1,
        )[1]

        active = pending_bets.get(
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
            (
                "💰 ¿Cuánto quieres apostar?\n\n"
                f"🎯 {active['selection']}\n"
                f"📈 Cuota: {active['odds']}"
            )
        )

        return

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------

    if data == "admin_bets":

        await show_admin_bets(
            query
        )

        return

    # --------------------------------------------------------

    if data.startswith(
        "admin_event:"
    ):

        event_id = data.split(
            ":",
            1,
        )[1]

        await show_admin_event(
            query,
            event_id,
        )

        return

    # --------------------------------------------------------

    if data.startswith(
        "admin_settle_event:"
    ):

        event_id = data.split(
            ":",
            1,
        )[1]

        await admin_settle_event(
            query,
            event_id,
        )

        return

    # --------------------------------------------------------

    if data.startswith(
        "admin_bet:"
    ):

        bet_id = data.split(
            ":",
            1,
        )[1]

        await show_admin_bet(
            query,
            int(bet_id),
        )

        return

    # --------------------------------------------------------

    if data.startswith(
        "admin_settle_bet:"
    ):

        bet_id = data.split(
            ":",
            1,
        )[1]

        await admin_settle_bet(
            query,
            int(bet_id),
        )

        return

    # --------------------------------------------------------
    # VOLVER
    # --------------------------------------------------------

    if data == "back_main":

        keyboard = [

            [
                InlineKeyboardButton(
                    "🇪🇸 LaLiga",
                    callback_data=(
                        "sport:soccer_spain_la_liga"
                    ),
                )
            ],

            [
                InlineKeyboardButton(
                    "🏴 Premier League",
                    callback_data=(
                        "sport:soccer_epl"
                    ),
                )
            ],

            [
                InlineKeyboardButton(
                    "🇮🇹 Serie A",
                    callback_data=(
                        "sport:soccer_italy_serie_a"
                    ),
                )
            ],

            [
                InlineKeyboardButton(
                    "🇩🇪 Bundesliga",
                    callback_data=(
                        "sport:soccer_germany_bundesliga"
                    ),
                )
            ],

            [
                InlineKeyboardButton(
                    "🇫🇷 Ligue 1",
                    callback_data=(
                        "sport:soccer_france_ligue_one"
                    ),
                )
            ],

            [
                InlineKeyboardButton(
                    "🏆 UEFA Nations League",
                    callback_data=(
                        "sport:soccer_uefa_nations_league"
                    ),
                )
            ],
        ]

        if is_admin(
            query.from_user.id
        ):

            keyboard.append([
                InlineKeyboardButton(
                    "🛠 ADMIN - APUESTAS PENDIENTES",
                    callback_data="admin_bets",
                )
            ])

        await query.edit_message_text(
            "🏆 CUBA SPORTS\n\n"
            "Selecciona una competición:",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            ),
        )

        return


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

        amount = float(
            update.message.text
            .replace(",", ".")
            .strip()
        )

    except:

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

    if balance < amount:

        await update.message.reply_text(
            (
                "❌ Saldo insuficiente.\n\n"
                f"💰 Saldo actual: {balance}"
            )
        )

        return

    event_name = (
        f"{active['home']} vs "
        f"{active['away']}"
    )

    potential_return = (
        amount * float(
            active["odds"]
        )
    )

    with get_db() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO unconfirmed_bets (
                    user_id,
                    event_id,
                    event_name,
                    sport,
                    competition,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    home_team,
                    away_team
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                RETURNING id
            """, (
                user_id,
                active["event_id"],
                event_name,
                "football",
                active["sport_key"],
                active["selection"],
                active["odds"],
                amount,
                potential_return,
                active["home"],
                active["away"],
            ))

            unconfirmed_id = (
                cur.fetchone()[0]
            )

        conn.commit()

    context.user_data[
        "unconfirmed_id"
    ] = unconfirmed_id

    keyboard = [

        [
            InlineKeyboardButton(
                "✅ CONFIRMAR APUESTA",
                callback_data=(
                    f"confirm:{unconfirmed_id}"
                ),
            )
        ],

        [
            InlineKeyboardButton(
                "❌ CANCELAR",
                callback_data=(
                    f"cancel:{unconfirmed_id}"
                ),
            )
        ],
    ]

    await update.message.reply_text(
        (
            "🧾 CONFIRMAR APUESTA\n\n"
            f"⚽ {active['home']}\n"
            f"vs\n"
            f"⚽ {active['away']}\n\n"
            f"🎯 Selección: "
            f"{active['selection']}\n"
            f"📈 Cuota: {active['odds']}\n"
            f"💰 Apuesta: {amount}\n"
            f"🏆 Retorno potencial: "
            f"{potential_return:.2f}"
        ),
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

    user_id, _ = get_or_create_user(
        query.from_user
    )

    with get_db() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    id,
                    user_id,
                    event_id,
                    event_name,
                    sport,
                    competition,
                    selection,
                    odds,
                    stake,
                    potential_return,
                    home_team,
                    away_team
                FROM unconfirmed_bets
                WHERE id = %s
                AND user_id = %s
            """, (
                unconfirmed_id,
                user_id,
            ))

            row = cur.fetchone()

            if not row:

                await query.edit_message_text(
                    "❌ La apuesta ya no está disponible."
                )

                return

            (
                unconfirmed_db_id,
                db_user_id,
                event_id,
                event_name,
                sport,
                competition,
                selection,
                odds,
                stake,
                potential_return,
                home_team,
                away_team,
            ) = row

            # -----------------------------------------------
            # BLOQUEAR SALDO
            # -----------------------------------------------

            cur.execute("""
                SELECT balance
                FROM users
                WHERE id = %s
                FOR UPDATE
            """, (db_user_id,))

            user_row = cur.fetchone()

            if not user_row:

                await query.edit_message_text(
                    "❌ Usuario no encontrado."
                )

                return

            balance = (
                user_row[0] or 0
            )

            if balance < stake:

                await query.edit_message_text(
                    "❌ Saldo insuficiente."
                )

                return

            new_balance = (
                balance - stake
            )

            cur.execute("""
                UPDATE users
                SET balance = %s
                WHERE id = %s
            """, (
                new_balance,
                db_user_id,
            ))

            # -----------------------------------------------
            # TRANSACCIÓN
            # -----------------------------------------------

            cur.execute("""
                INSERT INTO transactions (
                    user_id,
                    amount,
                    type,
                    description
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s
                )
            """, (
                db_user_id,
                -stake,
                "bet",
                (
                    f"Apuesta {event_name} "
                    f"- {selection}"
                ),
            ))

            # -----------------------------------------------
            # GUARDAR APUESTA PENDIENTE
            # -----------------------------------------------

            cur.execute("""
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
                    potential_return
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
            """, (
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
            ))

            # -----------------------------------------------
            # ELIMINAR CONFIRMACIÓN
            # -----------------------------------------------

            cur.execute("""
                DELETE FROM unconfirmed_bets
                WHERE id = %s
            """, (
                unconfirmed_db_id,
            ))

        conn.commit()

    await query.edit_message_text(
        (
            "✅ APUESTA CONFIRMADA\n\n"
            f"⚽ {home_team}\n"
            f"vs\n"
            f"⚽ {away_team}\n\n"
            f"🎯 {selection}\n"
            f"📈 Cuota: {odds}\n"
            f"💰 Apuesta: {stake}\n"
            f"🏆 Retorno potencial: "
            f"{potential_return:.2f}\n\n"
            "⏳ La apuesta queda pendiente "
            "hasta la liquidación del partido."
        )
    )


# ============================================================
# CANCELAR APUESTA
# ============================================================

async def cancel_bet(
    query,
    unconfirmed_id,
):

    user_id, _ = get_or_create_user(
        query.from_user
    )

    with get_db() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                DELETE FROM unconfirmed_bets
                WHERE id = %s
                AND user_id = %s
            """, (
                unconfirmed_id,
                user_id,
            ))

        conn.commit()

    await query.edit_message_text(
        "❌ Apuesta cancelada."
    )


# ============================================================
# CALLBACKS EXTRA PARA CONFIRMAR / CANCELAR
# ============================================================

async def handle_confirmation_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = query.data or ""

    if data.startswith(
        "confirm:"
    ):

        unconfirmed_id = int(
            data.split(":", 1)[1]
        )

        await confirm_bet(
            query,
            unconfirmed_id,
        )

        return True

    if data.startswith(
        "cancel:"
    ):

        unconfirmed_id = int(
            data.split(":", 1)[1]
        )

        await cancel_bet(
            query,
            unconfirmed_id,
        )

        return True

    return False


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

        events = get_recent_completed_events(
            sport_key
        )

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
                "?"
            )

            away = event.get(
                "away_team",
                "?"
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

        event = get_event_result(
            sport_key,
            event_id,
        )

        if not event:

            await update.message.reply_text(
                "❌ Evento no encontrado."
            )

            return

        await update.message.reply_text(
            (
                f"⚽ {event.get('home_team')}\n"
                f"vs\n"
                f"⚽ {event.get('away_team')}\n\n"
                f"🏁 Completado: "
                f"{event.get('completed')}\n\n"
                f"📊 Scores:\n"
                f"{event.get('scores')}"
            )
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

        event = get_event_result(
            sport_key,
            event_id,
        )

        if not event:

            await update.message.reply_text(
                "❌ Evento no encontrado."
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
            (
                f"📊 Resultado: {result}\n"
                f"🎯 Selección: {selection}\n"
                f"🏆 Ganada: {won}"
            )
        )

    except Exception as e:

        await update.message.reply_text(
            f"❌ Error:\n{e}"
        )


# ============================================================
# CALLBACK ROUTER FINAL
# ============================================================

async def callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    data = (
        update.callback_query.data
        or ""
    )

    if (
        data.startswith("confirm:")
        or data.startswith("cancel:")
    ):

        handled = (
            await handle_confirmation_callback(
                update,
                context,
            )
        )

        if handled:
            return

    await button(
        update,
        context,
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

    init_db()

    migrate_legacy_pending_bets()

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
            callback_router
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
