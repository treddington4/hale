"""
Historical weather via Open-Meteo's free archive API — no API key required.
Docs: https://open-meteo.com/en/docs/historical-weather-api
"""
import math
import requests
from datetime import datetime

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def get_historical_weather(lat: float, lon: float, date: str, hour: int):
    """
    date: "YYYY-MM-DD"
    hour: 0-23 local hour the run started
    Returns (temp_f, condition_string, heat_index_f, wet_bulb_f), any of which may
    be None on failure or if humidity wasn't available for the derived values.
    """
    if lat is None or lon is None:
        return None, None, None, None
    try:
        resp = requests.get(
            ARCHIVE_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "start_date": date,
                "end_date": date,
                "hourly": "temperature_2m,relativehumidity_2m,weathercode",
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        hourly = data.get("hourly", {})
        temps = hourly.get("temperature_2m", [])
        humidity = hourly.get("relativehumidity_2m", [])
        codes = hourly.get("weathercode", [])
        if not temps or hour >= len(temps):
            return None, None, None, None
        temp_f = temps[hour]
        condition = _weathercode_to_text(codes[hour] if hour < len(codes) else None)
        rh = humidity[hour] if humidity and hour < len(humidity) else None
        heat_index_f = _heat_index_f(temp_f, rh) if rh is not None else None
        wet_bulb_f = _wet_bulb_f(temp_f, rh) if rh is not None else None
        return (
            round(temp_f, 1),
            condition,
            round(heat_index_f, 1) if heat_index_f is not None else None,
            round(wet_bulb_f, 1) if wet_bulb_f is not None else None,
        )
    except Exception:
        return None, None, None, None


def _heat_index_f(temp_f: float, rh: float):
    """NWS Rothfusz regression. Below 80F/40%RH the heat index is essentially
    just the air temp, so the regression (which is only fit for hot/humid
    conditions) is skipped in favor of returning temp_f unchanged."""
    if temp_f < 80 or rh < 40:
        return temp_f
    t, r = temp_f, rh
    hi = (
        -42.379 + 2.04901523 * t + 10.14333127 * r - 0.22475541 * t * r
        - 0.00683783 * t * t - 0.05481717 * r * r
        + 0.00122874 * t * t * r + 0.00085282 * t * r * r
        - 0.00000199 * t * t * r * r
    )
    return hi


def _wet_bulb_f(temp_f: float, rh: float):
    """Stull (2011) approximation, computed in Celsius then converted back."""
    t_c = (temp_f - 32) * 5 / 9
    tw_c = (
        t_c * math.atan(0.151977 * math.sqrt(rh + 8.313659))
        + math.atan(t_c + rh) - math.atan(rh - 1.676331)
        + 0.00391838 * (rh ** 1.5) * math.atan(0.023101 * rh)
        - 4.686035
    )
    return tw_c * 9 / 5 + 32


def _weathercode_to_text(code):
    """WMO weather interpretation codes, simplified."""
    if code is None:
        return None
    mapping = {
        0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Fog", 48: "Fog",
        51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
        61: "Light rain", 63: "Rain", 65: "Heavy rain",
        71: "Light snow", 73: "Snow", 75: "Heavy snow",
        80: "Rain showers", 81: "Rain showers", 82: "Violent rain showers",
        95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ hail",
    }
    return mapping.get(code, "Unknown")


def get_weather_forecast(lat: float, lon: float, date: str):
    """P17: Fetch weather forecast for a race day via Open-Meteo's forecast API
    (different host from the archive API). Returns dict with forecast data or
    {"available": False, "reason": "..."} if unavailable.
    
    date: "YYYY-MM-DD" (must be within ~16 days of today)
    
    Returns: {
        "available": True,
        "date": "2026-08-15",
        "tempF": 72,
        "condition": "Partly cloudy",
        "heatIndexF": 74,
        "wetBulbF": 65,
        "humidity": 55
    } or {"available": False, "reason": "..."}
    """
    if lat is None or lon is None:
        return {"available": False, "reason": "Race location not set"}
    
    try:
        from datetime import datetime as dt
        today = dt.now().date()
        forecast_date = dt.strptime(date, "%Y-%m-%d").date()
        days_out = (forecast_date - today).days
        
        if days_out < 0:
            return {"available": False, "reason": "Date is in the past"}
        if days_out > 16:
            return {"available": False, "reason": "Forecast not available beyond 16 days"}
        
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "start_date": date,
                "end_date": date,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,relative_humidity_2m_max,relative_humidity_2m_min",
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        
        daily = data.get("daily", {})
        if not daily or not daily.get("time"):
            return {"available": False, "reason": "No forecast data"}
        
        # Extract day 0 data (the requested date)
        codes = daily.get("weather_code", [])
        temps_max = daily.get("temperature_2m_max", [])
        temps_min = daily.get("temperature_2m_min", [])
        humidity_max = daily.get("relative_humidity_2m_max", [])
        humidity_min = daily.get("relative_humidity_2m_min", [])
        
        if not temps_max:
            return {"available": False, "reason": "No temperature data"}
        
        code = codes[0] if codes else None
        temp_max = temps_max[0]
        temp_min = temps_min[0] if temps_min else temp_max
        humidity = humidity_max[0] if humidity_max else None
        
        condition = _weathercode_to_text(code)
        heat_index = _heat_index_f(temp_max, humidity) if humidity else None
        wet_bulb = _wet_bulb_f(temp_max, humidity) if humidity else None
        
        return {
            "available": True,
            "date": date,
            "tempMaxF": round(temp_max, 1),
            "tempMinF": round(temp_min, 1),
            "condition": condition,
            "heatIndexF": round(heat_index, 1) if heat_index else None,
            "wetBulbF": round(wet_bulb, 1) if wet_bulb else None,
            "humidity": humidity,
        }
    except Exception:
        return {"available": False, "reason": "Could not fetch forecast"}
