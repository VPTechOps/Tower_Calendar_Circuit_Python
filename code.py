"""
Tower Light Event Indicator - CircuitPython for Raspberry Pi Pico 2W

Author:
  Frederick M Meyer (VPTechOps)

License:
  MIT

Hardware:
  - Raspberry Pi Pico 2W
  - Adafruit Tower Light w/ Buzzer (Product 2993)
  - Adafruit NeoRGB Stemma Adapter (Product 5888)  -> GP1
  - N-Channel MOSFET buzzer switch (Product 355)   -> GP2
  - Adafruit VEML7700 Lux Sensor (Product 4162)    -> STEMMA
  - Refresh button                                 -> GP0

Libraries needed in /lib:
  neopixel, adafruit_veml7700, adafruit_bus_device,
  adafruit_requests, adafruit_ntp, adafruit_connection_manager
"""

import os
import time
import board
import busio
import digitalio
import pwmio
import neopixel
import wifi
import socketpool
import ssl
import adafruit_requests
import adafruit_ntp
import adafruit_veml7700

# ── Pin constants ────────────────────────────────────────────────────────────
PIXEL_PIN   = board.GP1  # Board pin 2
NUM_PIXELS  = 1
BUZZER_PIN  = board.GP2  #           4
BUTTON_PIN  = board.GP0  #           1

# ── Buzzer ───────────────────────────────────────────────────────────────────
BUZZER_FREQ  = 1000
BUZZER_DUTY  = int(65535 * (180 / 511))   # ~35 % — equivalent to duty 180/511 on 9-bit

# ── Brightness / lux ────────────────────────────────────────────────────────
BRIGHTNESS_MAX = 220
BRIGHTNESS_MIN = 24
LUX_MAX        = 300
LUX_MIN        = 50

# ── NTP / time zone ──────────────────────────────────────────────────────────
GMT_OFFSET_HOURS   = int(os.getenv("UTC_OFFSET"))
NTP_REFRESH_SEC    = 3600
NTP_SOCKET_TIMEOUT = 10

# ── Adafruit IO ──────────────────────────────────────────────────────────────
AIO_USERNAME  = os.getenv("ADAFRUIT_AIO_USERNAME")
AIO_KEY       = os.getenv("ADAFRUIT_AIO_KEY")
AIO_FEED_KEY  = os.getenv("ADAFRUIT_AIO_FEED_KEY")
AIO_URL       = (
    f"https://io.adafruit.com/api/v2/{AIO_USERNAME}"
    f"/feeds/{AIO_FEED_KEY}/data?include=value"
)

# ── State constants ──────────────────────────────────────────────────────────
RED    =  0
YELLOW =  1
GREEN  =  2
OFF    = -1

# ── Default date strings ─────────────────────────────────────────────────────
DEFAULT_SDATE = "2000/01/01"
DEFAULT_EDATE = "2099/12/31"

# Day-of-week lookup: Python's struct_time tm_wday is 0=Mon … 6=Sun
# Map to the MTWRFSU scheme used in the feed
_DOW_MAP = {0: "M", 1: "T", 2: "W", 3: "R", 4: "F", 5: "S", 6: "U"}

# ── Hardware init ────────────────────────────────────────────────────────────
pixels = neopixel.NeoPixel(PIXEL_PIN, NUM_PIXELS, bpp=3,
                           pixel_order=neopixel.RGB, auto_write=False)

buzzer = pwmio.PWMOut(BUZZER_PIN, frequency=BUZZER_FREQ,
                      duty_cycle=0, variable_frequency=False)

button = digitalio.DigitalInOut(BUTTON_PIN)
button.direction  = digitalio.Direction.INPUT
button.pull       = digitalio.Pull.UP

# ── VEML7700 ─────────────────────────────────────────────────────────────────
veml_present = False
veml         = None
try:
    i2c = board.STEMMA_I2C()
    veml = adafruit_veml7700.VEML7700(i2c)
    veml_present = True
    print(f"VEML7700 found, lux = {veml.lux:.1f}")
except Exception as exc:                       # not wired up — carry on
    print(f"VEML7700 not found: {exc}")

# ── Mutable globals ───────────────────────────────────────────────────────────
brightness_current = BRIGHTNESS_MAX
color_current      = OFF          # force first-run update

tower_color = [
    (BRIGHTNESS_MAX, 0,              0),   # RED
    (0,              0,              BRIGHTNESS_MAX),   # YELLOW (NeoRGB: G/B swapped on tower)
    (0,              BRIGHTNESS_MAX, 0),   # GREEN
]

warn_flash_timer = 0.0
warn_flash_state = False

g_events = []          # list of dicts

requests_session = None
ntp = None


# ────────────────────────────────────────────────────────────────────────────
# WiFi helpers
# ────────────────────────────────────────────────────────────────────────────

def connect_wifi():
    """Connect to WiFi and return a requests Session."""
    print("Connecting to WiFi …")
    wifi.radio.connect(os.getenv("CIRCUITPY_WIFI_SSID"), os.getenv("CIRCUITPY_WIFI_PASSWORD"))
    print(f"Connected: {wifi.radio.ipv4_address}")
    pool    = socketpool.SocketPool(wifi.radio)
    session = adafruit_requests.Session(pool, ssl.create_default_context())
    return pool, session


# ────────────────────────────────────────────────────────────────────────────
# NTP helpers
# ────────────────────────────────────────────────────────────────────────────

def init_ntp(pool):
    global ntp
    ntp = adafruit_ntp.NTP(pool, tz_offset=GMT_OFFSET_HOURS, server="0.us.pool.ntp.org",
                           cache_seconds=NTP_REFRESH_SEC, socket_timeout=NTP_SOCKET_TIMEOUT)
    _ = ntp.datetime


# ────────────────────────────────────────────────────────────────────────────
# Current local time helpers
# ────────────────────────────────────────────────────────────────────────────

def get_now():
    """Return current local time as struct_time, using NTP object."""
    return ntp.datetime


def now_minutes(t):
    """Return minutes-since-midnight for a struct_time."""
    return t.tm_hour * 60 + t.tm_min


def now_date_str(t):
    """Return 'yyyy/mm/dd' string for a struct_time."""
    return f"{t.tm_year:04d}/{t.tm_mon:02d}/{t.tm_mday:02d}"


def now_dow_char(t):
    """Return single DOW character (MTWRFSU) for a struct_time."""
    return _DOW_MAP[t.tm_wday]


# ────────────────────────────────────────────────────────────────────────────
# Brightness
# ────────────────────────────────────────────────────────────────────────────

def update_brightness():
    global brightness_current, tower_color
    if not veml_present:
        return
    lux = veml.lux
    if lux > LUX_MAX:
        b = BRIGHTNESS_MAX
    elif lux < LUX_MIN:
        b = BRIGHTNESS_MIN
    else:
        b = int(BRIGHTNESS_MIN + (lux - LUX_MIN) *
                (BRIGHTNESS_MAX - BRIGHTNESS_MIN) / (LUX_MAX - LUX_MIN))
    brightness_current = b
    tower_color[RED]    = (b, 0, 0)
    tower_color[YELLOW] = (0, 0, b)
    tower_color[GREEN]  = (0, b, 0)


# ────────────────────────────────────────────────────────────────────────────
# NeoPixel helpers
# ────────────────────────────────────────────────────────────────────────────

def show_color(state):
    if state in (RED, YELLOW, GREEN):
        pixels[0] = tower_color[state]
    else:
        pixels[0] = (0, 0, 0)
    pixels.show()


def update_warning_flash():
    global warn_flash_timer, warn_flash_state
    now = time.monotonic()
    if now - warn_flash_timer >= 0.3:
        warn_flash_timer  = now
        warn_flash_state  = not warn_flash_state
        pixels[0] = tower_color[YELLOW] if warn_flash_state else (0, 0, 0)
        pixels.show()


# ────────────────────────────────────────────────────────────────────────────
# Buzzer helpers
# ────────────────────────────────────────────────────────────────────────────

def buzzer_beep(on_ms=500, off_ms=700):
    buzzer.duty_cycle = BUZZER_DUTY
    time.sleep(on_ms / 1000)
    buzzer.duty_cycle = 0
    time.sleep(off_ms / 1000)


def buzzer_pattern(count):
    for _ in range(count):
        buzzer_beep(500, 700)


# ────────────────────────────────────────────────────────────────────────────
# Button
# ────────────────────────────────────────────────────────────────────────────

_last_button = True  # pulled-up, so idle = True (HIGH)

def check_refresh_button():
    global _last_button
    reading = button.value
    if not reading:                    # settle debounce
        time.sleep(0.0005)
        reading = button.value
    if _last_button and not reading:   # falling edge
        _last_button = reading
        return True
    _last_button = reading
    return False


# ────────────────────────────────────────────────────────────────────────────
# CSV / event parsing
# ────────────────────────────────────────────────────────────────────────────

def _trim_unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s


def _parse_csv_line(line):
    """
    Parse one CSV line into an event dict.
    Returns dict on success, None on failure.
    Fields: stime, duration, dow, sdate, edate, event
    Computed: start_min, end_min, on_start, warn_start, on_end
    end_min and on_end may exceed 1439 for events that span midnight.
    """
    parts = line.split(",", 5)
    if len(parts) < 6:
        return None

    stime    = _trim_unquote(parts[0])
    duration = int(_trim_unquote(parts[1]) or "0")
    dow      = _trim_unquote(parts[2])
    sdate    = _trim_unquote(parts[3])
    edate    = _trim_unquote(parts[4])
    event    = _trim_unquote(parts[5])

    if not sdate:
        sdate = DEFAULT_SDATE
    if not edate:
        edate = sdate if sdate != DEFAULT_SDATE else DEFAULT_EDATE

    if ":" not in stime:
        return None

    h, m = stime.split(":", 1)
    start_min = int(h) * 60 + int(m)

    # Allow end_min to exceed 1439 for overnight events — evaluated mod 1440
    end_min = start_min + duration - 1

    on_start   = max(start_min - 30, 0)
    warn_start = max(start_min - 5,  0)

    # Allow on_end to exceed 1439 too — same mod-1440 treatment
    on_end = end_min + 30

    return {
        "stime":      stime,
        "duration":   duration,
        "dow":        dow,
        "sdate":      sdate,
        "edate":      edate,
        "event":      event,
        "start_min":  start_min,
        "end_min":    end_min,    # may be > 1439 for overnight events
        "on_start":   on_start,
        "warn_start": warn_start,
        "on_end":     on_end,     # may be > 1439 for overnight events
    }


def parse_events_from_feed(json_array):
    """
    Build event list from Adafruit IO JSON array.
    Lines starting with '*' are comments and are skipped.
    """
    events = []

    for obj in json_array:
        val = obj.get("value", "")
        if not val or val.startswith("*"):
            continue
        line = val.strip()
        if not line:
            continue
        ev = _parse_csv_line(line)
        if ev:
            events.append(ev)

    print(f"Parsed {len(events)} events")
    return events


# ────────────────────────────────────────────────────────────────────────────
# Adafruit IO fetch
# ────────────────────────────────────────────────────────────────────────────

def refresh_data():
    global g_events, color_current

    show_color(RED)
    color_current = RED

    print("Fetching feed …")
    try:
        resp = requests_session.get(AIO_URL, headers={"X-AIO-Key": AIO_KEY})
        if resp.status_code == 200:
            data     = resp.json()
            g_events = parse_events_from_feed(data)
        else:
            print(f"HTTP error {resp.status_code}")
        resp.close()
    except Exception as exc:
        print(f"Feed fetch failed: {exc}")

    # Confirmation beep
    buzzer_beep(500, 3000)   # 0.5 s on, 3 s off


# ────────────────────────────────────────────────────────────────────────────
# Overnight helpers                                              ← NEW
# ────────────────────────────────────────────────────────────────────────────

def _prev_date_str(t):
    """Return 'yyyy/mm/dd' for the calendar day before struct_time t."""
    # Subtract one day's worth of seconds then reformat
    yr, mo, dy = t.tm_year, t.tm_mon, t.tm_mday
    dy -= 1
    if dy == 0:
        mo -= 1
        if mo == 0:
            mo = 12
            yr -= 1
        # Days in month lookup (non-leap-year safe for this purpose)
        _days = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        if mo == 2 and (yr % 4 == 0 and (yr % 100 != 0 or yr % 400 == 0)):
            dy = 29
        else:
            dy = _days[mo]
    return f"{yr:04d}/{mo:02d}/{dy:02d}"


def _prev_dow_char(t):
    """Return MTWRFSU character for the day before struct_time t."""
    return _DOW_MAP[(t.tm_wday - 1) % 7]


# ────────────────────────────────────────────────────────────────────────────
# Event evaluation
# ────────────────────────────────────────────────────────────────────────────

def evaluate_events(t):
    """
    Given a struct_time, return the desired LED state.
    Priority: GREEN > YELLOW > RED > OFF
    Handles events that span midnight correctly.
    """
    today     = now_date_str(t)
    yesterday = _prev_date_str(t)
    dow_ch    = now_dow_char(t)
    yday_dow  = _prev_dow_char(t)
    now_min   = now_minutes(t)

    desired = OFF

    for e in g_events:
        overnight = e["end_min"] > 1439

        if not overnight:
            # ── Normal same-day event ────────────────────────────────────────
            if e["dow"] and dow_ch not in e["dow"]:
                continue
            if not (e["sdate"] <= today <= e["edate"]):
                continue

            # GREEN: event active — highest priority, exit immediately
            if e["start_min"] <= now_min <= e["end_min"]:
                return GREEN

            # YELLOW: 5-min warning window
            if e["warn_start"] <= now_min < e["start_min"]:
                if desired < YELLOW:
                    desired = YELLOW

            # RED: 30-min approach or 30-min tail
            if ((e["on_start"] <= now_min < e["warn_start"]) or
                    (e["end_min"] < now_min <= e["on_end"])):
                if desired < RED:
                    desired = RED

        else:
            # ── Overnight event — two passes ─────────────────────────────────
            end_wrapped = e["end_min"] % 1440
            on_end_wrap = e["on_end"]  % 1440

            # Pass 1: we are on the START side (today is the event's start day,
            #         current time is after start_min heading toward midnight)
            dow_ok_start  = (not e["dow"] or dow_ch in e["dow"])
            date_ok_start = (e["sdate"] <= today <= e["edate"])

            if dow_ok_start and date_ok_start:
                if now_min >= e["start_min"]:           # after start, before midnight
                    return GREEN
                if e["warn_start"] <= now_min < e["start_min"]:
                    if desired < YELLOW:
                        desired = YELLOW
                if e["on_start"] <= now_min < e["warn_start"]:
                    if desired < RED:
                        desired = RED

            # Pass 2: we are on the END side (today is the day after start,
            #         current time is after midnight through end_wrapped)
            dow_ok_end  = (not e["dow"] or yday_dow in e["dow"])
            date_ok_end = (e["sdate"] <= yesterday <= e["edate"])

            if dow_ok_end and date_ok_end:
                if now_min <= end_wrapped:              # after midnight, before end
                    return GREEN
                if end_wrapped < now_min <= on_end_wrap:  # 30-min tail after end
                    if desired < RED:
                        desired = RED

    return desired


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

# Boot
show_color(RED)
color_current = RED

pool, requests_session = connect_wifi()
init_ntp(pool)

t = ntp.datetime
print(f"Time: {t.tm_year}-{t.tm_mon:02d}-{t.tm_mday:02d} "
      f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}")

refresh_data()
show_color(OFF)
color_current = OFF

# ── Main loop ────────────────────────────────────────────────────────────────
while True:

    # Manual refresh button
    if check_refresh_button():
        print("Manual refresh requested")
        refresh_data()

    # Brightness from lux sensor
    prev_brightness = brightness_current
    update_brightness()
    if prev_brightness != brightness_current and color_current >= 0:
        show_color(color_current)

    # Evaluate events against current time
    t       = get_now()
    desired = evaluate_events(t)

    # Apply state change
    if color_current != desired:
        color_current = desired

        if desired == YELLOW:
            warn_flash_timer = time.monotonic()
            warn_flash_state = False
            update_warning_flash()
        elif desired in (GREEN, RED):
            show_color(desired)
        else:                       # OFF
            show_color(OFF)

        print(f"State change → {desired}  {ntp.datetime}")

        # Buzzer: 4 beeps for GREEN, 2 for YELLOW, 0 for others
        beep_count = 4 if desired == GREEN else (2 if desired == YELLOW else 0)
        buzzer_pattern(beep_count)

    # Maintain YELLOW blinking
    if color_current == YELLOW:
        update_warning_flash()

    time.sleep(1)