import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from topopaper import weather  # noqa: E402

NOW = 1_790_000_000 - 1_790_000_000 % 3600 + 600     # 10 min past an hour (UTC)
HUD_OK = re.compile(r"^[A-Z '/&-]*$")


def series(start, values, step=900):
    return [start + i * step for i in range(len(values))], values


def fc(precip=None, codes=None, snow=None, hourly=None, highs=(15.0, 16.0), off=0):
    t0 = NOW - NOW % 900
    times, pr = series(t0, precip or [0.0] * 12)
    d = {
        "utc_offset_seconds": off,
        "current": {"temperature_2m": 12.3, "weather_code": 2},
        "minutely_15": {"time": times, "precipitation": pr,
                        "weather_code": codes or [0] * len(times),
                        "snowfall": snow or [0.0] * len(times)},
        "daily": {"temperature_2m_max": list(highs), "temperature_2m_min": [5.0, 6.0]},
    }
    if hourly is not None:
        d["hourly"] = hourly
    return d


def test_every_wmo_text_fits_the_hud():
    for code, text in weather.WMO.items():
        assert HUD_OK.match(text), text
        assert len(text) <= 22, text
    assert weather.describe(2) == "PARTLY CLOUDY"
    assert weather.describe(64) == "RAIN"            # unknown code -> family
    assert weather.describe(None) == ""


def test_units():
    assert weather.to_units(0, "imperial") == 32
    assert weather.to_units(-40, "imperial") == -40
    assert weather.to_units(21.5, "metric") == 22
    assert weather.to_units(-0.5, "metric") == -1
    assert weather.to_units(-0.4, "metric") == 0
    assert weather.to_units(37.0, "imperial") == 99


def test_rain_starting_soon():
    pr = [0, 0, 0, 0.4, 0.8] + [0] * 7
    news, eta = weather.derive_news(fc(precip=pr, codes=[0, 0, 0, 61, 63] + [0] * 7), NOW)
    assert news == "@RAIN"
    assert eta == NOW - NOW % 900 + 3 * 900


def test_snow_and_storms_classified():
    pr = [0, 0.3] + [0] * 10
    assert weather.derive_news(fc(precip=pr, codes=[0, 73] + [0] * 10), NOW)[0] == "@SNOW"
    assert weather.derive_news(fc(precip=pr, snow=[0, 0.2] + [0] * 10), NOW)[0] == "@SNOW"
    assert weather.derive_news(fc(codes=[0, 95] + [0] * 10), NOW)[0] == "@STORMS"


def test_no_heads_up_while_already_raining_or_too_far_off():
    assert weather.derive_news(fc(precip=[0.5] * 12), NOW) == ("", None)
    late = [0] * 7 + [1.0] * 5                  # starts 105 min out
    assert weather.derive_news(fc(precip=late), NOW) == ("", None)


def hourly_temps(values, start):
    return {"time": [start + i * 3600 for i in range(len(values))], "temperature_2m": values}


def test_frost_tonight():
    midnight = NOW - NOW % 86400
    temps = [8.0] * 18 + [3.0, 1.0, 0.0, -1.5] + [-2.0] * 10 + [4.0] * 8
    f = fc(hourly=hourly_temps(temps, midnight), highs=(9.0, 10.0))
    assert weather.derive_news(f, midnight + 10 * 3600) == ("FROST TONIGHT", None)
    # a cold day isn't news
    f = fc(hourly=hourly_temps(temps, midnight), highs=(3.0, 4.0))
    assert weather.derive_news(f, midnight + 10 * 3600) == ("", None)


def test_frost_follows_local_time():
    # UTC-6: local 18:00 is 00:00 UTC next day; cold only in the local night
    off = -6 * 3600
    local_mid_utc = (NOW + off) - (NOW + off) % 86400 - off
    temps = [10.0] * 18 + [-1.0] * 12 + [10.0] * 10
    f = fc(hourly=hourly_temps(temps, local_mid_utc), highs=(12.0, 12.0), off=off)
    assert weather.derive_news(f, local_mid_utc + 9 * 3600) == ("FROST TONIGHT", None)


def test_swing():
    assert weather.derive_news(fc(highs=(10.0, 19.0)), NOW) == ("MUCH WARMER TOMORROW", None)
    assert weather.derive_news(fc(highs=(20.0, 11.5)), NOW) == ("MUCH COLDER TOMORROW", None)
    assert weather.derive_news(fc(highs=(20.0, 13.0)), NOW) == ("", None)


def test_rain_beats_frost_and_swing():
    pr = [0, 0.5] + [0] * 10
    f = fc(precip=pr, highs=(0.0, 20.0))
    assert weather.derive_news(f, NOW)[0] == "@RAIN"


def test_freezing_level_and_render():
    t0 = NOW - NOW % 3600
    f = fc(precip=[0, 0.5] + [0] * 10)
    f["hourly"] = {"time": [t0 - 3600, t0, t0 + 3600], "freezing_level_height": [2900.4, 3010.6, 3100]}
    assert weather.freezing_level_now(f, NOW) == 3011
    txt = weather.render(f, NOW, "imperial", ("alps", 3011))
    kv = dict(ln.split("=", 1) for ln in txt.splitlines())
    assert kv["ts"] == str(NOW)
    assert kv["temp"] == "54"
    assert kv["cond"] == "PARTLY CLOUDY"
    assert kv["news"] == "@RAIN" and int(kv["neta"]) > NOW
    assert kv["apack"] == "alps" and kv["afrz"] == "3011"
    assert "news" not in weather.render(fc(), NOW, "metric")


def test_ip_services_parse():
    assert weather.parse_ip_location({"latitude": 46.53, "longitude": 7.96,
                                      "timezone": "Europe/Zurich", "city": "Town"}) \
        == (46.53, 7.96, "Europe/Zurich", "Town")
    assert weather.parse_ip_location({"success": True, "latitude": 1.5, "longitude": 2,
                                      "timezone": {"id": "Etc/GMT"}})[2] == "Etc/GMT"
    assert weather.parse_ip_location({"latitude": "10.25", "longitude": "-3.5",
                                      "timezone": "Africa/Abidjan"})[:2] == (10.25, -3.5)
    assert weather.parse_ip_location({"success": False, "message": "rate limited"}) is None
    assert weather.parse_ip_location({"error": True, "reason": "RateLimited"}) is None
    assert weather.round_coord(46.5349) == 46.5
