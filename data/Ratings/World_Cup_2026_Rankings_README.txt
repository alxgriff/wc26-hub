========================================================================
WORLD CUP 2026 RANKINGS DATASET - README
========================================================================

This folder contains 3 distinct predictive ranking models for the 48 
teams participating in the 2026 World Cup. Below is the data dictionary 
for each CSV file.

------------------------------------------------------------------------
1. Elo_Ratings_World_Cup_2026.csv
------------------------------------------------------------------------
A purely performance-based, zero-sum rating system tracking historical 
results, match importance, goal differentials, and opponent strength.

Columns:
* Team: The name of the country.
* Elo_Rating: The current numerical strength rating of the team. Higher 
  numbers represent stronger teams (baselines generally range from 1400 to 2200).
* 1_Year_Point_Change: The net number of Elo points the team has gained 
  or lost over the trailing 12 months.
* Global_Elo_Rank: The relative rank of the team strictly within this 
  48-team World Cup dataset based on their Elo rating.

------------------------------------------------------------------------
2. Opta_Predictions_World_Cup_2026.csv
------------------------------------------------------------------------
A data-heavy simulation model from Stats Perform utilizing granular, 
player-level expected goals (xG) and team power metrics across 25,000 
Monte Carlo simulations.

Columns:
* Team: The name of the country.
* Win_Tournament_%: The statistical probability of the team winning the final.
* Reach_Final_%: The probability of the team qualifying for the final match.
* Reach_Semi_Final_%: The probability of reaching the final four.
* Reach_Quarter_Final_%: The probability of reaching the final eight.
* Advance_From_Group_%: The probability of surviving the group stage and 
  moving into the knockout rounds.
* Opta_Rank: Position from 1 to 48 sorted by highest tournament win probability.

------------------------------------------------------------------------
3. Zeileis_Hybrid_Model_World_Cup_2026.csv
------------------------------------------------------------------------
An ensemble machine learning model combining retrospective team math, 
Transfermarkt player valuation data, and forward-looking international 
bookmaker consensus liquidity.

Columns:
* Team: The name of the country.
* Win_Probability_%: The combined model probability of tournament victory.
* Simulated_Final_Appearances_per_100k: The raw count of times the team 
  made it to the final match across 100,000 full tournament simulations.
* Transfermarkt_Squad_Value_Millions_EUR: Estimated cumulative market value 
  of the national team's roster in millions of Euros (EUR).
* Bookmaker_Consensus_Odds_Decimal: The averaged market decimal odds. 
  NOTE ON DECIMAL ODDS: This represents the TOTAL PAYOUT per unit staked, 
  not profit ratio. For example, odds of 6.9 mean a $1 bet returns a total 
  of $6.90 ($5.90 profit + $1 original stake returned). This translates 
  to traditional fractional odds of 5.9:1.
* Zeileis_Rank: Position from 1 to 48 sorted by highest win probability.

------------------------------------------------------------------------
4. Futi_Detailed_Profiles_Final.csv
------------------------------------------------------------------------
Extracted directly from the Futi soccer analytics application. Futi goes 
beyond standard outcome-based metrics by utilizing advanced possession models, 
granular tracking data, and expected action values. Created by soccer analysts, 
the platform assesses the underlying stylistic tendencies of teams (e.g., measuring 
"patient buildup" versus "chaos") and evaluates the micro-actions of individual 
players. This makes their rating system highly predictive, as it isolates sustainable 
tactical advantages and true player value rather than simply relying on past 
match scores.

Columns:
* Team: Name of the country.
* Futi_Rating: Overall power rating assigned by the app based on underlying performance metrics.
* Attack: Offensive efficiency score.
* Defense: Defensive solidity score.
* Formation: The primary tactical lineup shape preferred by the team (formatted with spaces to prevent spreadsheet software from auto-converting to a date).
* Top_Player: The name of the highest-rated individual player highlighted by the app.
* Top_Player_Rating: The rating score of that top player.
* Coach: The current head coach name (marked 'N/A' if omitted from screen view).

