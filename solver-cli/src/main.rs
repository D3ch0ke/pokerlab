// pokerlab-solver -- thin JSON-in/JSON-out CLI wrapper around `postflop-solver`.
//
// LICENSE NOTICE
// This binary links against `postflop-solver` (https://github.com/b-inary/postflop-solver),
// which is licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).
// Because AGPL is a strong copyleft license, this crate -- and any work that is a derivative of
// it or distributes/serves it over a network -- must also be made available under
// AGPL-3.0-or-later, including complete corresponding source code. This crate is therefore
// licensed AGPL-3.0-or-later. Keep this in mind before shipping this binary or exposing it
// through a networked service.
//
// This crate contains NO poker logic of its own: it only marshals JSON in and out of the
// solver library.

use std::io::Read;
use std::time::Instant;

use postflop_solver::*;
use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Input contract (stdin)
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct StreetSizes {
    #[serde(default)]
    flop: Vec<String>,
    #[serde(default)]
    turn: Vec<String>,
    #[serde(default)]
    river: Vec<String>,
}

impl Default for StreetSizes {
    fn default() -> Self {
        Self {
            flop: Vec::new(),
            turn: Vec::new(),
            river: Vec::new(),
        }
    }
}

#[derive(Debug, Deserialize)]
struct Input {
    oop_range: String,
    ip_range: String,
    board: Vec<String>,
    starting_pot: i32,
    effective_stack: i32,
    #[serde(default)]
    bet_sizes: StreetSizes,
    #[serde(default)]
    raise_sizes: StreetSizes,
    #[serde(default = "default_max_iterations")]
    max_iterations: u32,
    /// Target exploitability as a PERCENT OF THE POT (e.g. 0.5 == 0.5% of pot).
    #[serde(default = "default_target_exploitability")]
    target_exploitability: f32,

    // Optional passthroughs to TreeConfig. Defaults match the upstream example.
    #[serde(default)]
    rake_rate: f64,
    #[serde(default)]
    rake_cap: f64,
    #[serde(default = "default_add_allin_threshold")]
    add_allin_threshold: f64,
    #[serde(default = "default_force_allin_threshold")]
    force_allin_threshold: f64,
    #[serde(default = "default_merging_threshold")]
    merging_threshold: f64,
    /// Use 16-bit compressed storage (halves memory, slightly less precision).
    #[serde(default)]
    compress_memory: bool,

    /// Refuse to allocate a tree larger than this, in bytes. 0 disables.
    ///
    /// Tree size is known BEFORE any memory is committed, so an impossible
    /// spot can be reported in milliseconds instead of paging the machine for
    /// minutes and then failing. A deep stack in a small pot is the usual
    /// cause: at 30:1 stack-to-pot there are several streets of raises to
    /// enumerate and the tree runs to many gigabytes.
    #[serde(default)]
    max_memory_bytes: u64,

    /// Actions to walk from the root before reading the strategy.
    ///
    /// Two forms are accepted per step:
    ///   "bet 30"  -- exactly as `available_actions` renders it.
    ///   "bet~35"  -- the bet closest to 35 chips among those the tree offers.
    ///
    /// The second exists because a real hand's bet sizes are whatever the
    /// players chose, and a solvable tree can only carry a few. The size
    /// actually used is echoed in `node_path`, and a substitution that moves
    /// the price materially is reported in `warnings` rather than hidden.
    ///
    /// The replayer uses this to put the caller's ACTUAL decision point at the
    /// node being reported. Without it only the first decision of a street can
    /// be examined, which is the minority of real decisions.
    #[serde(default)]
    action_path: Vec<String>,
}

fn default_max_iterations() -> u32 {
    1000
}
fn default_target_exploitability() -> f32 {
    0.5
}
fn default_add_allin_threshold() -> f64 {
    1.5
}
fn default_force_allin_threshold() -> f64 {
    0.15
}
fn default_merging_threshold() -> f64 {
    0.1
}

// ---------------------------------------------------------------------------
// Output contract (stdout)
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct HandStrategy {
    hand: String,
    /// Action name -> frequency in [0, 1].
    actions: serde_json::Map<String, serde_json::Value>,
    /// Action name -> expected value in chips, for THIS hand taking THIS action.
    ///
    /// This is what makes a decision gradable: a frequency says what the
    /// solver does, an EV says what it costs you not to.
    ev: serde_json::Map<String, serde_json::Value>,
    /// This hand's equity against the opponent's range at this node, as if
    /// all cards were run out with no more betting. Independent of the
    /// strategy, so it is exact even at zero iterations -- which is how a
    /// range gets ranked on a board without a solve.
    equity: f64,
}

#[derive(Debug, Serialize)]
struct Output {
    /// Exploitability in chips. ALWAYS reported.
    exploitability: f32,
    /// Exploitability as a percent of the starting pot.
    exploitability_pct_pot: f32,
    /// The target that was asked for, in chips.
    target_exploitability: f32,
    /// Whether `exploitability <= target_exploitability`.
    converged: bool,
    iterations: u32,
    max_iterations: u32,
    oop_ev: f32,
    ip_ev: f32,
    oop_equity: f32,
    ip_equity: f32,
    root_player: String,
    root_actions: Vec<String>,
    /// The action path actually walked from the root. Empty means the reported
    /// node IS the root; `root_*` below always describe the node reached.
    node_path: Vec<String>,
    /// Non-fatal advisories about the configuration (e.g. a degenerate tree).
    warnings: Vec<String>,
    root_strategy: Vec<HandStrategy>,
    memory_usage_bytes: u64,
    elapsed_ms: u128,
}

#[derive(Debug, Serialize)]
struct ErrorOutput {
    error: String,
}

// ---------------------------------------------------------------------------

fn main() {
    match run() {
        Ok(output) => {
            // Only ever printed once the solve has fully completed.
            println!("{}", serde_json::to_string(&output).unwrap());
        }
        Err(e) => {
            let err = ErrorOutput { error: e };
            eprintln!("{}", serde_json::to_string(&err).unwrap());
            std::process::exit(1);
        }
    }
}

fn run() -> Result<Output, String> {
    let mut raw = String::new();
    std::io::stdin()
        .read_to_string(&mut raw)
        .map_err(|e| format!("failed to read stdin: {e}"))?;

    if raw.trim().is_empty() {
        return Err("empty stdin: expected a JSON spot description".to_string());
    }

    let input: Input =
        serde_json::from_str(&raw).map_err(|e| format!("invalid input JSON: {e}"))?;

    let start = Instant::now();

    // --- card configuration -------------------------------------------------
    let oop_range: Range = input
        .oop_range
        .parse()
        .map_err(|e| format!("invalid oop_range: {e}"))?;
    let ip_range: Range = input
        .ip_range
        .parse()
        .map_err(|e| format!("invalid ip_range: {e}"))?;

    if input.board.len() < 3 || input.board.len() > 5 {
        return Err(format!(
            "board must contain 3, 4 or 5 cards, got {}",
            input.board.len()
        ));
    }

    let flop_str = input.board[..3].concat();
    let flop = flop_from_str(&flop_str).map_err(|e| format!("invalid flop cards: {e}"))?;

    let turn = match input.board.get(3) {
        Some(c) => card_from_str(c).map_err(|e| format!("invalid turn card: {e}"))?,
        None => NOT_DEALT,
    };
    let river = match input.board.get(4) {
        Some(c) => card_from_str(c).map_err(|e| format!("invalid river card: {e}"))?,
        None => NOT_DEALT,
    };

    let initial_state = match input.board.len() {
        3 => BoardState::Flop,
        4 => BoardState::Turn,
        _ => BoardState::River,
    };

    let card_config = CardConfig {
        range: [oop_range, ip_range],
        flop,
        turn,
        river,
    };

    // --- tree configuration -------------------------------------------------
    if input.starting_pot <= 0 {
        return Err("starting_pot must be > 0".to_string());
    }
    if input.effective_stack <= 0 {
        return Err("effective_stack must be > 0".to_string());
    }

    let flop_sizes = build_bet_sizes("flop", &input.bet_sizes.flop, &input.raise_sizes.flop)?;
    let turn_sizes = build_bet_sizes("turn", &input.bet_sizes.turn, &input.raise_sizes.turn)?;
    let river_sizes = build_bet_sizes("river", &input.bet_sizes.river, &input.raise_sizes.river)?;

    let tree_config = TreeConfig {
        initial_state,
        starting_pot: input.starting_pot,
        effective_stack: input.effective_stack,
        rake_rate: input.rake_rate,
        rake_cap: input.rake_cap,
        flop_bet_sizes: [flop_sizes.clone(), flop_sizes],
        turn_bet_sizes: [turn_sizes.clone(), turn_sizes],
        river_bet_sizes: [river_sizes.clone(), river_sizes],
        turn_donk_sizes: None,
        river_donk_sizes: None,
        add_allin_threshold: input.add_allin_threshold,
        force_allin_threshold: input.force_allin_threshold,
        merging_threshold: input.merging_threshold,
    };

    let action_tree =
        ActionTree::new(tree_config).map_err(|e| format!("failed to build action tree: {e}"))?;
    let mut game = PostFlopGame::with_config(card_config, action_tree)
        .map_err(|e| format!("failed to build game: {e}"))?;

    let (mem_uncompressed, mem_compressed) = game.memory_usage();
    let mem_usage = if input.compress_memory {
        mem_compressed
    } else {
        mem_uncompressed
    };

    // Checked before allocating: this is the difference between a clear
    // refusal and a machine that stops responding.
    if input.max_memory_bytes > 0 && mem_usage > input.max_memory_bytes {
        return Err(format!(
            "tree needs {:.1} GB but the budget is {:.1} GB (stack-to-pot {:.0}:1,              {} oop combos x {} ip combos). Narrow the ranges, drop a bet or raise              size, cap the effective stack, or set compress_memory.",
            mem_usage as f64 / 1e9,
            input.max_memory_bytes as f64 / 1e9,
            input.effective_stack as f64 / input.starting_pot as f64,
            game.private_cards(0).len(),
            game.private_cards(1).len(),
        ));
    }

    game.allocate_memory(input.compress_memory);

    // --- solve --------------------------------------------------------------
    // Target is given as a percent of the pot; the solver works in chips.
    let target = input.starting_pot as f32 * (input.target_exploitability / 100.0);

    let mut exploitability = compute_exploitability(&game);
    let mut iterations: u32 = 0;

    for t in 0..input.max_iterations {
        if exploitability <= target {
            break;
        }
        solve_step(&game, t);
        iterations = t + 1;
        // Exploitability is expensive; mirror upstream's every-10-iterations cadence.
        if (t + 1) % 10 == 0 || t + 1 == input.max_iterations {
            exploitability = compute_exploitability(&game);
        }
    }

    finalize(&mut game);
    // Recompute after finalize so the reported number describes the strategy
    // that is actually returned below. This is the number the caller must trust.
    let exploitability = compute_exploitability(&game);

    // --- extract results ----------------------------------------------------
    game.back_to_root();
    game.cache_normalized_weights();

    // Whole-tree numbers are always measured at the root, whatever node the
    // caller asked to inspect: they describe the spot, not the decision.
    let oop_ev = compute_average(&game.expected_values(0), game.normalized_weights(0));
    let ip_ev = compute_average(&game.expected_values(1), game.normalized_weights(1));
    let oop_equity = compute_average(&game.equity(0), game.normalized_weights(0));
    let ip_equity = compute_average(&game.equity(1), game.normalized_weights(1));

    // Walk to the requested node. Every step is matched by exact name so a
    // mistyped path fails loudly rather than reporting a different decision.
    let mut walked: Vec<String> = Vec::new();
    let mut substitutions: Vec<String> = Vec::new();
    for step in &input.action_path {
        if game.is_terminal_node() {
            return Err(format!(
                "action_path step {step:?} is unreachable: the node after {walked:?} is terminal"
            ));
        }
        if game.is_chance_node() {
            return Err(format!(
                "action_path step {step:?} lands on a chance node (a card to come) after                  {walked:?}. Pass the board as of the node's own street instead of dealing                  runouts inside the tree."
            ));
        }
        let names: Vec<String> = game
            .available_actions()
            .iter()
            .map(action_to_string)
            .collect();
        let idx = match resolve_step(step, &names) {
            Some((i, Some(gap))) => {
                // The tree could not offer the size actually used. Say by how
                // much, in chips: a caller comparing a line against a solve
                // must know the solve answered a slightly different question.
                if gap > 0.0 {
                    substitutions.push(format!(
                        "{step:?} was played as {:?} ({gap:.0} chips away): the tree does \
                         not contain the exact size used",
                        names[i]
                    ));
                }
                i
            }
            Some((i, None)) => i,
            None => {
                return Err(format!(
                    "action_path step {step:?} is not available after {walked:?}; \
                     the node offers {names:?}"
                ))
            }
        };
        game.play(idx);
        walked.push(names[idx].clone());
    }
    game.cache_normalized_weights();

    if game.is_terminal_node() || game.is_chance_node() {
        return Err(format!(
            "action_path {walked:?} ends on a node with no decision to make"
        ));
    }

    let root_player_idx = game.current_player();
    let root_player = if root_player_idx == 0 { "oop" } else { "ip" };

    let actions = game.available_actions();
    let action_names: Vec<String> = actions.iter().map(action_to_string).collect();

    let hands = holes_to_strings(game.private_cards(root_player_idx))
        .map_err(|e| format!("failed to render private cards: {e}"))?;
    let strategy = game.strategy();
    // Per-action EV for the acting player, laid out exactly like `strategy`.
    let ev_detail = game.expected_values_detail(root_player_idx);
    let equities = game.equity(root_player_idx);
    let n_hands = hands.len();

    if n_hands == 0 {
        return Err("root player has no combos on this board (empty range?)".to_string());
    }
    if strategy.len() != n_hands * actions.len() {
        return Err(format!(
            "unexpected strategy length: got {}, expected {} hands x {} actions",
            strategy.len(),
            n_hands,
            actions.len()
        ));
    }
    if ev_detail.len() != n_hands * actions.len() {
        return Err(format!(
            "unexpected EV length: got {}, expected {} hands x {} actions",
            ev_detail.len(),
            n_hands,
            actions.len()
        ));
    }

    let root_strategy = hands
        .iter()
        .enumerate()
        .map(|(i, hand)| {
            let mut map = serde_json::Map::new();
            let mut evs = serde_json::Map::new();
            for (a, name) in action_names.iter().enumerate() {
                let freq = strategy[a * n_hands + i];
                map.insert(
                    name.clone(),
                    serde_json::json!((freq as f64 * 1e6).round() / 1e6),
                );
                let ev = ev_detail[a * n_hands + i];
                evs.insert(
                    name.clone(),
                    serde_json::json!((ev as f64 * 1e4).round() / 1e4),
                );
            }
            HandStrategy {
                hand: hand.clone(),
                actions: map,
                ev: evs,
                equity: (equities[i] as f64 * 1e4).round() / 1e4,
            }
        })
        .collect();

    let mut warnings = Vec::new();
    warnings.extend(substitutions);
    if actions.len() <= 1 {
        warnings.push(format!(
            "root node has only {:?} available -- the action tree contains no betting on this \
             street, so the reported exploitability is trivially near zero and does not mean the \
             spot was solved. Check that bet_sizes/raise_sizes were supplied for this street.",
            action_names
        ));
    }
    if iterations >= input.max_iterations && exploitability > target {
        warnings.push(
            "iteration limit reached before hitting target_exploitability; the returned strategy \
             is NOT converged."
                .to_string(),
        );
    }

    Ok(Output {
        exploitability,
        exploitability_pct_pot: 100.0 * exploitability / input.starting_pot as f32,
        target_exploitability: target,
        converged: exploitability <= target,
        iterations,
        max_iterations: input.max_iterations,
        oop_ev,
        ip_ev,
        oop_equity,
        ip_equity,
        root_player: root_player.to_string(),
        root_actions: action_names,
        node_path: walked,
        warnings,
        root_strategy,
        memory_usage_bytes: mem_usage,
        elapsed_ms: start.elapsed().as_millis(),
    })
}

/// Resolve one `action_path` step against the actions a node actually offers.
///
/// Returns the chosen index and, when the step asked for a size by proximity,
/// how far the chosen size sits from the one requested.
fn resolve_step(step: &str, names: &[String]) -> Option<(usize, Option<f64>)> {
    if let Some(i) = names.iter().position(|n| n == step) {
        return Some((i, None));
    }
    let (verb, wanted) = step.split_once('~')?;
    let wanted: f64 = wanted.trim().parse().ok()?;
    let verb = verb.trim();

    // "call" and "check" carry no size, so a "~" form still matches them.
    if let Some(i) = names.iter().position(|n| n == verb) {
        return Some((i, Some(0.0)));
    }

    let mut best: Option<(usize, f64)> = None;
    for (i, name) in names.iter().enumerate() {
        let Some((n_verb, amount)) = name.split_once(' ') else {
            continue;
        };
        // An all-in IS a bet or a raise; the tree just names it differently,
        // and refusing to match it would make every shove unreachable.
        let comparable = n_verb == verb
            || (n_verb == "allin" && (verb == "bet" || verb == "raise"));
        if !comparable {
            continue;
        }
        let Ok(amount) = amount.parse::<f64>() else {
            continue;
        };
        let gap = (amount - wanted).abs();
        if best.map_or(true, |(_, b)| gap < b) {
            best = Some((i, gap));
        }
    }
    best.map(|(i, gap)| (i, Some(gap)))
}

/// Join the caller's size lists into the comma-separated form the library parses.
fn build_bet_sizes(
    street: &str,
    bets: &[String],
    raises: &[String],
) -> Result<BetSizeOptions, String> {
    let bet_str = bets.join(",");
    let raise_str = raises.join(",");
    BetSizeOptions::try_from((bet_str.as_str(), raise_str.as_str()))
        .map_err(|e| format!("invalid {street} bet/raise sizes ({bet_str:?} / {raise_str:?}): {e}"))
}

fn action_to_string(action: &Action) -> String {
    match action {
        Action::None => "none".to_string(),
        Action::Fold => "fold".to_string(),
        Action::Check => "check".to_string(),
        Action::Call => "call".to_string(),
        Action::Bet(n) => format!("bet {n}"),
        Action::Raise(n) => format!("raise {n}"),
        Action::AllIn(n) => format!("allin {n}"),
        Action::Chance(c) => format!("chance {c}"),
    }
}
