from flask import Flask, request, render_template, redirect
import pandas as pd
from sqlalchemy import create_engine
import os
from itertools import combinations
from collections import defaultdict
import tempfile
from dotenv import load_dotenv
load_dotenv()
app = Flask(__name__)

# PostgreSQL 연결
POSTGRES_URL = os.environ.get("POSTGRES_URL", "postgresql://user:pass@host:port/dbname")
engine = create_engine(POSTGRES_URL)

@app.route("/")
def index():
    df = pd.read_sql("SELECT DISTINCT date, game_number FROM quarter_entries ORDER BY date, game_number", engine)
    games_by_date = defaultdict(list)
    for _, row in df.iterrows():
        games_by_date[row["date"]].append(row["game_number"])
    return render_template("index.html", games_by_date=games_by_date)


@app.route("/player")
def player_page():
    df = pd.read_sql("SELECT player_name, date, game_number, quarter, team, score_margin FROM quarter_entries", engine)
    selected_players = request.args.getlist('selected')
    player_set = sorted(df["player_name"].unique())

    history = {}
    total_margin = 0
    player_stats = {}

    if 1 <= len(selected_players) <= 5:
        quarter_to_players = defaultdict(set)
        quarter_to_teams = defaultdict(dict)
        quarter_to_margins = defaultdict(dict)

        for _, row in df.iterrows():
            name = row["player_name"]
            date = row["date"]
            game = row["game_number"]
            qtr = row["quarter"]
            team = row["team"]
            margin = row["score_margin"]
            qid = f"{date}|{game}|{qtr}"
            quarter_to_players[qid].add(name)
            quarter_to_teams[qid][name] = team
            if name in selected_players:
                adjusted = margin if team == 'A' else -margin
                quarter_to_margins[qid][name] = adjusted

        valid_quarters = []
        for qid, players in quarter_to_players.items():
            if all(p in players for p in selected_players):
                teams = [quarter_to_teams[qid][p] for p in selected_players]
                if len(set(teams)) == 1:
                    valid_quarters.append(qid)

        if valid_quarters:
            for name in selected_players:
                history[name] = []
                for qid in valid_quarters:
                    if name in quarter_to_margins[qid]:
                        date, game, qtr = qid.split('|')
                        simple_date = pd.to_datetime(date).strftime('%m-%d')
                        label = f"{simple_date} G{game}Q{qtr}"
                        history[name].append((label, quarter_to_margins[qid][name]))

            total_margin = sum(
                sum(quarter_to_margins[qid][p] for p in selected_players) // len(selected_players)
                for qid in valid_quarters
            )

            for name in selected_players:
                quarters = history.get(name, [])
                count = len(quarters)
                win = sum(1 for _, margin in quarters if margin >= 0)
                win_rate = round((win / count) * 100, 1) if count > 0 else 0.0
                player_stats[name] = {
                    "count": count,
                    "win": win,
                    "win_rate": win_rate
                }

    return render_template(
        "player.html",
        players=player_set,
        selected_players=selected_players,
        total_margin=total_margin,
        history=history,
        player_stats=player_stats
    )

@app.route("/game")
def show_game():
    key = request.args.get("key")
    if not key or '|' not in key:
        return "잘못된 요청입니다"
    date, game_number = key.split('|')
    query = f"""
        SELECT quarter, team, player_name, player_order, team_a_score, team_b_score, score_margin
        FROM quarter_entries
        WHERE date = %s AND game_number = %s
        ORDER BY quarter, team, player_order
    """
    df = pd.read_sql(query, engine, params=(date, game_number))
    data = {}
    for _, row in df.iterrows():
        qtr = row["quarter"]
        team = row["team"]
        if qtr not in data:
            data[qtr] = {
                "A": [], "B": [],
                "score_a": row["team_a_score"],
                "score_b": row["team_b_score"],
                "margin": row["score_margin"]
            }
        data[qtr][team].append(row["player_name"])
    return render_template("game.html", date=date, game_number=game_number, game_data=data)

@app.route("/ranking")
def ranking_page():
    min_games = request.args.get("min_games", type=int)
    if min_games is None or min_games < 1:
        min_games = 10
    df = pd.read_sql("SELECT * FROM quarter_entries", engine)

    quarter_team_players = defaultdict(lambda: defaultdict(list))
    quarter_team_scores = defaultdict(dict)
    for _, row in df.iterrows():
        qid = row["quarter_id"]
        team = row["team"]
        player = row["player_name"]
        score = row["team_a_score"] if team == "A" else row["team_b_score"]
        opponent_score = row["team_b_score"] if team == "A" else row["team_a_score"]
        margin = score - opponent_score
        quarter_team_players[qid][team].append(player)
        quarter_team_scores[qid][team] = margin

    combo_stats = defaultdict(lambda: {"margin": 0, "count": 0, "wins": 0})
    for qid in quarter_team_players:
        for team in quarter_team_players[qid]:
            margin = quarter_team_scores[qid][team]
            players = quarter_team_players[qid][team]
            for r in range(1, 6):
                for combo in combinations(sorted(players), r):
                    combo_stats[combo]["margin"] += margin
                    combo_stats[combo]["count"] += 1
                    if margin >= 0:
                        combo_stats[combo]["wins"] += 1

    records = []
    for combo, stats in combo_stats.items():
        if stats["count"] < min_games:
            continue
        records.append({
            "players": ", ".join(combo),
            "size": len(combo),
            "games": stats["count"],
            "margin": stats["margin"],
            "winrate": round(stats["wins"] / stats["count"] * 100, 1)
        })

    df_combo = pd.DataFrame(records)
    if df_combo.empty:
        df_combo = pd.DataFrame(columns=["players", "size", "games", "margin", "winrate", "type"])

    top_margin = df_combo[df_combo["margin"] > 0].sort_values("margin", ascending=False).groupby("size", group_keys=False).head(5).assign(type="TOP_MARGIN")
    top_winrate = df_combo[df_combo["games"] >= 3].sort_values(["winrate", "games"], ascending=[False, False]).groupby("size", group_keys=False).head(5).assign(type="TOP_WINRATE")
    worst_margin = df_combo[df_combo["margin"] < 0].sort_values("margin").groupby("size", group_keys=False).head(5).assign(type="WORST_MARGIN")
    worst_winrate = df_combo[df_combo["games"] >= 3].sort_values(["winrate", "games"], ascending=[True, False]).groupby("size", group_keys=False).head(5).assign(type="WORST_WINRATE")

    df_final = pd.concat([top_margin, top_winrate, worst_margin, worst_winrate], ignore_index=True)
    return render_template("ranking.html", ranking=df_final.to_dict("records"), min_games=min_games)

@app.route("/input", methods=["GET", "POST"])
def input_page():
    password = request.form.get("password") if request.method == 'POST' else None
    authenticated = password == "9091"
    return render_template("input.html", authenticated=authenticated)

@app.route("/input/upload", methods=["POST"])
def upload_excel():
    file = request.files.get("excel_file")
    if not file:
        return "파일이 없습니다."
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        file.save(tmp.name)
        filepath = tmp.name

    df = pd.read_excel(filepath, header=None)
    results = []
    row = 0
    prev_a_score = 0
    prev_b_score = 0
    current_game_number = None

    while row + 6 < len(df):
        try:
            date = str(df.iloc[row, 0])
            game_number = int(df.iloc[row, 1])
            quarter = int(df.iloc[row, 2])
        except:
            break

        try:
            total_a_score = int(df.iloc[row + 6, 0])
            total_b_score = int(df.iloc[row + 6, 1])
        except:
            break

        if game_number != current_game_number:
            prev_a_score = 0
            prev_b_score = 0
            current_game_number = game_number

        quarter_id = f"{date}_{game_number}_{quarter}"
        a_players = df.iloc[row+1:row+6, 0].dropna().tolist()
        b_players = df.iloc[row+1:row+6, 1].dropna().tolist()

        quarter_a_score = total_a_score - prev_a_score
        quarter_b_score = total_b_score - prev_b_score
        margin = quarter_a_score - quarter_b_score

        prev_a_score = total_a_score
        prev_b_score = total_b_score

        for i, name in enumerate(a_players, 1):
            results.append((quarter_id, date, game_number, quarter, name.strip(), "A", i, quarter_a_score, quarter_b_score, margin))
        for i, name in enumerate(b_players, 1):
            results.append((quarter_id, date, game_number, quarter, name.strip(), "B", i, quarter_a_score, quarter_b_score, margin))

        row += 7

    df_new = pd.DataFrame(results, columns=["quarter_id", "date", "game_number", "quarter", "player_name", "team", "player_order", "team_a_score", "team_b_score", "score_margin"])
    df_new.to_sql("quarter_entries", engine, if_exists="append", index=False)
    return redirect("/input")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
