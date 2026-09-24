# pokerlab README — illustration shot list and prompts

Ian Xiaohei style, plan_only output. Paste one prompt per image into an image
model (GPT Image, Nano Banana, etc.). Save results beside this file as
`01-…png` … `06-…png` using the save names below.

Anchors chosen from README.md: the founding principle, the money model, time
scoping, effective sizing, tree shape, and the uniform-vs-ranked lock. Left as
text: the command table, the dashboard pages, licensing.

---

## Shot list

| # | placement | core idea | structure | Xiaohei action | labels |
|---|---|---|---|---|---|
| 1 | after the opening principle | every claim is tied to a database row; where it can't be, say "not enough data" | conceptual metaphor | tying a thread from a claim to a row; one claim left untied with a red tag | `claim` / `row` / `n=12` / `not enough data` |
| 2 | "The money model" | uncalled = Σ contributions − Total Pot, and every hand must balance | conceptual metaphor | balancing one hand on a two-pan scale, handing an uncalled chip back | `everyone put in` / `pot + rake` / `returned` / `11,272 / 11,272` |
| 3 | "Every statistic is time-scoped" | a lifetime average blends several different players | before/after contrast | cutting an 11-month strip into windows with scissors | `11 months` / `several players` / `last 90 days` / `no lifetime` |
| 4 | "Bet sizes use effective" | a €5 shove into €2 vs €1 behind is a 50% bet | conceptual metaphor | holding a ruler against a tall chip tower; the ruler stops at villain's short stack | `announced €5` / `€1 behind` / `effective 50%` |
| 5 | "Solver" (tree shape) | removing raise branches changes the answer by up to 50 points | small comic sequence (2 panels) | pruning a tree with shears; the fruit labelled with the c-bet % changes | `full tree` / `raises cut` / `c-bet 30%` / `c-bet 80%` / `different question` |
| 6 | "Pool-locked solves" | folding half of every hand alike (sets included) is absurd; fold weakest-first | before/after contrast | left: flipping a coin over every hand; right: sorting a line of cards and folding the bottom | `fold 50%` / `even the set?` / `weakest first` / `ranked` |

Most reliable: 2, 3, 4 (single physical action, few labels).
Optional: 1, 5, 6 (two ideas each; regenerate if the frame fills up).

---

## Prompt 1 — `01-claim-tied-to-row.png`

```text
Generate one standalone 16:9 horizontal English article illustration.

Visual DNA:
Pure white background. Minimalist black hand-drawn line art. Slightly wobbly pen lines. Lots of empty white space. Sparse red/orange/blue handwritten English annotations. Clean absurd product-sketch feeling. No gradients, no shadows, no paper texture, no complex background, no commercial vector style, no PPT infographic look, no cute mascot poster, no children's illustration, no realistic UI.

Recurring IP character required:
Xiaohei, a small solid-black absurd creature with white dot eyes, tiny thin legs, blank serious expression, slightly uneven hand-drawn body shape. Xiaohei must perform the core conceptual action, not decorate the scene. Make Xiaohei serious, deadpan, and slightly bizarre, not cute.

Theme:
A poker analysis tool where every sentence must be anchored to a row of data.

Structure type:
conceptual metaphor

Core idea:
Prose is only allowed to say what a database row already says. A claim that cannot be tied to enough rows gets tagged "not enough data" instead of a guess.

Composition:
Three small hand-written claim cards float in the upper left. Below them, a plain rectangular grid of a few table rows. Xiaohei stands between them, seriously tying an orange thread from each claim card down to one specific row, like a tailor. One claim card has no thread; Xiaohei has hung a small red paper tag on it. Large empty white space on the right.

Suggested elements:
claim cards / thread / table rows / red paper tag

English handwritten labels:
claim / row / n=12 / not enough data

Color use:
Black for main line art and Xiaohei. Orange for the threads. Red only for the tag "not enough data". Blue only for the small "n=12" note.

Constraints:
One image explains only one core structure. Keep the main subject around 40%-60% of the canvas. Preserve at least 35% blank white space. Use at most 5 short handwritten English labels. Do not write a title in the top-left corner. Do not write the structure type on the image. Do not make it a formal diagram, course slide, or dense explainer. Invent a fresh visual metaphor; do not copy prior examples. Clear but not instructional, interesting but not childish, strange but clean. English labels only.
```

## Prompt 2 — `02-every-hand-balances.png`

```text
Generate one standalone 16:9 horizontal English article illustration.

Visual DNA:
Pure white background. Minimalist black hand-drawn line art. Slightly wobbly pen lines. Lots of empty white space. Sparse red/orange/blue handwritten English annotations. Clean absurd product-sketch feeling. No gradients, no shadows, no paper texture, no complex background, no commercial vector style, no PPT infographic look, no cute mascot poster, no children's illustration, no realistic UI.

Recurring IP character required:
Xiaohei, a small solid-black absurd creature with white dot eyes, tiny thin legs, blank serious expression, slightly uneven hand-drawn body shape. Xiaohei must perform the core conceptual action, not decorate the scene. Make Xiaohei serious, deadpan, and slightly bizarre, not cute.

Theme:
The money model of a poker hand-history parser: what everyone put in must equal the pot plus rake plus whatever was returned uncalled, for every single hand.

Structure type:
conceptual metaphor

Core idea:
The hand file never states the returned amount, but it is exactly derivable, and the tool refuses to trust itself until every hand on disk balances.

Composition:
A large old-fashioned two-pan balance scale in the center. Left pan: a loose heap of small chips. Right pan: one neat pot of chips and a tiny separate cup. Xiaohei stands on a stool at the scale, deadpan, handing one leftover chip back over the edge of the scale to an off-frame hand, with an orange arrow showing the chip leaving. The scale beam is perfectly level. Small blue tally in the lower right corner. Lots of white space above.

Suggested elements:
two-pan scale / heap of chips / pot with tiny rake cup / one returned chip

English handwritten labels:
everyone put in / pot + rake / returned / 11,272 / 11,272

Color use:
Black for main line art and Xiaohei. Orange for the arrow of the returned chip. Red for nothing or only the word "returned". Blue for the tally "11,272 / 11,272".

Constraints:
One image explains only one core structure. Keep the main subject around 40%-60% of the canvas. Preserve at least 35% blank white space. Use at most 5 short handwritten English labels. Do not write a title in the top-left corner. Do not write the structure type on the image. Do not make it a formal diagram, course slide, or dense explainer. Invent a fresh visual metaphor; do not copy prior examples. Clear but not instructional, interesting but not childish, strange but clean. English labels only.
```

## Prompt 3 — `03-no-lifetime-average.png`

```text
Generate one standalone 16:9 horizontal English article illustration.

Visual DNA:
Pure white background. Minimalist black hand-drawn line art. Slightly wobbly pen lines. Lots of empty white space. Sparse red/orange/blue handwritten English annotations. Clean absurd product-sketch feeling. No gradients, no shadows, no paper texture, no complex background, no commercial vector style, no PPT infographic look, no cute mascot poster, no children's illustration, no realistic UI.

Recurring IP character required:
Xiaohei, a small solid-black absurd creature with white dot eyes, tiny thin legs, blank serious expression, slightly uneven hand-drawn body shape. Xiaohei must perform the core conceptual action, not decorate the scene. Make Xiaohei serious, deadpan, and slightly bizarre, not cute.

Theme:
Poker statistics that are always scoped to a time window because the player changed over eleven months.

Structure type:
before/after contrast

Core idea:
An all-time average blends several different players into one meaningless number; slicing by window keeps each version of the player separate.

Composition:
Left side: one long horizontal paper strip like a receipt, with three or four slightly different small Xiaohei silhouettes drawn along it as if they were different people, all squeezed into a single messy circled number. Orange arrow in the middle. Right side: Xiaohei with a large pair of scissors, seriously cutting the same strip into short separate pieces, each piece laid down flat with its own small number. The most recent piece is slightly in front. Plenty of white space at the top.

Suggested elements:
long paper strip / scissors / cut pieces / one blended circled number

English handwritten labels:
11 months / several players / last 90 days / no lifetime

Color use:
Black for main line art and Xiaohei. Orange for the middle arrow and the cut lines. Red only for "no lifetime" crossing out the blended number. Blue for "last 90 days".

Constraints:
One image explains only one core structure. Keep the main subject around 40%-60% of the canvas. Preserve at least 35% blank white space. Use at most 5 short handwritten English labels. Do not write a title in the top-left corner. Do not write the structure type on the image. Do not make it a formal diagram, course slide, or dense explainer. Invent a fresh visual metaphor; do not copy prior examples. Clear but not instructional, interesting but not childish, strange but clean. English labels only.
```

## Prompt 4 — `04-effective-not-announced.png`

```text
Generate one standalone 16:9 horizontal English article illustration.

Visual DNA:
Pure white background. Minimalist black hand-drawn line art. Slightly wobbly pen lines. Lots of empty white space. Sparse red/orange/blue handwritten English annotations. Clean absurd product-sketch feeling. No gradients, no shadows, no paper texture, no complex background, no commercial vector style, no PPT infographic look, no cute mascot poster, no children's illustration, no realistic UI.

Recurring IP character required:
Xiaohei, a small solid-black absurd creature with white dot eyes, tiny thin legs, blank serious expression, slightly uneven hand-drawn body shape. Xiaohei must perform the core conceptual action, not decorate the scene. Make Xiaohei serious, deadpan, and slightly bizarre, not cute.

Theme:
Measuring a poker bet by what the opponent can actually call, not by what was pushed forward.

Structure type:
conceptual metaphor

Core idea:
Shoving five euros into a two-euro pot against an opponent with one euro left is a half-pot bet, not a two-and-a-half-pot bet. The tall stack above the opponent's reach does not count.

Composition:
Center-left: a tall wobbly tower of chips pushed forward. Center-right: a very small stack of one chip belonging to the opponent, with a dotted horizontal line drawn from its top across to the tower. Xiaohei stands on a small stepladder holding a long wooden ruler vertically against the tower, but the ruler is deliberately short and ends exactly at the dotted line; the part of the tower above the line is drawn in lighter sketchy lines as if it does not count. Big white space on the right.

Suggested elements:
tall chip tower / tiny opposing stack / dotted reach line / short ruler / stepladder

English handwritten labels:
announced €5 / €1 behind / effective 50% / does not count

Color use:
Black for main line art and Xiaohei. Orange for the dotted reach line. Red only for "does not count" beside the faded upper tower. Blue for "effective 50%".

Constraints:
One image explains only one core structure. Keep the main subject around 40%-60% of the canvas. Preserve at least 35% blank white space. Use at most 5 short handwritten English labels. Do not write a title in the top-left corner. Do not write the structure type on the image. Do not make it a formal diagram, course slide, or dense explainer. Invent a fresh visual metaphor; do not copy prior examples. Clear but not instructional, interesting but not childish, strange but clean. English labels only.
```

## Prompt 5 — `05-tree-shape-decides.png`

```text
Generate one standalone 16:9 horizontal English article illustration.

Visual DNA:
Pure white background. Minimalist black hand-drawn line art. Slightly wobbly pen lines. Lots of empty white space. Sparse red/orange/blue handwritten English annotations. Clean absurd product-sketch feeling. No gradients, no shadows, no paper texture, no complex background, no commercial vector style, no PPT infographic look, no cute mascot poster, no children's illustration, no realistic UI.

Recurring IP character required:
Xiaohei, a small solid-black absurd creature with white dot eyes, tiny thin legs, blank serious expression, slightly uneven hand-drawn body shape. Xiaohei must perform the core conceptual action, not decorate the scene. Make Xiaohei serious, deadpan, and slightly bizarre, not cute.

Theme:
A poker solver's answer depends more on the shape of the game tree it was given than on any setting.

Structure type:
small comic sequence, two panels side by side

Core idea:
Cutting raise branches off the tree to save memory changes the solver's answer by up to fifty points; a pruned tree answers a different question.

Composition:
Two panels separated by a thin vertical line. Left panel: a small hand-drawn tree with several forking branches, one single fruit hanging from it with a number written on it; Xiaohei stands below holding large garden shears, looking up. Right panel: the same tree with most branches cut off and lying on the ground, the same fruit now bearing a very different number; Xiaohei holds the shears, deadpan, with a small red note. Keep both panels sparse with white space above the trees.

Suggested elements:
forking tree / garden shears / one fruit with a number / cut branches on the ground

English handwritten labels:
full tree / c-bet 30% / raises cut / c-bet 80% / different question

Color use:
Black for main line art and Xiaohei. Orange for the cut marks on the branches. Red only for "different question". Blue for the two percentage numbers.

Constraints:
One image explains only one core structure. Keep the main subject around 40%-60% of the canvas. Preserve at least 35% blank white space. Use at most 5 short handwritten English labels. Do not write a title in the top-left corner. Do not write the structure type on the image. Do not make it a formal diagram, course slide, or dense explainer. Invent a fresh visual metaphor; do not copy prior examples. Clear but not instructional, interesting but not childish, strange but clean. English labels only.
```

## Prompt 6 — `06-fold-weakest-first.png`

```text
Generate one standalone 16:9 horizontal English article illustration.

Visual DNA:
Pure white background. Minimalist black hand-drawn line art. Slightly wobbly pen lines. Lots of empty white space. Sparse red/orange/blue handwritten English annotations. Clean absurd product-sketch feeling. No gradients, no shadows, no paper texture, no complex background, no commercial vector style, no PPT infographic look, no cute mascot poster, no children's illustration, no realistic UI.

Recurring IP character required:
Xiaohei, a small solid-black absurd creature with white dot eyes, tiny thin legs, blank serious expression, slightly uneven hand-drawn body shape. Xiaohei must perform the core conceptual action, not decorate the scene. Make Xiaohei serious, deadpan, and slightly bizarre, not cute.

Theme:
Modelling an opponent who folds half the time: the wrong way treats every hand alike, the right way folds the weakest hands first.

Structure type:
before/after contrast

Core idea:
Telling a solver the opponent folds 50% of every hand, strong ones included, produces an absurd opponent that the solver bets into with everything; dealing the folds weakest-first gives a sane answer.

Composition:
Left side: a short row of face-up playing cards, one clearly strong (three of a kind), and Xiaohei standing over them flipping a coin in the air above each card, deadpan; one coin hovers over the strong hand with a small red question note. Orange arrow in the middle. Right side: the same cards now arranged in a sloped line from weakest to strongest, and Xiaohei calmly pushing the bottom half of the line over a small edge into a bin, leaving the strong hand untouched at the top. Generous white space above both scenes.

Suggested elements:
playing cards / a coin in the air / sloped line of cards / small bin

English handwritten labels:
fold 50% / even the set? / weakest first / ranked

Color use:
Black for main line art and Xiaohei. Orange for the middle arrow and the push direction. Red only for "even the set?". Blue for "ranked".

Constraints:
One image explains only one core structure. Keep the main subject around 40%-60% of the canvas. Preserve at least 35% blank white space. Use at most 5 short handwritten English labels. Do not write a title in the top-left corner. Do not write the structure type on the image. Do not make it a formal diagram, course slide, or dense explainer. Invent a fresh visual metaphor; do not copy prior examples. Clear but not instructional, interesting but not childish, strange but clean. English labels only.
```

---

## QA checklist per generated image (from the skill)

aspect ratio 16:9 · pure white background · Xiaohei performs the core action
(remove Xiaohei and the metaphor should break) · ≤ 5 labels, legible · no
top-left title · frame ≥ 35% empty · not a flowchart / PPT · not cute.
If any fails: regenerate, or use the skill's edit prompt for title removal.
