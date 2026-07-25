DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS games;
DROP TABLE IF EXISTS matches;
DROP TABLE IF EXISTS match_players;
DROP TABLE IF EXISTS game_elo;

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    elo REAL NOT NULL DEFAULT 1200,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    min_players INTEGER NOT NULL DEFAULT 2,
    max_players INTEGER NOT NULL DEFAULT 4,
    avg_duration_minutes INTEGER NOT NULL DEFAULT 60,
    complexity INTEGER NOT NULL DEFAULT 3, -- 1 (ligero) a 5 (pesado)
    notes TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL,
    played_at TEXT NOT NULL,
    duration_minutes INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (game_id) REFERENCES games(id)
);

CREATE TABLE match_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    rank INTEGER NOT NULL, -- 1 = ganador, empates comparten numero
    score INTEGER,
    elo_before REAL NOT NULL,
    elo_after REAL NOT NULL,
    FOREIGN KEY (match_id) REFERENCES matches(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Elo especifico por juego (independiente del Elo global)
CREATE TABLE game_elo (
    user_id INTEGER NOT NULL,
    game_id INTEGER NOT NULL,
    elo REAL NOT NULL DEFAULT 1200,
    PRIMARY KEY (user_id, game_id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (game_id) REFERENCES games(id)
);
