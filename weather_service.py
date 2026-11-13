import json
import errno
import http.client
import math
import os
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path


def classify_weather_failure(error):
    errors = []
    pending = [error]
    seen = set()
    while pending:
        item = pending.pop()
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        seen.add(id(item))
        errors.append(item)
        pending.extend((getattr(item, 'reason', None), item.__cause__, item.__context__))
    offline_codes = {errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH, 10050, 10051, 10065}
    closed_codes = {errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE, 10053, 10054}
    for item in errors:
        if isinstance(item, TimeoutError) or getattr(item, 'errno', None) in (errno.ETIMEDOUT, 10060):
            return 'timeout'
        if isinstance(item, urllib.error.HTTPError) and item.code in (408, 504):
            return 'timeout'
    for item in errors:
        if isinstance(item, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                             http.client.RemoteDisconnected, http.client.IncompleteRead,
                             ssl.SSLEOFError, EOFError)):
            return 'closed'
        if getattr(item, 'errno', None) in closed_codes:
            return 'closed'
    if any(isinstance(item, socket.gaierror) or getattr(item, 'errno', None) in offline_codes
           for item in errors):
        return 'offline'
    return 'unexpected'


def weather_failure_message(error, app_folder=None):
    category = classify_weather_failure(error)
    details = f'{type(error).__name__}: {error}'
    folder = Path(app_folder) if app_folder is not None else Path(__file__).resolve().parent
    try:
        logs = folder / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S-%f')
        path = logs / f'weatherfailure-{timestamp}.log'
        path.write_text(
            f'Timestamp: {datetime.now().astimezone().isoformat()}\nCategory: {category}\n'
            + ''.join(traceback.format_exception(type(error), error, error.__traceback__)),
            encoding='utf-8',
        )
        print(f'MarioAssistant weather: failure logged to {path}', file=sys.stderr, flush=True)
    except OSError as log_error:
        print(f'MarioAssistant weather: could not write failure log: {log_error}', file=sys.stderr, flush=True)
    messages = {
        'offline': "You don't have an active internet connection. Location and weather querying requires Wi-Fi to function.",
        'timeout': "Open-Meteo timed out. I couldn't retrieve weather info.",
        'closed': "The connection closed unexpectedly. I couldn't retrieve weather info.",
    }
    return messages.get(category, 'An unexpected error has occured. Error log: ' + ' '.join(details.split()))


def weather_description(code):
    code = int(code)
    if code == 0:
        return 'clear skies'
    if code == 1:
        return 'mostly clear skies'
    if code == 2:
        return 'partly cloudy skies'
    if code == 3:
        return 'cloudy skies'
    if code in (45, 48):
        return 'fog'
    if code in (51, 53, 55):
        return 'drizzle'
    if code in (56, 57):
        return 'freezing drizzle'
    if code in (61, 63, 65):
        return 'rain'
    if code in (66, 67):
        return 'freezing rain'
    if code in (71, 73, 75, 77):
        return 'snow'
    if code in (80, 81, 82):
        return 'rain showers'
    if code in (85, 86):
        return 'snow showers'
    if code in (95, 96, 99):
        return 'thunderstorms with hail' if code in (96, 99) else 'thunderstorms'
    raise ValueError(f'Unknown weather condition code: {code}')


def temperature_text(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('The weather service returned an invalid temperature')
    return f'{value:.1f}'.removesuffix('.0')


def format_weather_report(data):
    current = data['current']
    daily = data['daily']
    hourly = data['hourly']
    now = int(current['time'])
    today = next(index for index, start in enumerate(daily['time'])
                 if start <= now and (index + 1 == len(daily['time']) or now < daily['time'][index + 1]))
    day_end = daily['time'][today + 1] if today + 1 < len(daily['time']) else daily['time'][today] + 86400
    future = [(timestamp, code) for timestamp, code in zip(hourly['time'], hourly['weather_code'])
              if timestamp > now and code is not None]
    later_today = [(timestamp, code) for timestamp, code in future if timestamp < day_end]
    later = [code for timestamp, code in later_today if timestamp >= now + 3 * 3600]
    if not later:
        later = [code for _, code in later_today]
    ending = 'later on in the day'
    if not later:
        later = [code for _, code in future[:3]]
        ending = 'later tonight'
    if not later:
        raise ValueError('No later forecast is available')
    today_description = weather_description(daily['weather_code'][today])
    later_description = weather_description(Counter(later).most_common(1)[0][0])
    if later_description == today_description:
        later_description = 'more of the same'
    return (
        f"Right now is {temperature_text(current['temperature_2m'])} degrees Celsius "
        f"with a low of {temperature_text(daily['temperature_2m_min'][today])} degrees Celsius "
        f"and a high of {temperature_text(daily['temperature_2m_max'][today])} degrees Celsius. "
        "\\pause=500\\"
        f'Today, you can expect {today_description}, and you can expect '
        f'{later_description} {ending}.'
    )


def wind_level(speed):
    speed = float(speed)
    if not math.isfinite(speed) or speed < 0:
        raise ValueError('The weather service returned an invalid wind speed')
    if speed < 6:
        return 'calm'
    if speed < 12:
        return 'light'
    if speed < 20:
        return 'moderate'
    if speed < 29:
        return 'fresh'
    if speed < 39:
        return 'strong'
    if speed < 50:
        return 'near-gale'
    if speed < 62:
        return 'gale-force'
    if speed < 75:
        return 'strong gale-force'
    if speed < 89:
        return 'storm-force'
    return 'violent storm-force'


def windy_description(speed):
    speed = float(speed)
    if speed < 6:
        return 'calm'
    if speed < 12:
        return 'a little breezy'
    if speed < 20:
        return 'breezy'
    if speed < 29:
        return 'windy'
    if speed < 39:
        return 'very windy'
    if speed < 50:
        return 'quite windy'
    return 'extremely windy'


def forecast_wind_description(speeds):
    levels = []
    for speed in speeds:
        level = wind_level(speed)
        if level not in levels:
            levels.append(level)
    if not levels:
        raise ValueError('No later wind forecast is available')
    if len(levels) == 1:
        return f'{levels[0]} winds'
    if len(levels) == 2:
        return f'{levels[0]} to {levels[1]} winds'
    return f'{", ".join(levels[:-1])} and {levels[-1]} winds'


def format_wind_report(data):
    current = data['current']
    hourly = data['hourly']
    now = int(current['time'])
    daily = data.get('daily', {})
    sunsets = daily.get('sunset', [])
    sunset = next((int(value) for value in sunsets if int(value) > now), now + 18 * 3600)
    future = [(int(timestamp), float(speed)) for timestamp, speed in
              zip(hourly['time'], hourly['wind_speed_10m'])
              if int(timestamp) > now and speed is not None]
    next_hours = [speed for timestamp, speed in future if timestamp <= now + 3 * 3600]
    if not next_hours:
        next_hours = [speed for _, speed in future[:3]]
    later = [speed for timestamp, speed in future if now + 5 * 3600 <= timestamp < sunset]
    if not later:
        later = [speed for timestamp, speed in future if timestamp < sunset]
    if not later:
        later = [speed for _, speed in future[3:6]] or next_hours
    current_speed = float(current['wind_speed_10m'])
    if not next_hours or not later:
        raise ValueError('The weather service returned an incomplete wind forecast')
    next_description = forecast_wind_description(next_hours)
    later_description = forecast_wind_description(later)
    if later_description == next_description:
        later_description = 'more of the same'
    speed_text = str(round(current_speed))
    return (
        f"Currently, it's {windy_description(current_speed)}. "
        "\\pause=500\\"
        f"There's {wind_level(current_speed)} winds around {speed_text} "
        "\\showas=km/h\\kilometers \\hidden\\per \\hidden\\hour. "
        "\\pause=500\\"
        f'Expect {next_description} throughout the next few hours '
        f'and {later_description} later on in the day.'
    )


class WeatherService:
    def __init__(self, city='', country_code=''):
        self.city = str(city or '').strip()
        self.country_code = str(country_code or '').strip().upper()
        self.lock = threading.Lock()
        self.location = None
        self.location_at = 0
        self.report = None
        self.report_at = 0
        self.wind_report = None
        self.wind_report_at = 0

    def _get_json(self, url):
        request = urllib.request.Request(url, headers={'User-Agent': 'MarioAssistant/1.0', 'Accept': 'application/json'})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def _coordinates(self):
        latitude = os.environ.get('MARIOASSISTANT_LATITUDE')
        longitude = os.environ.get('MARIOASSISTANT_LONGITUDE')
        if self.city:
            if self.location is not None and time.monotonic() - self.location_at < 3600:
                return self.location
            parameters = {'name': self.city, 'count': 1, 'language': 'en', 'format': 'json'}
            if self.country_code:
                parameters['countryCode'] = self.country_code
            result = self._get_json('https://geocoding-api.open-meteo.com/v1/search?' + urllib.parse.urlencode(parameters))
            matches = result.get('results', [])
            if not matches:
                raise ValueError(f'No weather location found for {self.city}')
            location = matches[0]
            coordinates = float(location['latitude']), float(location['longitude'])
            label = ', '.join(str(location.get(field, '')) for field in ('name', 'admin1', 'country') if location.get(field))
            print(f'MarioAssistant weather: saved city: {label}', flush=True)
        elif latitude is not None or longitude is not None:
            if latitude is None or longitude is None:
                raise ValueError('Set both MARIOASSISTANT_LATITUDE and MARIOASSISTANT_LONGITUDE')
            coordinates = float(latitude), float(longitude)
        elif self.location is not None and time.monotonic() - self.location_at < 3600:
            return self.location
        else:
            location = self._get_json('https://ipapi.co/json/')
            if location.get('error'):
                raise RuntimeError(location.get('reason', 'Location lookup failed'))
            coordinates = float(location['latitude']), float(location['longitude'])
            print(f"MarioAssistant weather: approximate location: {location.get('city', '')}, {location.get('country_name', '')}", flush=True)
        latitude, longitude = coordinates
        if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError('Invalid weather coordinates')
        self.location = coordinates
        self.location_at = time.monotonic()
        return coordinates

    def get_report(self):
        with self.lock:
            if self.report is not None and time.monotonic() - self.report_at < 300:
                return self.report
            latitude, longitude = self._coordinates()
            query = urllib.parse.urlencode({
                'latitude': latitude, 'longitude': longitude,
                'current': 'temperature_2m,weather_code',
                'daily': 'temperature_2m_min,temperature_2m_max,weather_code',
                'hourly': 'weather_code', 'timezone': 'auto',
                'temperature_unit': 'celsius', 'timeformat': 'unixtime', 'forecast_days': 2,
            })
            data = self._get_json('https://api.open-meteo.com/v1/forecast?' + query)
            if data.get('error'):
                raise RuntimeError(data.get('reason', 'Weather lookup failed'))
            report = format_weather_report(data)
            self.report = report
            self.report_at = time.monotonic()
            return report

    def get_wind_report(self):
        with self.lock:
            if self.wind_report is not None and time.monotonic() - self.wind_report_at < 300:
                return self.wind_report
            latitude, longitude = self._coordinates()
            query = urllib.parse.urlencode({
                'latitude': latitude, 'longitude': longitude,
                'current': 'wind_speed_10m',
                'hourly': 'wind_speed_10m',
                'daily': 'sunset',
                'wind_speed_unit': 'kmh', 'timezone': 'auto',
                'timeformat': 'unixtime', 'forecast_days': 2,
            })
            data = self._get_json('https://api.open-meteo.com/v1/forecast?' + query)
            if data.get('error'):
                raise RuntimeError(data.get('reason', 'Weather lookup failed'))
            report = format_wind_report(data)
            self.wind_report = report
            self.wind_report_at = time.monotonic()
            return report
