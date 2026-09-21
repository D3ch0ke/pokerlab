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
// solver library. The one judgement it carries is the "ranked" lock mode below, which
// assigns a locked frequency to hands strongest-first; that ordering is the caller's
// stated model of the opponent and is documented as such.

use std::collections::BTreeMap;
use std::io::Read;
use std::time::Instant;

use postflop_solver::*;
use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Input contract (stdin)
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize, Clone, Default)]
struct StreetSizes {
    #[serde(default)]
    flop: Vec<String>,
    #[serde(default)]
    turn: Vec<String>,
    #[serde(default)]
    river: Vec<String>,
}

/// Fix the strategy of one node before solving, so the solver answers "what
/// is my best response to an opponent who plays THIS way here" instead of
/// "what is the equilibrium".
///
/// `path` walks from the root exactly like `action_path`; a `"*"` step at a
/// chance node means every card that can be dealt, so one lock can cover a
/// whole street. The node reached must belong to the player being modelled --
/// which player that is falls out of the path.
///
/// Modes:
///   "uniform" -- every hand uses the same mix, `actions` (name -> frequency).
///   "ranked"  -- hands are sorted by equity at the node and the mix is applied
///                strongest-first in `rank_order` (default: allin, raise, bet,
///                call, check, fold). "folds 50%" then means the weakest half
///                of the range folds. `blend` mixes in the uniform version
///                (0 = pure ranked, 1 = pure uniform).
///   "hands"   -- explicit per-hand mixes in `hands`; hands not listed stay free.
#[derive(Debug, Deserialize)]
struct Lock {
    path: Vec<String>,
    #[serde(default = "default_lock_mode")]
    mode: String,
    #[serde(default)]
    actions: BTreeMap<String, f64>,
    #[serde(default)]
    rank_order: Vec<String>,
    #[serde(default)]
    blend: f64,
    #[serde(default)]
    hands: BTreeMap<String, BTreeMap<String, f64>>,
}

fn default_lock_mode() -> String {
    "uniform".to_string()
}

#[derive(Debug, Deserialize)]
struct Input {
    oop_range: String,
    ip_range: String,
    board: Vec<String>,
    starting_pot: i32,
    effective_stack: i32,
    /// Sizes shared by both players unless a per-player override is given.
    #[serde(default)]
    bet_sizes: StreetSizes,
    #[serde(default)]
    raise_sizes: StreetSizes,
    /// Per-player overrides. An opponent who only ever bets 50% or 100% should
    /// be modelled with those two sizes while hero keeps a wider menu.
    #[serde(default)]
    oop_bet_sizes: Option<StreetSizes>,
    #[serde(default)]
    ip_bet_sizes: Option<StreetSizes>,
    #[serde(default)]
    oop_raise_sizes: Option<StreetSizes>,
    #[serde(default)]
    ip_raise_sizes: Option<StreetSizes>,
    /// Donk (OOP lead into the previous street's aggressor) sizes for turn and
    /// river. Empty means the library default.
    #[serde(default)]
    donk_sizes: StreetSizes,
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

    /// Build the tree, report its size and every decision node, and stop
    /// before allocating anything. Milliseconds, whatever the tree.
    #[serde(default)]
    dry_run: bool,

    /// Nodes whose strategy is fixed before solving. See `Lock`.
    #[serde(default)]
    locks: Vec<Lock>,

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

    /// Further nodes to report from the same solve, each a path from the root.
    /// A solve is the expensive part; reading ten nodes from it is free.
    #[serde(default)]
    report_paths: Vec<Vec<String>>,
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
    /// This hand's share of the acting player's range at this node, summing
    /// to 1 over the node. What an aggregate frequency must be weighted by.
    weight: f64,
}

/// One decision node read from a solved game.
#[derive(Debug, Serialize)]
struct NodeReport {
    path: Vec<String>,
    player: String,
    actions: Vec<String>,
    /// Action name -> range-weighted frequency at this node.
    aggregate: serde_json::Map<String, serde_json::Value>,
    /// Whether a lock fixed this node's strategy before the solve.
    locked: bool,
    strategy: Vec<HandStrategy>,
    warnings: Vec<String>,
}

/// One decision node of the action tree, from a dry run.
#[derive(Debug, Serialize)]
struct TreeNode {
    path: Vec<String>,
    street: String,
    player: String,
    pot: i32,
    actions: Vec<String>,
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
    /// Range-weighted action frequencies at the reported node.
    aggregate: serde_json::Map<String, serde_json::Value>,
    /// Extra nodes read from the same solve (`report_paths`), in order.
    reports: Vec<NodeReport>,
    /// How many nodes a lock fixed. With locks, `exploitability` measures the
    /// UNLOCKED player's best response gap, not an equilibrium.
    locked_nodes: usize,
    memory_usage_bytes: u64,
    memory_usage_compressed_bytes: u64,
    elapsed_ms: u128,
    /// Only on a dry run: every decision node of the tree.
    #[serde(skip_serializing_if = "Option::is_none")]
    tree: Option<Vec<TreeNode>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tree_truncated: Option<bool>,
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

const MAX_TREE_NODES: usize = 50_000;

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

    let sizes_for = |player: &str, street: &str| -> Result<BetSizeOptions, String> {
        let (bets_over, raises_over) = if player == "oop" {
            (&input.oop_bet_sizes, &input.oop_raise_sizes)
        } else {
            (&input.ip_bet_sizes, &input.ip_raise_sizes)
        };
        let pick = |s: &StreetSizes| -> Vec<String> {
            match street {
                "flop" => s.flop.clone(),
                "turn" => s.turn.clone(),
                _ => s.river.clone(),
            }
        };
        let bets = bets_over.as_ref().map(&pick).unwrap_or_else(|| pick(&input.bet_sizes));
        let raises = raises_over
            .as_ref()
            .map(&pick)
            .unwrap_or_else(|| pick(&input.raise_sizes));
        build_bet_sizes(&format!("{player} {street}"), &bets, &raises)
    };

    let donk_for = |street: &str| -> Result<Option<DonkSizeOptions>, String> {
        let list = if street == "turn" {
            &input.donk_sizes.turn
        } else {
            &input.donk_sizes.river
        };
        if list.is_empty() {
            return Ok(None);
        }
        let joined = list.join(",");
        DonkSizeOptions::try_from(joined.as_str())
            .map(Some)
            .map_err(|e| format!("invalid {street} donk sizes ({joined:?}): {e}"))
    };

    let tree_config = TreeConfig {
        initial_state,
        starting_pot: input.starting_pot,
        effective_stack: input.effective_stack,
        rake_rate: input.rake_rate,
        rake_cap: input.rake_cap,
        flop_bet_sizes: [sizes_for("oop", "flop")?, sizes_for("ip", "flop")?],
        turn_bet_sizes: [sizes_for("oop", "turn")?, sizes_for("ip", "turn")?],
        river_bet_sizes: [sizes_for("oop", "river")?, sizes_for("ip", "river")?],
        turn_donk_sizes: donk_for("turn")?,
        river_donk_sizes: donk_for("river")?,
        add_allin_threshold: input.add_allin_threshold,
        force_allin_threshold: input.force_allin_threshold,
        merging_threshold: input.merging_threshold,
    };

    let mut action_tree =
        ActionTree::new(tree_config).map_err(|e| format!("failed to build action tree: {e}"))?;

    let listing = if input.dry_run {
        let mut nodes = Vec::new();
        let mut path = Vec::new();
        let street0 = match initial_state {
            BoardState::Flop => 0,
            BoardState::Turn => 1,
            _ => 2,
        };
        let truncated = list_nodes(
            &mut action_tree,
            &mut path,
            0,
            street0,
            input.starting_pot,
            &mut nodes,
        );
        action_tree.back_to_root();
        Some((nodes, truncated))
    } else {
        None
    };

    let mut game = PostFlopGame::with_config(card_config, action_tree)
        .map_err(|e| format!("failed to build game: {e}"))?;

    let (mem_uncompressed, mem_compressed) = game.memory_usage();
    let mem_usage = if input.compress_memory {
        mem_compressed
    } else {
        mem_uncompressed
    };

    let mut warnings = Vec::new();

    if let Some((nodes, truncated)) = listing {
        if truncated {
            warnings.push(format!(
                "tree listing stopped at {MAX_TREE_NODES} decision nodes; the tree is larger"
            ));
        }
        let root_actions: Vec<String> = game.available_actions().iter().map(action_to_string).collect();
        return Ok(Output {
            exploitability: 0.0,
            exploitability_pct_pot: 0.0,
            target_exploitability: 0.0,
            converged: false,
            iterations: 0,
            max_iterations: input.max_iterations,
            oop_ev: 0.0,
            ip_ev: 0.0,
            oop_equity: 0.0,
            ip_equity: 0.0,
            root_player: "oop".to_string(),
            root_actions,
            node_path: Vec::new(),
            warnings,
            root_strategy: Vec::new(),
            aggregate: serde_json::Map::new(),
            reports: Vec::new(),
            locked_nodes: 0,
            memory_usage_bytes: mem_uncompressed,
            memory_usage_compressed_bytes: mem_compressed,
            elapsed_ms: start.elapsed().as_millis(),
            tree: Some(nodes),
            tree_truncated: Some(truncated),
        });
    }

    // Checked before allocating: this is the difference between a clear
    // refusal and a machine that stops responding.
    if input.max_memory_bytes > 0 && mem_usage > input.max_memory_bytes {
        return Err(format!(
            "tree needs {:.1} GB but the budget is {:.1} GB (stack-to-pot {:.0}:1, {} oop combos \
             x {} ip combos). Narrow the ranges, drop a bet or raise size, cap the effective \
             stack, or set compress_memory.",
            mem_usage as f64 / 1e9,
            input.max_memory_bytes as f64 / 1e9,
            input.effective_stack as f64 / input.starting_pot as f64,
            game.private_cards(0).len(),
            game.private_cards(1).len(),
        ));
    }

    game.allocate_memory(input.compress_memory);

    // --- locks --------------------------------------------------------------
    let mut locked_nodes = 0usize;
    for (i, lock) in input.locks.iter().enumerate() {
        game.back_to_root();
        let mut walked = Vec::new();
        apply_lock(&mut game, lock, &lock.path, &mut walked, &mut locked_nodes, &mut warnings)
            .map_err(|e| format!("lock #{i} ({:?}): {e}", lock.path))?;
    }
    game.back_to_root();

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

    let main = read_node(&mut game, &input.action_path)?;

    let mut reports = Vec::new();
    for path in &input.report_paths {
        reports.push(read_node(&mut game, path)?);
    }

    warnings.extend(main.warnings.iter().cloned());
    if iterations >= input.max_iterations && exploitability > target {
        warnings.push(
            "iteration limit reached before hitting target_exploitability; the returned strategy \
             is NOT converged."
                .to_string(),
        );
    }
    if locked_nodes > 0 {
        warnings.push(format!(
            "{locked_nodes} node(s) locked: the exploitability reported is the free player's \
             distance from a best response to the locked play, not an equilibrium gap"
        ));
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
        root_player: main.player,
        root_actions: main.actions,
        node_path: main.path,
        warnings,
        root_strategy: main.strategy,
        aggregate: main.aggregate,
        reports,
        locked_nodes,
        memory_usage_bytes: mem_usage,
        memory_usage_compressed_bytes: mem_compressed,
        elapsed_ms: start.elapsed().as_millis(),
        tree: None,
        tree_truncated: None,
    })
}

/// Walk the action tree from the current node and record every decision node.
/// Returns true when the listing was cut short.
fn list_nodes(
    tree: &mut ActionTree,
    path: &mut Vec<String>,
    player: usize,
    street: usize,
    starting_pot: i32,
    out: &mut Vec<TreeNode>,
) -> bool {
    if tree.is_terminal_node() {
        return false;
    }
    if out.len() >= MAX_TREE_NODES {
        return true;
    }
    let (player, street, pushed_star) = if tree.is_chance_node() {
        path.push("*".to_string());
        (0usize, street + 1, true)
    } else {
        (player, street, false)
    };
    let bets = tree.total_bet_amount();
    let actions: Vec<Action> = tree.available_actions().to_vec();
    let names: Vec<String> = actions.iter().map(action_to_string).collect();
    out.push(TreeNode {
        path: path.clone(),
        street: ["flop", "turn", "river"][street.min(2)].to_string(),
        player: if player == 0 { "oop".into() } else { "ip".into() },
        pot: starting_pot + bets[0] + bets[1],
        actions: names.clone(),
    });
    let mut truncated = false;
    for (action, name) in actions.iter().zip(names.iter()) {
        let next = match action {
            Action::Bet(_) | Action::Raise(_) | Action::AllIn(_) => 1 - player,
            Action::Check if player == 0 => 1,
            _ => 0,
        };
        if tree.play(*action).is_err() {
            continue;
        }
        path.push(name.clone());
        truncated |= list_nodes(tree, path, next, street, starting_pot, out);
        path.pop();
        let _ = tree.undo();
    }
    if pushed_star {
        path.pop();
    }
    truncated
}

/// Walk `steps` from the current node and lock every decision node they reach.
/// A `"*"` at a chance node fans out over every dealable card.
fn apply_lock(
    game: &mut PostFlopGame,
    lock: &Lock,
    steps: &[String],
    walked: &mut Vec<String>,
    count: &mut usize,
    warnings: &mut Vec<String>,
) -> Result<(), String> {
    if steps.is_empty() {
        if game.is_terminal_node() || game.is_chance_node() {
            return Err(format!("path {walked:?} ends on a node with no decision to lock"));
        }
        lock_here(game, lock)?;
        *count += 1;
        return Ok(());
    }
    if game.is_terminal_node() {
        return Err(format!("step {:?} is unreachable: the node after {walked:?} is terminal", steps[0]));
    }
    let step = &steps[0];
    if game.is_chance_node() {
        let cards: Vec<usize> = if step == "*" {
            let mask = game.possible_cards();
            (0..52).filter(|c| mask & (1u64 << c) != 0).collect()
        } else {
            let c = card_from_str(step).map_err(|e| format!("step {step:?} at a chance node: {e}"))?;
            vec![c as usize]
        };
        let here = game.history().to_vec();
        for card in cards {
            game.apply_history(&here);
            game.play(card);
            walked.push(card_to_string(card as Card).unwrap_or_else(|_| step.clone()));
            apply_lock(game, lock, &steps[1..], walked, count, warnings)?;
            walked.pop();
        }
        game.apply_history(&here);
        return Ok(());
    }
    let names: Vec<String> = game.available_actions().iter().map(action_to_string).collect();
    let idx = match resolve_step(step, &names) {
        Some((i, gap)) => {
            if let Some(gap) = gap {
                if gap > 0.0 {
                    warnings.push(format!(
                        "lock step {step:?} was walked as {:?} ({gap:.0} chips away)",
                        names[i]
                    ));
                }
            }
            i
        }
        None => {
            return Err(format!(
                "step {step:?} is not available after {walked:?}; the node offers {names:?}"
            ))
        }
    };
    let here = game.history().to_vec();
    game.play(idx);
    walked.push(names[idx].clone());
    apply_lock(game, lock, &steps[1..], walked, count, warnings)?;
    walked.pop();
    game.apply_history(&here);
    Ok(())
}

/// Fix the current node's strategy from a lock description.
fn lock_here(game: &mut PostFlopGame, lock: &Lock) -> Result<(), String> {
    let player = game.current_player();
    let names: Vec<String> = game.available_actions().iter().map(action_to_string).collect();
    let n_actions = names.len();
    let hands = holes_to_strings(game.private_cards(player))
        .map_err(|e| format!("failed to render private cards: {e}"))?;
    let n_hands = hands.len();
    if n_hands == 0 {
        return Err("locked player has no combos on this board".to_string());
    }

    // Action frequencies by index; an unknown action name is an error rather
    // than a silent zero, because a mistyped "raise" would lock a pure fold.
    // Keys are exact names ("raise 188"), proximity forms ("raise~200") or
    // bare verbs ("raise": spread equally over every raise size, all-in
    // included). A key that matches nothing is an error rather than a
    // silent zero, because a mistyped "raise" would lock a pure fold.
    let freq_of = |table: &BTreeMap<String, f64>| -> Result<Vec<f64>, String> {
        let mut v = vec![0.0; n_actions];
        for (name, f) in table {
            let f = f.max(0.0);
            let matches: Vec<usize> = if names.contains(name) || name.contains('~') {
                resolve_step(name, &names).map(|(i, _)| vec![i]).unwrap_or_default()
            } else {
                names
                    .iter()
                    .enumerate()
                    .filter(|(_, n)| {
                        let verb = n.split(' ').next().unwrap_or("");
                        verb == name || (verb == "allin" && (name == "bet" || name == "raise"))
                    })
                    .map(|(i, _)| i)
                    .collect()
            };
            if matches.is_empty() {
                return Err(format!("action {name:?} is not offered here; the node has {names:?}"));
            }
            for i in &matches {
                v[*i] += f / matches.len() as f64;
            }
        }
        Ok(v)
    };

    let mut strategy = vec![-1.0f32; n_actions * n_hands];

    match lock.mode.as_str() {
        "uniform" => {
            let freqs = freq_of(&lock.actions)?;
            if freqs.iter().sum::<f64>() <= 0.0 {
                return Err("uniform lock needs at least one positive action frequency".into());
            }
            for h in 0..n_hands {
                for a in 0..n_actions {
                    strategy[a * n_hands + h] = freqs[a] as f32;
                }
            }
        }
        "ranked" => {
            let freqs = freq_of(&lock.actions)?;
            let total: f64 = freqs.iter().sum();
            if total <= 0.0 {
                return Err("ranked lock needs at least one positive action frequency".into());
            }
            game.cache_normalized_weights();
            let equity = game.equity(player);
            let weights: Vec<f64> = game.normalized_weights(player).iter().map(|&w| w as f64).collect();
            let order = rank_order(&names, &lock.rank_order)?;
            // Hands strongest first; dead hands (weight 0) are left free.
            let mut idx: Vec<usize> = (0..n_hands).filter(|&h| weights[h] > 0.0).collect();
            idx.sort_by(|&a, &b| equity[b].partial_cmp(&equity[a]).unwrap_or(std::cmp::Ordering::Equal));
            let live: f64 = idx.iter().map(|&h| weights[h]).sum();
            // Budget of range weight for each action, in rank order.
            let mut budgets: Vec<(usize, f64)> = order
                .iter()
                .map(|&a| (a, live * freqs[a] / total))
                .filter(|(_, b)| *b > 0.0)
                .collect();
            let mut cursor = 0usize;
            let blend = lock.blend.clamp(0.0, 1.0);
            for &h in &idx {
                let mut remaining = weights[h];
                let mut ranked = vec![0.0f64; n_actions];
                while remaining > 1e-12 && cursor < budgets.len() {
                    let (a, ref mut budget) = budgets[cursor];
                    let take = remaining.min(*budget);
                    ranked[a] += take;
                    *budget -= take;
                    remaining -= take;
                    if *budget <= 1e-12 {
                        cursor += 1;
                    }
                }
                if remaining > 1e-12 {
                    // Rounding leftover: give it to the last action in order.
                    if let Some(&(a, _)) = budgets.last() {
                        ranked[a] += remaining;
                    }
                }
                let sum: f64 = ranked.iter().sum();
                for a in 0..n_actions {
                    let r = if sum > 0.0 { ranked[a] / sum } else { 0.0 };
                    let u = freqs[a] / total;
                    strategy[a * n_hands + h] = (blend * u + (1.0 - blend) * r) as f32;
                }
            }
        }
        "hands" => {
            if lock.hands.is_empty() {
                return Err("hands lock has no hands".into());
            }
            for (hand, table) in &lock.hands {
                let Some(h) = hands.iter().position(|x| x == hand) else {
                    // A hand blocked by the board is not in the list; say so
                    // rather than ignoring what might be a typo.
                    return Err(format!("hand {hand:?} is not in the locked player's range here"));
                };
                let freqs = freq_of(table)?;
                for a in 0..n_actions {
                    strategy[a * n_hands + h] = freqs[a] as f32;
                }
            }
        }
        other => return Err(format!("unknown lock mode {other:?}; use uniform, ranked or hands")),
    }

    game.lock_current_strategy(&strategy);
    Ok(())
}

/// Action indices strongest-first for a ranked lock.
fn rank_order(names: &[String], wanted: &[String]) -> Result<Vec<usize>, String> {
    if !wanted.is_empty() {
        let mut out = Vec::new();
        for w in wanted {
            let Some(i) = names.iter().position(|n| n == w) else {
                return Err(format!("rank_order names {w:?}, which the node does not offer: {names:?}"));
            };
            out.push(i);
        }
        return Ok(out);
    }
    // Default: the most aggressive action is taken by the strongest hands.
    let verb_rank = |n: &str| -> (usize, i64) {
        let (verb, amount) = n.split_once(' ').unwrap_or((n, "0"));
        let amt: i64 = amount.parse().unwrap_or(0);
        let r = match verb {
            "allin" => 0,
            "raise" => 1,
            "bet" => 2,
            "call" => 3,
            "check" => 4,
            _ => 5,
        };
        (r, -amt)
    };
    let mut idx: Vec<usize> = (0..names.len()).collect();
    idx.sort_by_key(|&i| verb_rank(&names[i]));
    Ok(idx)
}

/// Walk `path` from the root of a solved game and read the node reached.
fn read_node(game: &mut PostFlopGame, path: &[String]) -> Result<NodeReport, String> {
    game.back_to_root();
    let mut walked: Vec<String> = Vec::new();
    let mut warnings: Vec<String> = Vec::new();
    for step in path {
        if game.is_terminal_node() {
            return Err(format!(
                "action_path step {step:?} is unreachable: the node after {walked:?} is terminal"
            ));
        }
        if game.is_chance_node() {
            // A concrete card is allowed so a what-if can look past a street.
            let c = card_from_str(step).map_err(|_| {
                format!(
                    "action_path step {step:?} lands on a chance node (a card to come) after \
                     {walked:?}. Pass the board as of the node's own street instead of dealing \
                     runouts inside the tree, or name the card."
                )
            })?;
            game.play(c as usize);
            walked.push(step.clone());
            continue;
        }
        let names: Vec<String> = game.available_actions().iter().map(action_to_string).collect();
        let idx = match resolve_step(step, &names) {
            Some((i, Some(gap))) => {
                // The tree could not offer the size actually used. Say by how
                // much, in chips: a caller comparing a line against a solve
                // must know the solve answered a slightly different question.
                if gap > 0.0 {
                    warnings.push(format!(
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
        return Err(format!("action_path {walked:?} ends on a node with no decision to make"));
    }

    let player_idx = game.current_player();
    let player = if player_idx == 0 { "oop" } else { "ip" };
    let actions = game.available_actions();
    let names: Vec<String> = actions.iter().map(action_to_string).collect();
    let hands = holes_to_strings(game.private_cards(player_idx))
        .map_err(|e| format!("failed to render private cards: {e}"))?;
    let strategy = game.strategy();
    let ev_detail = game.expected_values_detail(player_idx);
    let equities = game.equity(player_idx);
    let weights = game.normalized_weights(player_idx);
    let n_hands = hands.len();
    if n_hands == 0 {
        return Err("acting player has no combos on this board (empty range?)".to_string());
    }
    if strategy.len() != n_hands * actions.len() || ev_detail.len() != n_hands * actions.len() {
        return Err(format!(
            "unexpected strategy length: got {}, expected {} hands x {} actions",
            strategy.len(),
            n_hands,
            actions.len()
        ));
    }
    let wsum: f64 = weights.iter().map(|&w| w as f64).sum();
    let mut aggregate_raw = vec![0.0f64; names.len()];

    let rows: Vec<HandStrategy> = hands
        .iter()
        .enumerate()
        .map(|(i, hand)| {
            let w = if wsum > 0.0 { weights[i] as f64 / wsum } else { 0.0 };
            let mut map = serde_json::Map::new();
            let mut evs = serde_json::Map::new();
            for (a, name) in names.iter().enumerate() {
                let freq = strategy[a * n_hands + i];
                aggregate_raw[a] += w * freq as f64;
                map.insert(name.clone(), serde_json::json!((freq as f64 * 1e6).round() / 1e6));
                let ev = ev_detail[a * n_hands + i];
                evs.insert(name.clone(), serde_json::json!((ev as f64 * 1e4).round() / 1e4));
            }
            HandStrategy {
                hand: hand.clone(),
                actions: map,
                ev: evs,
                equity: (equities[i] as f64 * 1e4).round() / 1e4,
                weight: (w * 1e6).round() / 1e6,
            }
        })
        .collect();

    let mut aggregate = serde_json::Map::new();
    for (a, name) in names.iter().enumerate() {
        aggregate.insert(name.clone(), serde_json::json!((aggregate_raw[a] * 1e4).round() / 1e4));
    }

    if actions.len() <= 1 {
        warnings.push(format!(
            "node has only {:?} available -- the action tree contains no betting on this \
             street, so the reported exploitability is trivially near zero and does not mean the \
             spot was solved. Check that bet_sizes/raise_sizes were supplied for this street.",
            names
        ));
    }

    Ok(NodeReport {
        path: walked,
        player: player.to_string(),
        actions: names,
        aggregate,
        locked: game.current_locking_strategy().is_some(),
        strategy: rows,
        warnings,
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
    label: &str,
    bets: &[String],
    raises: &[String],
) -> Result<BetSizeOptions, String> {
    let bet_str = bets.join(",");
    let raise_str = raises.join(",");
    BetSizeOptions::try_from((bet_str.as_str(), raise_str.as_str()))
        .map_err(|e| format!("invalid {label} bet/raise sizes ({bet_str:?} / {raise_str:?}): {e}"))
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
