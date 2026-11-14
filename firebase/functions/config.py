"""Server configuration constants."""

# The ladder's modes and the seats in each game. A bot registers for one mode;
# its rating and record in mode m are the bot document's elo{m}, games{m} and
# wins{m} fields. 3p plays 4p's rules with one seat fewer.
MODES = {"1v1": 2, "3p": 3, "4p": 4}
ACTION_TIMEOUT_SECONDS = 60
# A seat past its deadline has the pending move made for it and a strike
# recorded; this many strikes in a row abort the game (1: abort at once).
TIMEOUT_STRIKES = 3
RENAME_COOLDOWN_SECONDS = 24 * 60 * 60   # a bot may change its name once a day
ELO_K_FACTOR = 32.0
INITIAL_ELO = 1500.0
GCS_REPLAY_BUCKET = "openhex-replays"
