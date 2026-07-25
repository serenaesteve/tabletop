import csv
import io
import json
import sqlite3
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, g,
    jsonify, Response
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, login_required, current_user
)
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import requests
except ImportError:
    requests = None

app = Flask(__name__)
app.secret_key = "cambia-esto-por-algo-secreto"
csrf = CSRFProtect(app)

DATABASE = "instance/tabletop.db"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3"
K_FACTOR = 32
BASE_ELO = 1200

OLLAMA_CACHE = {}  # cache en memoria: clave -> texto de recomendacion

SAMPLE_GAMES = [
    ("Catan", 3, 4, 90, 3, "Clasico de comercio y construccion."),
    ("Azul", 2, 4, 45, 2, "Colocacion de losetas, ligero y visual."),
    ("Codenames", 4, 8, 20, 1, "Party game de palabras, ideal para grupos grandes."),
    ("Dixit", 3, 6, 30, 1, "Narrativo con cartas ilustradas."),
    ("Coup", 2, 6, 20, 2, "Faroleo y deduccion, partidas rapidas."),
]


# ---------- DB helpers ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with app.app_context():
        db = get_db()
        with app.open_resource("schema.sql") as f:
            db.executescript(f.read().decode("utf8"))
        db.commit()


# ---------- Auth (Flask-Login) ----------

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Tienes que iniciar sesión primero."
login_manager.login_message_category = "error"


class User(UserMixin):
    def __init__(self, row):
        self.id = str(row["id"])
        self.username = row["username"]
        self.elo = row["elo"]


@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return User(row) if row else None


# ---------- Elo logic (multijugador por pares) ----------

def expected_score(elo_a, elo_b):
    return 1 / (1 + 10 ** ((elo_b - elo_a) / 400))


def compute_multiplayer_elo(players):
    """
    players: lista de dicts {"user_id": int, "elo": float, "rank": int}
    rank: 1 = ganador, numeros mas altos = peor puesto, iguales = empate.
    Devuelve dict user_id -> nuevo_elo
    """
    n = len(players)
    if n < 2:
        return {p["user_id"]: p["elo"] for p in players}

    new_elos = {}
    for i, p_i in enumerate(players):
        delta_sum = 0
        for j, p_j in enumerate(players):
            if i == j:
                continue
            exp_ij = expected_score(p_i["elo"], p_j["elo"])
            if p_i["rank"] < p_j["rank"]:
                actual_ij = 1
            elif p_i["rank"] > p_j["rank"]:
                actual_ij = 0
            else:
                actual_ij = 0.5
            delta_sum += (actual_ij - exp_ij)
        new_elo = p_i["elo"] + (K_FACTOR * delta_sum / (n - 1))
        new_elos[p_i["user_id"]] = round(new_elo, 1)
    return new_elos


def recompute_all_elo():
    """
    Recalcula desde cero el Elo global y el Elo por juego de todo el mundo,
    repasando las partidas en orden cronologico. Se llama tras crear, editar
    o borrar cualquier partida para que todo quede siempre consistente.
    """
    db = get_db()
    user_ids = [r["id"] for r in db.execute("SELECT id FROM users").fetchall()]
    current_global = {uid: BASE_ELO for uid in user_ids}
    current_game = {}  # (user_id, game_id) -> elo

    matches = db.execute(
        "SELECT * FROM matches ORDER BY played_at ASC, id ASC"
    ).fetchall()

    for m in matches:
        mps = db.execute(
            "SELECT * FROM match_players WHERE match_id = ? ORDER BY rank",
            (m["id"],),
        ).fetchall()
        if not mps:
            continue

        players_global = [
            {"user_id": mp["user_id"], "elo": current_global.get(mp["user_id"], BASE_ELO), "rank": mp["rank"]}
            for mp in mps
        ]
        new_global = compute_multiplayer_elo(players_global)

        players_game = []
        for mp in mps:
            key = (mp["user_id"], m["game_id"])
            current_game.setdefault(key, BASE_ELO)
            players_game.append({"user_id": mp["user_id"], "elo": current_game[key], "rank": mp["rank"]})
        new_game = compute_multiplayer_elo(players_game)

        for mp in mps:
            elo_before = current_global.get(mp["user_id"], BASE_ELO)
            elo_after = new_global[mp["user_id"]]
            db.execute(
                "UPDATE match_players SET elo_before = ?, elo_after = ? WHERE id = ?",
                (elo_before, elo_after, mp["id"]),
            )
            current_global[mp["user_id"]] = elo_after
            key = (mp["user_id"], m["game_id"])
            current_game[key] = new_game[mp["user_id"]]

    for uid, elo in current_global.items():
        db.execute("UPDATE users SET elo = ? WHERE id = ?", (elo, uid))

    for (uid, gid), elo in current_game.items():
        db.execute(
            """INSERT INTO game_elo (user_id, game_id, elo) VALUES (?, ?, ?)
               ON CONFLICT(user_id, game_id) DO UPDATE SET elo = excluded.elo""",
            (uid, gid, elo),
        )

    db.commit()


def get_recent_form(user_id, limit=5):
    """Devuelve lista cronologica de 'V' (victoria) o 'D' (derrota/resto)."""
    db = get_db()
    rows = db.execute(
        """SELECT mp.rank,
                  (SELECT MIN(rank) FROM match_players mp3 WHERE mp3.match_id = mp.match_id) AS best_rank
           FROM match_players mp
           JOIN matches m ON m.id = mp.match_id
           WHERE mp.user_id = ?
           ORDER BY m.played_at DESC, m.id DESC
           LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    form = ["V" if r["rank"] == r["best_rank"] else "D" for r in rows]
    return list(reversed(form))


def current_streak(form):
    streak = 0
    for result in reversed(form):
        if result == "V":
            streak += 1
        else:
            break
    return streak


# ---------- Ollama helper ----------

def ask_ollama(prompt, fallback="No he podido pensar una recomendación con IA ahora mismo, pero mira las opciones de arriba."):
    if requests is None:
        return fallback
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("response", fallback).strip()
        return fallback
    except Exception:
        return fallback


# ---------- Auth routes ----------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        db = get_db()
        error = None
        if not username or not password:
            error = "Usuario y contraseña son obligatorios."
        elif db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
            error = "Ese usuario ya existe."

        if error:
            flash(error, "error")
        else:
            db.execute(
                "INSERT INTO users (username, password_hash, elo) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), BASE_ELO),
            )
            db.commit()
            flash("Cuenta creada. Ya puedes iniciar sesión.", "success")
            return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        remember = bool(request.form.get("remember"))
        db = get_db()
        user_row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user_row is None or not check_password_hash(user_row["password_hash"], password):
            flash("Usuario o contraseña incorrectos.", "error")
        else:
            login_user(User(user_row), remember=remember)
            return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------- Dashboard ----------

@app.route("/")
@login_required
def dashboard():
    db = get_db()
    recent_matches = db.execute(
        """
        SELECT m.id, m.played_at, g.name AS game_name,
               GROUP_CONCAT(u.username || ' (#' || mp.rank || ')') AS players
        FROM matches m
        JOIN games g ON g.id = m.game_id
        JOIN match_players mp ON mp.match_id = m.id
        JOIN users u ON u.id = mp.user_id
        GROUP BY m.id
        ORDER BY m.played_at DESC, m.id DESC
        LIMIT 8
        """
    ).fetchall()

    top_players = db.execute(
        "SELECT id, username, elo FROM users ORDER BY elo DESC LIMIT 5"
    ).fetchall()

    total_matches = db.execute("SELECT COUNT(*) AS c FROM matches").fetchone()["c"]
    total_games = db.execute("SELECT COUNT(*) AS c FROM games").fetchone()["c"]

    return render_template(
        "dashboard.html",
        recent_matches=recent_matches,
        top_players=top_players,
        total_matches=total_matches,
        total_games=total_games,
    )


@app.route("/games/seed", methods=["POST"])
@login_required
def games_seed():
    db = get_db()
    existing = db.execute("SELECT COUNT(*) AS c FROM games").fetchone()["c"]
    if existing == 0:
        for name, mn, mx, dur, cx, notes in SAMPLE_GAMES:
            db.execute(
                """INSERT INTO games (name, min_players, max_players, avg_duration_minutes, complexity, notes, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, mn, mx, dur, cx, notes, int(current_user.id)),
            )
        db.commit()
        flash("Ludoteca de ejemplo cargada. ¡Edítala a tu gusto!", "success")
    return redirect(url_for("games_list"))


# ---------- Games ----------

@app.route("/games")
@login_required
def games_list():
    db = get_db()
    games = db.execute("SELECT * FROM games ORDER BY name").fetchall()
    return render_template("games.html", games=games)


@app.route("/games/new", methods=["GET", "POST"])
@login_required
def games_new():
    if request.method == "POST":
        db = get_db()
        db.execute(
            """INSERT INTO games (name, min_players, max_players, avg_duration_minutes, complexity, notes, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                request.form["name"].strip(),
                int(request.form["min_players"]),
                int(request.form["max_players"]),
                int(request.form["avg_duration_minutes"]),
                int(request.form["complexity"]),
                request.form.get("notes", "").strip(),
                int(current_user.id),
            ),
        )
        db.commit()
        flash("Juego añadido a la ludoteca.", "success")
        return redirect(url_for("games_list"))
    return render_template("game_form.html")


# ---------- Matches ----------

@app.route("/matches")
@login_required
def matches_list():
    db = get_db()
    matches = db.execute(
        """
        SELECT m.id, m.played_at, m.duration_minutes, g.name AS game_name
        FROM matches m JOIN games g ON g.id = m.game_id
        ORDER BY m.played_at DESC, m.id DESC
        """
    ).fetchall()
    return render_template("matches.html", matches=matches)


@app.route("/matches/new", methods=["GET", "POST"])
@login_required
def matches_new():
    db = get_db()
    games = db.execute("SELECT * FROM games ORDER BY name").fetchall()
    users = db.execute("SELECT * FROM users ORDER BY username").fetchall()

    if request.method == "POST":
        game_id = int(request.form["game_id"])
        played_at = request.form["played_at"]
        duration_minutes = request.form.get("duration_minutes") or None
        notes = request.form.get("notes", "").strip()

        player_ids = request.form.getlist("player_id")
        ranks = request.form.getlist("rank")
        scores = request.form.getlist("score")

        if len(player_ids) < 2:
            flash("Necesitas al menos 2 jugadores para registrar una partida.", "error")
            return render_template("match_form.html", games=games, users=users)

        cur = db.execute(
            "INSERT INTO matches (game_id, played_at, duration_minutes, notes) VALUES (?, ?, ?, ?)",
            (game_id, played_at, duration_minutes, notes),
        )
        match_id = cur.lastrowid

        for uid, rank, score in zip(player_ids, ranks, scores):
            db.execute(
                """INSERT INTO match_players (match_id, user_id, rank, score, elo_before, elo_after)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (match_id, int(uid), int(rank), int(score) if score else None, BASE_ELO, BASE_ELO),
            )
        db.commit()

        # Recalcula todo el historial para que el Elo (global y por juego) quede consistente
        recompute_all_elo()

        flash("Partida registrada y Elo actualizado.", "success")
        return redirect(url_for("matches_list"))

    return render_template("match_form.html", games=games, users=users)


@app.route("/matches/<int:match_id>")
@login_required
def match_detail(match_id):
    db = get_db()
    match = db.execute(
        """SELECT m.*, g.name AS game_name FROM matches m
           JOIN games g ON g.id = m.game_id WHERE m.id = ?""",
        (match_id,),
    ).fetchone()
    players = db.execute(
        """SELECT mp.*, u.username FROM match_players mp
           JOIN users u ON u.id = mp.user_id
           WHERE mp.match_id = ? ORDER BY mp.rank""",
        (match_id,),
    ).fetchall()
    return render_template("match_detail.html", match=match, players=players)


@app.route("/matches/<int:match_id>/delete", methods=["POST"])
@login_required
def match_delete(match_id):
    db = get_db()
    db.execute("DELETE FROM match_players WHERE match_id = ?", (match_id,))
    db.execute("DELETE FROM matches WHERE id = ?", (match_id,))
    db.commit()
    recompute_all_elo()
    flash("Partida eliminada y Elo recalculado.", "success")
    return redirect(url_for("matches_list"))


# ---------- Ranking ----------

@app.route("/ranking")
@login_required
def ranking():
    db = get_db()
    game_id = request.args.get("game_id", type=int)
    games = db.execute("SELECT id, name FROM games ORDER BY name").fetchall()

    if game_id:
        players = db.execute(
            """SELECT u.id, u.username, ge.elo AS elo
               FROM game_elo ge JOIN users u ON u.id = ge.user_id
               WHERE ge.game_id = ?
               ORDER BY ge.elo DESC""",
            (game_id,),
        ).fetchall()
        selected_game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    else:
        players = db.execute("SELECT id, username, elo FROM users ORDER BY elo DESC").fetchall()
        selected_game = None

    forms = {}
    for p in players:
        form = get_recent_form(p["id"])
        forms[p["id"]] = {"form": form, "streak": current_streak(form)}

    return render_template(
        "ranking.html", players=players, games=games, selected_game=selected_game, forms=forms
    )


@app.route("/players/<int:user_id>")
@login_required
def player_profile(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    history = db.execute(
        """SELECT mp.elo_after, m.played_at, g.name AS game_name, mp.rank
           FROM match_players mp
           JOIN matches m ON m.id = mp.match_id
           JOIN games g ON g.id = m.game_id
           WHERE mp.user_id = ?
           ORDER BY m.played_at ASC, m.id ASC""",
        (user_id,),
    ).fetchall()
    game_elos = db.execute(
        """SELECT g.name AS game_name, ge.elo AS elo
           FROM game_elo ge JOIN games g ON g.id = ge.game_id
           WHERE ge.user_id = ?
           ORDER BY ge.elo DESC""",
        (user_id,),
    ).fetchall()
    form = get_recent_form(user_id)
    streak = current_streak(form)
    return render_template(
        "player_profile.html", user=user, history=history, game_elos=game_elos,
        form=form, streak=streak,
    )


# ---------- Recomendador ----------

COMPLEXITY_RANGES = {
    "ligero": (1, 2),
    "medio": (3, 3),
    "pesado": (4, 5),
    "cualquiera": (1, 5),
}


@app.route("/recommend", methods=["GET", "POST"])
@login_required
def recommend():
    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY username").fetchall()
    recommendations = None
    ai_blurb = None
    selected_players = []
    minutes = None
    complexity_filter = "cualquiera"
    priorizar = "variedad"

    if request.method == "POST":
        player_ids = [int(x) for x in request.form.getlist("player_id")]
        minutes = int(request.form["minutes"])
        complexity_filter = request.form.get("complexity", "cualquiera")
        priorizar = request.form.get("priorizar", "variedad")
        n_players = len(player_ids)
        selected_players = [u["username"] for u in users if u["id"] in player_ids]
        cx_min, cx_max = COMPLEXITY_RANGES.get(complexity_filter, (1, 5))

        candidate_games = db.execute(
            """SELECT * FROM games
               WHERE min_players <= ? AND max_players >= ? AND avg_duration_minutes <= ?
               AND complexity BETWEEN ? AND ?""",
            (n_players, n_players, minutes, cx_min, cx_max),
        ).fetchall()

        scored = []
        for game in candidate_games:
            placeholders = ",".join("?" for _ in player_ids)
            last_played = db.execute(
                f"""SELECT MAX(m.played_at) AS last_date
                    FROM matches m JOIN match_players mp ON mp.match_id = m.id
                    WHERE m.game_id = ? AND mp.user_id IN ({placeholders})""",
                (game["id"], *player_ids),
            ).fetchone()["last_date"]

            avg_group_elo = None
            if player_ids:
                elo_rows = db.execute(
                    f"""SELECT elo FROM game_elo WHERE game_id = ? AND user_id IN ({placeholders})""",
                    (game["id"], *player_ids),
                ).fetchall()
                elos = [r["elo"] for r in elo_rows]
                if elos:
                    avg_group_elo = sum(elos) / len(elos)

            scored.append({"game": game, "last_played": last_played, "avg_group_elo": avg_group_elo})

        if priorizar == "afinidad":
            scored.sort(key=lambda x: (x["avg_group_elo"] is None, -(x["avg_group_elo"] or 0)))
        else:
            scored.sort(key=lambda x: (x["last_played"] is not None, x["last_played"] or ""))

        recommendations = scored[:5]

        if recommendations:
            cache_key = (tuple(sorted(player_ids)), minutes, complexity_filter, priorizar)
            if cache_key in OLLAMA_CACHE:
                ai_blurb = OLLAMA_CACHE[cache_key]
            else:
                games_txt = ", ".join(r["game"]["name"] for r in recommendations)
                prompt = (
                    f"Sois {n_players} jugadores ({', '.join(selected_players)}) y tenéis {minutes} minutos. "
                    f"Los juegos candidatos son: {games_txt}. "
                    "Responde en español, en 2-3 frases, recomendando cuál jugar primero de esa lista y por qué, "
                    "con un tono cercano y entusiasta, sin usar markdown."
                )
                ai_blurb = ask_ollama(prompt)
                OLLAMA_CACHE[cache_key] = ai_blurb

    return render_template(
        "recommend.html",
        users=users,
        recommendations=recommendations,
        ai_blurb=ai_blurb,
        selected_players=selected_players,
        minutes=minutes,
        complexity_filter=complexity_filter,
        priorizar=priorizar,
    )


# ---------- Exportar datos ----------

def _export_rows():
    db = get_db()
    return db.execute(
        """SELECT m.id AS match_id, m.played_at, g.name AS game, m.duration_minutes,
                  u.username AS player, mp.rank, mp.score, mp.elo_before, mp.elo_after
           FROM matches m
           JOIN games g ON g.id = m.game_id
           JOIN match_players mp ON mp.match_id = m.id
           JOIN users u ON u.id = mp.user_id
           ORDER BY m.played_at ASC, m.id ASC, mp.rank ASC"""
    ).fetchall()


@app.route("/export/matches.csv")
@login_required
def export_csv():
    rows = _export_rows()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["match_id", "played_at", "game", "duration_minutes", "player", "rank", "score", "elo_before", "elo_after"])
    for r in rows:
        writer.writerow([r["match_id"], r["played_at"], r["game"], r["duration_minutes"], r["player"], r["rank"], r["score"], r["elo_before"], r["elo_after"]])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tabletop_matches.csv"},
    )


@app.route("/export/matches.json")
@login_required
def export_json():
    rows = _export_rows()
    data = [dict(r) for r in rows]
    return Response(
        json.dumps(data, indent=2, ensure_ascii=False),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=tabletop_matches.json"},
    )


if __name__ == "__main__":
    import os
    if not os.path.exists(DATABASE):
        init_db()
        print("Base de datos inicializada.")
    app.run(debug=True, port=5000)
