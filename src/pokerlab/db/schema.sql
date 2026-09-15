CREATE TABLE IF NOT EXISTS hands (
    hand_id     VARCHAR PRIMARY KEY,
    game_name   VARCHAR NOT NULL,
    played_at   TIMESTAMPTZ NOT NULL,
    table_id    VARCHAR,
    sb          INTEGER NOT NULL,      -- cents
    bb          INTEGER NOT NULL,
    total_pot   INTEGER NOT NULL,
    rake        INTEGER NOT NULL,
    uncalled    INTEGER NOT NULL,      -- returned to the last aggressor
    n_players   INTEGER NOT NULL,
    hero        VARCHAR NOT NULL,
    hero_pos    VARCHAR,
    hero_net    INTEGER NOT NULL,      -- cents, profit
    hero_cards  VARCHAR,
    hero_rake   INTEGER NOT NULL,      -- hero's share, pro-rata on contribution
    saw_flop    BOOLEAN NOT NULL,      -- hero specifically, not "a flop was dealt"
    showdown    BOOLEAN NOT NULL,      -- hero specifically reached showdown
    hero_won    BOOLEAN NOT NULL,
    board       VARCHAR,
    flop_paired   BOOLEAN,
    flop_suits    VARCHAR,     -- rainbow | two-tone | monotone
    flop_high     VARCHAR,     -- A K Q J T | low
    flop_connect  VARCHAR,     -- connected | gapped | disconnected | paired
    flop_wet      BOOLEAN
);

CREATE TABLE IF NOT EXISTS seats (
    hand_id   VARCHAR NOT NULL,
    seat_no   INTEGER NOT NULL,
    name      VARCHAR NOT NULL,
    stack     INTEGER NOT NULL,
    position  VARCHAR,
    is_hero   BOOLEAN NOT NULL,
    net       INTEGER NOT NULL,
    shown     VARCHAR            -- cards revealed at showdown, NULL if never shown
);

CREATE TABLE IF NOT EXISTS actions (
    hand_id     VARCHAR NOT NULL,
    idx         INTEGER NOT NULL,      -- order within the hand
    street      VARCHAR NOT NULL,
    seat_no     INTEGER NOT NULL,
    name        VARCHAR NOT NULL,
    position    VARCHAR,
    is_hero     BOOLEAN NOT NULL,
    verb        VARCHAR NOT NULL,
    announced   INTEGER NOT NULL,
    contributed INTEGER NOT NULL,
    effective   INTEGER NOT NULL,      -- what every %-pot metric uses
    pot_before  INTEGER NOT NULL,      -- called money in the pot before this action
    all_in      BOOLEAN NOT NULL,
    dead        BOOLEAN NOT NULL,
    secs        DOUBLE                 -- tank time before acting
);

CREATE TABLE IF NOT EXISTS quarantine (
    reason VARCHAR,
    raw    VARCHAR
);

CREATE INDEX IF NOT EXISTS hands_played_at ON hands (played_at);
CREATE INDEX IF NOT EXISTS actions_hand    ON actions (hand_id);
