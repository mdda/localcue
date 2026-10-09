"""Voice-follow: local speech recognition (Vosk) that moves the script position
to match what the presenter is saying.

- Audio comes from PulseAudio's API (served by PipeWire's pipewire-pulse) via
  libpulse-simple — the default source unless a device is given.
- Recognition is fully local (Vosk; the small English model is downloaded on
  first use to ~/.cache/vosk).
- Matching is deliberately loose: the presenter may riff, read bullet points,
  or repeat a line after a fluff. Only a confident run of matching words moves
  the position, and only within a window from a little behind the current
  position (repeats) to a few hundred characters ahead.

Matching ideas borrowed from promptme-ai (github.com/larsbaunwall/promptme-ai):
phonetic tokens (Double Metaphone), a/the and an/and folding, stutter collapse,
word-level edit-distance window scoring, and a penalty on distance ahead.
"""

import bisect
import json
import re
import threading

from metaphone import doublemetaphone

# ---- Matching parameters (in words) ----
SPOKEN_WORDS   = 6     # how many of the most recent spoken words to match
MIN_WORDS      = 3     # need at least this many before moving at all
LOOKBACK       = 25    # words behind the current position still considered (repeats)
LOOKAHEAD      = 60    # words ahead considered (~300+ characters)
HALVING_DIST   = 30    # a match this far ahead needs double the score of a nearby one
ACCEPT_SCORE   = 0.5   # minimum distance-adjusted similarity to move
MAX_DRIFT      = 40    # cap on "words heard since the last match" used to estimate progress

SAMPLE_RATE = 16000
DEFAULT_MODEL = "vosk-model-small-en-us-0.15"

WORD_RE = re.compile(r"[A-Za-z0-9']+")
# Recognisers often swap these; Double Metaphone can't fold them on its own.
FOLD = {"a": "@art", "the": "@art", "an": "@and", "and": "@and"}


def norm(word):
    """Phonetic token for a word, so homophones (right/write, there/their) match."""
    w = word.lower().replace("'", "")
    if w in FOLD:
        return FOLD[w]
    if w.isdigit():
        return w
    return doublemetaphone(w)[0] or w


def similarity(a, b):
    """Word-level Levenshtein similarity in [0, 1]."""
    if not a or not b:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return 1 - prev[-1] / max(len(a), len(b))


class Matcher:
    """Finds where recently spoken words are in the script."""

    def __init__(self, text=""):
        self.set_script(text)

    def set_script(self, text):
        self.text = text
        self.tokens, self.starts = [], []
        for m in WORD_RE.finditer(text):
            self.tokens.append(norm(m.group()))
            self.starts.append(m.start())

    def word_at(self, pos):
        """Index of the word at (or just before) character position pos."""
        return max(0, bisect.bisect_right(self.starts, pos) - 1)

    def match(self, spoken_words, pos, unmatched=0):
        """Given recently spoken words (oldest first) and the current position (chars),
        return the character position of the last matched word, or None to stay put.

        unmatched = words heard since the position last moved. The presenter has
        probably advanced about that far, so the stretch up to there isn't penalised
        as "far ahead" — otherwise a run of misrecognitions could leave the matcher
        behind for good (each step further back makes catching up harder)."""
        spoken = []
        for w in spoken_words:
            t = norm(w)
            if not spoken or spoken[-1] != t:  # collapse stutters: "or or or" → "or"
                spoken.append(t)
        spoken = spoken[-SPOKEN_WORDS:]
        n = len(spoken)
        if n < MIN_WORDS or not self.tokens:
            return None

        cur = self.word_at(pos)
        est = cur + min(unmatched, MAX_DRIFT)  # estimated progress since the last match
        best_score, best_end, best_dist = ACCEPT_SCORE, None, None
        lo = max(0, cur - LOOKBACK)
        hi = min(len(self.tokens), est + LOOKAHEAD)
        for start in range(lo, hi):
            dist_ahead = max(0, start - est)  # no penalty for looking back (repeats) or up to est
            locality = 1 / (1 + dist_ahead / HALVING_DIST)
            for length in (n - 1, n, n + 1):  # slack for a dropped/extra word
                end = start + length
                if length < MIN_WORDS or end > len(self.tokens):
                    continue
                score = similarity(spoken, self.tokens[start:end]) * locality
                dist = abs(start - cur)
                if score > best_score or (score == best_score and best_dist is not None and dist < best_dist):
                    best_score, best_end, best_dist = score, end, dist
        if best_end is None:
            return None
        return self.starts[best_end - 1]


class VoiceFollower:
    """Background thread: microphone → Vosk → Matcher → on_position(pos)."""

    def __init__(self, get_text, get_pos, on_position, on_status, model=DEFAULT_MODEL, device=None):
        self.get_text = get_text        # () -> current script text
        self.get_pos = get_pos          # () -> current position (chars)
        self.on_position = on_position  # (pos) -> None, when speech matches the script
        self.on_status = on_status      # (dict) -> None: {state, heard?, error?}
        self.model_spec = model
        self.device = device
        self.matcher = Matcher()
        self._history = []              # recent finalised words
        self._last_spoken = None
        self._final_count = 0           # total finalised words heard (for `unmatched`)
        self._mark = None               # word count at the last move (None = set on next feed)
        self._reset_recognizer = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._model = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            if not self._stop.is_set():
                return                      # already listening
            self._thread.join(timeout=2)    # switched off and straight back on: let it finish
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="voice-follow", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def reset(self):
        """Forget what was heard — call when the position is moved manually, so
        old words don't pull it back."""
        with self._lock:
            self._history = []
            self._last_spoken = None
            self._mark = None
            self._reset_recognizer = True  # also drop the utterance in progress

    def _load_model(self):
        if self._model is None:
            import os
            import vosk
            vosk.SetLogLevel(-1)
            spec = self.model_spec
            self._model = vosk.Model(model_path=spec) if os.path.isdir(spec) else vosk.Model(model_name=spec)
        return self._model

    def _run(self):
        try:
            self.on_status({"state": "loading", "heard": "loading speech model…"})
            import vosk
            import pasimple
            rec = vosk.KaldiRecognizer(self._load_model(), SAMPLE_RATE)
            # fragsize ≈ 0.1 s keeps capture latency low (the server default can be ~2 s).
            chunk = SAMPLE_RATE * 2 // 10
            with pasimple.PaSimple(pasimple.PA_STREAM_RECORD, pasimple.PA_SAMPLE_S16LE, 1, SAMPLE_RATE,
                                   app_name="LocalCue", stream_name="voice-follow",
                                   device_name=self.device, fragsize=chunk) as pa:
                self.on_status({"state": "listening", "heard": ""})
                while not self._stop.is_set():
                    self.feed(rec, pa.read(chunk))
            self.on_status({"state": "off"})
        except Exception as e:  # report anything (missing model, no mic, …) to the desktop
            self.on_status({"state": "error", "error": f"{type(e).__name__}: {e}"})

    def feed(self, rec, audio):
        """Process one chunk of 16 kHz mono s16le audio (separate from capture, for testing)."""
        with self._lock:
            if self._reset_recognizer:
                rec.Reset()
                self._reset_recognizer = False
        if rec.AcceptWaveform(audio):  # end of an utterance: its words become history
            words = json.loads(rec.Result()).get("text", "").split()
            partial = []
        else:
            words = []
            partial = json.loads(rec.PartialResult()).get("partial", "").split()
        with self._lock:
            self._final_count += len(words)
            self._history = (self._history + words)[-SPOKEN_WORDS * 2:]
            spoken = self._history + partial
            if spoken == self._last_spoken:  # nothing new heard (silence) — nothing to do
                return
            self._last_spoken = spoken
            total = self._final_count + len(partial)
            if self._mark is None:
                self._mark = total
            unmatched = max(0, total - self._mark)

        text = self.get_text()
        if text != self.matcher.text:
            self.matcher.set_script(text)
        pos = self.get_pos()
        new = self.matcher.match(spoken, pos, unmatched)
        self.on_status({"state": "listening", "heard": " ".join(spoken[-SPOKEN_WORDS * 2:])})
        if new is not None and self.matcher.word_at(new) != self.matcher.word_at(pos):
            with self._lock:
                self._mark = total
            self.on_position(new)
