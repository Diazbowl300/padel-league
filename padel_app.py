import streamlit as st
import sqlite3
import pandas as pd
import math

# --- CONFIGURATION ---
DB_FILE = "padel_league.db"
K_FACTOR_STANDARD = 32
K_FACTOR_PROVISIONAL = 64
PROVISIONAL_LIMIT = 5
RATING_FLOOR = 100  # Equivalent to 1.0
SCALING_FACTOR = 100 # To convert 1.0 -> 100

# --- DATABASE FUNCTIONS ---

def init_db():
    """Initialize the SQLite database with players and matches tables."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Create Players Table
    c.execute('''CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    rating REAL,
                    matches_played INTEGER
                )''')
    
    # Create Matches Table
    c.execute('''CREATE TABLE IF NOT EXISTS matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    p1_id INTEGER, p2_id INTEGER,
                    p3_id INTEGER, p4_id INTEGER,
                    score_team1 INTEGER,
                    score_team2 INTEGER,
                    rating_change_team1 REAL,
                    rating_change_team2 REAL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    conn.commit()
    conn.close()

def add_player(name, initial_rating_display):
    """Add a new player. Input rating is 1.0-10.0, stored as 100-1000."""
    backend_rating = initial_rating_display * SCALING_FACTOR
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO players (name, rating, matches_played) VALUES (?, ?, ?)", 
                  (name, backend_rating, 0))
        conn.commit()
        success = True
    except sqlite3.IntegrityError:
        success = False
    conn.close()
    return success

def get_players():
    """Fetch all players for dropdowns."""
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT id, name, rating, matches_played FROM players ORDER BY name", conn)
    conn.close()
    return df

def get_leaderboard():
    """Fetch leaderboard sorted by rating."""
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT name, rating, matches_played FROM players ORDER BY rating DESC", conn)
    conn.close()
    # Convert backend rating (100-1000) to frontend display (1.0-10.0)
    df['Display Rating'] = df['rating'] / SCALING_FACTOR
    return df

def get_match_history():
    """Fetch recent matches."""
    conn = sqlite3.connect(DB_FILE)
    query = '''
        SELECT 
            m.id, 
            p1.name as p1, p2.name as p2, 
            p3.name as p3, p4.name as p4,
            m.score_team1, m.score_team2,
            m.timestamp
        FROM matches m
        JOIN players p1 ON m.p1_id = p1.id
        JOIN players p2 ON m.p2_id = p2.id
        JOIN players p3 ON m.p3_id = p3.id
        JOIN players p4 ON m.p4_id = p4.id
        ORDER BY m.timestamp DESC
    '''
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

# --- ELO MATH LOGIC ---

def calculate_expected_score(rating_a, rating_b):
    """
    Logistic curve formula.
    Returns the win probability (0 to 1) for Team A.
    """
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))

def process_match(p1_id, p2_id, p3_id, p4_id, score_t1, score_t2):
    """
    Core algorithm:
    1. Get current ratings.
    2. Calculate Team Averages.
    3. Calculate Expected Score (Logistic).
    4. Calculate Actual Score (Margin of Victory).
    5. Determine K-Factor (Provisional vs Standard).
    6. Update Database.
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # 1. Fetch current data
    players = {}
    for pid in [p1_id, p2_id, p3_id, p4_id]:
        c.execute("SELECT rating, matches_played FROM players WHERE id=?", (pid,))
        players[pid] = c.fetchone()
    
    r1, m1 = players[p1_id]
    r2, m2 = players[p2_id]
    r3, m3 = players[p3_id]
    r4, m4 = players[p4_id]
    
    # 2. Team Ratings (Average)
    team1_rating = (r1 + r2) / 2
    team2_rating = (r3 + r4) / 2
    
    # 3. Expected Score
    expected_t1 = calculate_expected_score(team1_rating, team2_rating)
    
    # 4. Actual Score (Fractional / Margin of Victory)
    total_points = score_t1 + score_t2
    if total_points == 0:
        return False, "Total points cannot be zero."
        
    actual_t1 = score_t1 / total_points
    
    # 5. Calculate Changes
    # We calculate individual deltas because K-factors might differ per player
    # (e.g., a Newbie playing with a Pro)
    
    updates = []
    
    # Team 1 Updates
    for pid, rating, matches in [(p1_id, r1, m1), (p2_id, r2, m2)]:
        k = K_FACTOR_PROVISIONAL if matches < PROVISIONAL_LIMIT else K_FACTOR_STANDARD
        change = k * (actual_t1 - expected_t1)
        new_rating = max(RATING_FLOOR, rating + change)
        updates.append((pid, new_rating, matches + 1))
        
    # Team 2 Updates
    # Note: actual_t2 = 1 - actual_t1, expected_t2 = 1 - expected_t1
    # So (Actual - Expected) for T2 is equivalent to -(Actual_T1 - Expected_T1)
    for pid, rating, matches in [(p3_id, r3, m3), (p4_id, r4, m4)]:
        k = K_FACTOR_PROVISIONAL if matches < PROVISIONAL_LIMIT else K_FACTOR_STANDARD
        change = k * ((1 - actual_t1) - (1 - expected_t1))
        new_rating = max(RATING_FLOOR, rating + change)
        updates.append((pid, new_rating, matches + 1))

    # 6. Commit to DB
    # Update Players
    for pid, new_r, new_m in updates:
        c.execute("UPDATE players SET rating=?, matches_played=? WHERE id=?", (new_r, new_m, pid))
    
    # Record Match
    # Storing the rating change for Team 1 (avg) for historical reference
    # We just grab the delta calculated for the first player of T1 as a reference point
    ref_change = updates[0][1] - r1 
    
    c.execute('''INSERT INTO matches 
                 (p1_id, p2_id, p3_id, p4_id, score_team1, score_team2, rating_change_team1, rating_change_team2)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
              (p1_id, p2_id, p3_id, p4_id, score_t1, score_t2, ref_change, -ref_change))
    
    conn.commit()
    conn.close()
    return True, f"Match recorded! Rating change: {ref_change/SCALING_FACTOR:.3f} (Display Units)"

# --- STREAMLIT UI ---

def main():
    st.set_page_config(page_title="Padel Americano League", page_icon="🎾")
    init_db()

    st.title("🎾 Padel Americano League")
    st.markdown("### 2v2 Modified Elo System")

    menu = ["Leaderboard", "Record Match", "Register Player", "Match History"]
    choice = st.sidebar.selectbox("Navigation", menu)

    # --- LEADERBOARD TAB ---
    if choice == "Leaderboard":
        st.header("🏆 League Standings")
        df = get_leaderboard()
        
        if not df.empty:
            # Formatting for display
            df['Rank'] = range(1, len(df) + 1)
            df = df[['Rank', 'name', 'Display Rating', 'matches_played']]
            df.columns = ['Rank', 'Player', 'Rating (1.0-10.0)', 'Games Played']
            
            # Highlight top players
            st.dataframe(
                df.style.format({"Rating (1.0-10.0)": "{:.2f}"}),
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("No players registered yet.")

    # --- RECORD MATCH TAB ---
    elif choice == "Record Match":
        st.header("📝 Record Match Result")
        
        player_df = get_players()
        
        if len(player_df) < 4:
            st.warning("You need at least 4 players registered to record a match.")
        else:
            # Create a dictionary for dropdowns {name: id}
            player_dict = dict(zip(player_df['name'], player_df['id']))
            player_names = list(player_dict.keys())

            col1, col2, col3 = st.columns([1, 0.2, 1])

            with col1:
                st.subheader("Team 1")
                t1_p1 = st.selectbox("Player 1", player_names, key="p1")
                t1_p2 = st.selectbox("Player 2", player_names, key="p2")
                score_t1 = st.number_input("Team 1 Points", min_value=0, value=0)

            with col3:
                st.subheader("Team 2")
                t2_p1 = st.selectbox("Player 3", player_names, key="p3")
                t2_p2 = st.selectbox("Player 4", player_names, key="p4")
                score_t2 = st.number_input("Team 2 Points", min_value=0, value=0)

            with col2:
                st.write("")
                st.write("")
                st.markdown("<h2 style='text-align: center;'>VS</h2>", unsafe_allow_html=True)

            if st.button("Submit Result", type="primary"):
                # Validation
                selected_players = [t1_p1, t1_p2, t2_p1, t2_p2]
                if len(set(selected_players)) != 4:
                    st.error("Error: You selected the same player multiple times.")
                elif score_t1 == 0 and score_t2 == 0:
                    st.error("Error: Total points cannot be zero.")
                else:
                    # Process
                    p1_id, p2_id = player_dict[t1_p1], player_dict[t1_p2]
                    p3_id, p4_id = player_dict[t2_p1], player_dict[t2_p2]
                    
                    success, msg = process_match(p1_id, p2_id, p3_id, p4_id, score_t1, score_t2)
                    if success:
                        st.success(msg)
                    else:
                        st.error(msg)

    # --- REGISTER PLAYER TAB ---
    elif choice == "Register Player":
        st.header("👤 Register New Player")
        
        with st.form("add_player_form"):
            new_name = st.text_input("Player Name")
            initial_rating = st.slider("Self-Assessed Level (1.0 = Beginner, 10.0 = Pro)", 
                                     min_value=1.0, max_value=10.0, value=3.0, step=0.1)
            
            submitted = st.form_submit_button("Add Player")
            
            if submitted:
                if new_name:
                    if add_player(new_name, initial_rating):
                        st.success(f"Player '{new_name}' added with rating {initial_rating}!")
                    else:
                        st.error("Player name already exists.")
                else:
                    st.error("Please enter a name.")

    # --- HISTORY TAB ---
    elif choice == "Match History":
        st.header("📜 Recent Matches")
        df = get_match_history()
        
        if not df.empty:
            for index, row in df.iterrows():
                with st.container():
                    st.markdown(f"""
                    **{row['timestamp']}** {row['p1']} & {row['p2']} **({row['score_team1']})** vs **({row['score_team2']})** {row['p3']} & {row['p4']}
                    ___
                    """)
        else:
            st.info("No matches recorded yet.")

if __name__ == "__main__":
    main()
