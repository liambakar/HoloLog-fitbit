"""Create a Markdown activity, sleep, and heart-rate report from Google Health data.

The script uses the OAuth token JSON created by scripts/get_google_health_token.py.
It refreshes the access token automatically when a refresh token is available.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


API_ROOT = "https://health.googleapis.com/v4"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_ENV_FILE = ".secrets/google_health.env"
DEFAULT_TOKEN_FILE = ".secrets/oauth_tokens.json"

RECOMMENDED_SCOPES = (
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly "
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly "
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
)
ACTIVITY_SCOPE = "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly"
SLEEP_SCOPE = "https://www.googleapis.com/auth/googlehealth.sleep.readonly"
HEALTH_METRICS_SCOPE = (
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
)

DATA_TYPE_SCOPES = {
    "steps": ACTIVITY_SCOPE,
    "distance": ACTIVITY_SCOPE,
    "total-calories": ACTIVITY_SCOPE,
    "active-zone-minutes": ACTIVITY_SCOPE,
    "active-minutes": ACTIVITY_SCOPE,
    "activity-level": ACTIVITY_SCOPE,
    "exercise": ACTIVITY_SCOPE,
    "heart-rate": HEALTH_METRICS_SCOPE,
    "sleep": SLEEP_SCOPE,
}


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"{status}: {message}")


@dataclass
class DataResult:
    data: Any
    error: str | None = None


def read_codelab_vars(path: Path) -> dict[str, str]:
    variables: dict[str, str] = {}
    if not path.exists():
        return variables

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("@") or "=" not in stripped:
            continue
        name, value = stripped[1:].split("=", 1)
        variables[name.strip()] = value.strip()
    return variables


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def first_value(*values: str | None) -> str | None:
    for value in values:
        if value and not value.startswith("YOUR_"):
            return value
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a Fitbit/Google Health report from OAuth tokens."
    )
    parser.add_argument(
        "--tokens",
        default=DEFAULT_TOKEN_FILE,
        help="Token JSON from get_google_health_token.py.",
    )
    parser.add_argument(
        "--http-file",
        default="Codelab.http",
        help="REST Client file containing client credentials.",
    )
    parser.add_argument(
        "--env-file",
        default=DEFAULT_ENV_FILE,
        help="Local env file containing Google Health OAuth credentials.",
    )
    parser.add_argument(
        "--client-id", help="OAuth client ID. Defaults to @client_id in Codelab.http."
    )
    parser.add_argument(
        "--client-secret",
        help="OAuth client secret. Defaults to @secret in Codelab.http.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to include when --start is omitted.",
    )
    parser.add_argument("--start", help="Start date, inclusive, in YYYY-MM-DD format.")
    parser.add_argument(
        "--end", help="End date, exclusive, in YYYY-MM-DD format. Defaults to tomorrow."
    )
    parser.add_argument(
        "--output", default="outputs/fitbit_report.md", help="Markdown report path."
    )
    parser.add_argument("--raw-output", help="Optional path for raw API response JSON.")
    return parser.parse_args()


def load_tokens(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"Token file not found: {path}\n"
            "Run: python3 scripts/get_google_health_token.py"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def save_tokens(path: Path, tokens: dict[str, Any]) -> None:
    path.write_text(json.dumps(tokens, indent=2) + "\n", encoding="utf-8")


def granted_scopes(tokens: dict[str, Any]) -> set[str]:
    scope_text = tokens.get("scope")
    if not isinstance(scope_text, str):
        return set()
    return set(scope_text.split())


def missing_scope_error(data_type: str, tokens: dict[str, Any]) -> str | None:
    scopes = granted_scopes(tokens)
    required_scope = DATA_TYPE_SCOPES.get(data_type)
    if not scopes or not required_scope or required_scope in scopes:
        return None
    return (
        f"{data_type}: token is missing {required_scope}. "
        "Run: python3 scripts/get_google_health_token.py --scope-preset report"
    )


def request_json(
    url: str,
    *,
    method: str = "GET",
    access_token: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise ApiError(error.code, detail) from error
    except urllib.error.URLError as error:
        raise ApiError(0, str(error.reason)) from error


def refresh_access_token(
    *,
    token_path: Path,
    tokens: dict[str, Any],
    client_id: str,
    client_secret: str,
) -> str:
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise ApiError(
            401,
            f"Access token expired and {DEFAULT_TOKEN_FILE} does not contain a refresh_token.",
        )

    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            refreshed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise ApiError(error.code, f"Token refresh failed: {detail}") from error

    tokens.update(refreshed)
    tokens["refresh_token"] = refresh_token
    save_tokens(token_path, tokens)
    return str(tokens["access_token"])


class GoogleHealthClient:
    def __init__(
        self,
        *,
        token_path: Path,
        tokens: dict[str, Any],
        client_id: str,
        client_secret: str,
    ) -> None:
        self.token_path = token_path
        self.tokens = tokens
        self.client_id = client_id
        self.client_secret = client_secret

    @property
    def access_token(self) -> str:
        token = self.tokens.get("access_token")
        if not token:
            raise SystemExit(f"{DEFAULT_TOKEN_FILE} does not contain an access_token.")
        return str(token)

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return request_json(
                url, method=method, access_token=self.access_token, body=body
            )
        except ApiError as error:
            if error.status != 401:
                raise
            refresh_access_token(
                token_path=self.token_path,
                tokens=self.tokens,
                client_id=self.client_id,
                client_secret=self.client_secret,
            )
            return request_json(
                url, method=method, access_token=self.access_token, body=body
            )


def iso_date(value: str) -> date:
    return date.fromisoformat(value)


def civil_date(value: date) -> dict[str, int]:
    return {"year": value.year, "month": value.month, "day": value.day}


def daily_rollup(
    client: GoogleHealthClient,
    data_type: str,
    start: date,
    end: date,
    *,
    max_chunk_days: int = 14,
) -> DataResult:
    missing_scope = missing_scope_error(data_type, client.tokens)
    if missing_scope:
        return DataResult([], missing_scope)

    rows: list[dict[str, Any]] = []
    cursor = start
    try:
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=max_chunk_days), end)
            body = {
                "range": {
                    "start": {"date": civil_date(cursor)},
                    "end": {"date": civil_date(chunk_end)},
                },
                "windowSizeDays": 1,
                "pageSize": 10000,
            }
            url = f"{API_ROOT}/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp"
            while True:
                response = client.request(url, method="POST", body=body)
                rows.extend(response.get("rollupDataPoints", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
                body["pageToken"] = page_token
            cursor = chunk_end
    except ApiError as error:
        return DataResult(rows, friendly_api_error(data_type, error))
    return DataResult(rows)


def list_data_points(
    client: GoogleHealthClient,
    data_type: str,
    filter_expression: str,
    *,
    page_size: int = 25,
) -> DataResult:
    missing_scope = missing_scope_error(data_type, client.tokens)
    if missing_scope:
        return DataResult([], missing_scope)

    rows: list[dict[str, Any]] = []
    params = {
        "filter": filter_expression,
        "pageSize": str(page_size),
    }
    try:
        while True:
            query = urllib.parse.urlencode(params)
            url = f"{API_ROOT}/users/me/dataTypes/{data_type}/dataPoints?{query}"
            response = client.request(url)
            rows.extend(response.get("dataPoints", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
            params["pageToken"] = page_token
    except ApiError as error:
        return DataResult(rows, friendly_api_error(data_type, error))
    return DataResult(rows)


def list_data_points_with_filter_fallbacks(
    client: GoogleHealthClient,
    data_type: str,
    filter_expressions: list[str],
    *,
    page_size: int = 25,
) -> DataResult:
    last_result = DataResult([])
    for filter_expression in filter_expressions:
        result = list_data_points(
            client, data_type, filter_expression, page_size=page_size
        )
        if not result.error:
            return result
        last_result = result
        if "INVALID_ARGUMENT" not in result.error and "failed (400)" not in result.error:
            return result
    return last_result


def friendly_api_error(data_type: str, error: ApiError) -> str:
    scope_hint = api_scope_hint(error.message)
    if error.status in {403, 401}:
        scope_hint = scope_hint or (
            " Run: python3 scripts/get_google_health_token.py --scope-preset report"
        )
    return (
        f"{data_type}: API request failed ({error.status}).{scope_hint} {error.message}"
    )


def api_scope_hint(message: str) -> str:
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return ""

    metadata = {}
    for detail in payload.get("error", {}).get("details", []):
        if isinstance(detail, dict) and detail.get("reason") == "MISSING_OAUTH_SCOPE":
            metadata = detail.get("metadata", {})
            break

    required = metadata.get("any_of_required", "")
    if "health_metrics_and_measurements" in required:
        return (
            f" Missing health metrics scope ({HEALTH_METRICS_SCOPE}). "
            "Run: python3 scripts/get_google_health_token.py --scope-preset report"
        )
    if "sleep" in required:
        return (
            f" Missing sleep scope ({SLEEP_SCOPE}). "
            "Run: python3 scripts/get_google_health_token.py --scope-preset report"
        )
    if "activity_and_fitness" in required:
        return (
            f" Missing activity scope ({ACTIVITY_SCOPE}). "
            "Run: python3 scripts/get_google_health_token.py --scope-preset report"
        )
    return ""


def duration_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    value = value.strip()
    if value.endswith("s"):
        try:
            return float(value[:-1])
        except ValueError:
            return 0.0
    return 0.0


def format_duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def format_number(value: float | int | None, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}{suffix}"
    return f"{int(value):,}{suffix}"


def get_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def get_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def civil_label(point: dict[str, Any]) -> str:
    civil = point.get("civilStartTime") or point.get("civilEndTime") or {}
    date_part = civil.get("date", {})
    if date_part:
        return f"{date_part.get('year', 0):04d}-{date_part.get('month', 0):02d}-{date_part.get('day', 0):02d}"
    start_time = point.get("startTime")
    return start_time[:10] if isinstance(start_time, str) else "-"


def interval_date_label(interval: dict[str, Any]) -> str:
    civil = interval.get("civilStartTime") or {}
    date_part = civil.get("date", {})
    if date_part:
        return f"{date_part.get('year', 0):04d}-{date_part.get('month', 0):02d}-{date_part.get('day', 0):02d}"
    start_time = interval.get("startTime")
    return start_time[:10] if isinstance(start_time, str) else "-"


def summarize_daily_rollups(
    raw: dict[str, DataResult], activity_levels: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}

    def row_for(point: dict[str, Any]) -> dict[str, Any]:
        label = civil_label(point)
        return by_date.setdefault(label, {"date": label})

    for point in raw["steps"].data:
        row_for(point)["steps"] = get_int(point.get("steps", {}).get("countSum"))

    for point in raw["distance"].data:
        mm = get_float(point.get("distance", {}).get("millimetersSum"))
        row_for(point)["distance_mi"] = mm / 1_609_344 if mm else 0.0

    for point in raw["total-calories"].data:
        row_for(point)["calories"] = get_float(
            point.get("totalCalories", {}).get("kcalSum")
        )

    for point in raw["active-zone-minutes"].data:
        value = point.get("activeZoneMinutes", {})
        total = sum(get_int(value.get(key)) for key in value)
        row_for(point)["active_zone_minutes"] = total

    for point in raw["active-minutes"].data:
        value = point.get("activeMinutes", {})
        minutes = 0
        for item in value.get("activeMinutesByActivityLevel", []):
            minutes += get_int(item.get("activeMinutesSum"))
        row_for(point)["active_minutes"] = minutes

    activity_by_date: dict[str, dict[str, float]] = {}
    for point in activity_levels:
        value = point.get("activityLevel", {})
        interval = value.get("interval", {})
        label = interval_date_label(interval)
        level = str(value.get("activityLevelType", "UNKNOWN")).replace("_", " ").title()
        seconds = duration_seconds_from_interval(interval)
        levels = activity_by_date.setdefault(label, {})
        levels[level] = levels.get(level, 0.0) + seconds

    for label, levels in activity_by_date.items():
        row = by_date.setdefault(label, {"date": label})
        row["activity_levels"] = ", ".join(
            f"{level}: {format_duration(seconds)}"
            for level, seconds in sorted(levels.items())
        )

    for point in raw["heart-rate"].data:
        value = point.get("heartRate", {})
        row = row_for(point)
        row["heart_avg"] = get_float(value.get("beatsPerMinuteAvg")) or None
        row["heart_min"] = get_float(value.get("beatsPerMinuteMin")) or None
        row["heart_max"] = get_float(value.get("beatsPerMinuteMax")) or None

    return [by_date[key] for key in sorted(by_date)]


def summarize_sleep(points: list[dict[str, Any]]) -> dict[str, Any]:
    sleeps = []
    total_minutes_asleep = 0
    stage_totals: dict[str, int] = {}

    for point in points:
        sleep = point.get("sleep", {})
        summary = sleep.get("summary", {})
        interval = sleep.get("interval", {})
        minutes_asleep = get_int(summary.get("minutesAsleep"))
        if not minutes_asleep:
            minutes_asleep = int(duration_seconds_from_interval(interval) / 60)
        total_minutes_asleep += minutes_asleep

        for stage in summary.get("stageSummaries", []):
            name = str(stage.get("stage", "UNKNOWN")).replace("_", " ").title()
            stage_totals[name] = stage_totals.get(name, 0) + get_int(
                stage.get("durationMinutes")
            )

        sleeps.append(
            {
                "start": interval.get("startTime") or "-",
                "end": interval.get("endTime") or "-",
                "type": sleep.get("type", "-"),
                "minutes_asleep": minutes_asleep,
                "main": sleep.get("metadata", {}).get("mainSleep"),
            }
        )

    return {
        "sessions": sleeps,
        "total_minutes_asleep": total_minutes_asleep,
        "stage_totals": stage_totals,
    }


def duration_seconds_from_interval(interval: dict[str, Any]) -> float:
    start = parse_datetime(interval.get("startTime"))
    end = parse_datetime(interval.get("endTime"))
    if not start or not end:
        return 0.0
    return max((end - start).total_seconds(), 0.0)


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def summarize_exercises(points: list[dict[str, Any]]) -> dict[str, Any]:
    exercises = []
    total_seconds = 0.0
    total_calories = 0.0
    total_distance_mi = 0.0

    for point in points:
        exercise = point.get("exercise", {})
        metrics = exercise.get("metricsSummary", {})
        active_seconds = duration_seconds(exercise.get("activeDuration"))
        if not active_seconds:
            active_seconds = duration_seconds_from_interval(
                exercise.get("interval", {})
            )
        distance_mi = get_float(metrics.get("distanceMillimeters")) / 1_609_344
        calories = get_float(metrics.get("caloriesKcal"))

        total_seconds += active_seconds
        total_calories += calories
        total_distance_mi += distance_mi

        exercises.append(
            {
                "start": exercise.get("interval", {}).get("startTime") or "-",
                "name": exercise.get("displayName")
                or exercise.get("exerciseType", "-"),
                "type": exercise.get("exerciseType", "-"),
                "duration": active_seconds,
                "distance_mi": distance_mi,
                "calories": calories,
                "steps": get_int(metrics.get("steps")),
                "avg_hr": get_int(metrics.get("averageHeartRateBeatsPerMinute"))
                or None,
            }
        )

    return {
        "sessions": exercises,
        "total_seconds": total_seconds,
        "total_calories": total_calories,
        "total_distance_mi": total_distance_mi,
    }


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_No data returned._"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def build_report(
    *,
    start: date,
    end: date,
    daily: list[dict[str, Any]],
    sleep: dict[str, Any],
    exercises: dict[str, Any],
    errors: list[str],
) -> str:
    days = max((end - start).days, 1)
    total_steps = sum(get_int(row.get("steps")) for row in daily)
    total_distance = sum(get_float(row.get("distance_mi")) for row in daily)
    total_calories = sum(get_float(row.get("calories")) for row in daily)
    heart_values = [
        get_float(row.get("heart_avg")) for row in daily if row.get("heart_avg")
    ]

    lines = [
        "# Fitbit / Google Health Report",
        "",
        f"Range: {start.isoformat()} through {(end - timedelta(days=1)).isoformat()}",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Overview",
        "",
        f"- Steps: {total_steps:,} total / {round(total_steps / days):,} per day",
        f"- Distance: {total_distance:,.1f} mi total / {total_distance / days:,.1f} mi per day",
        f"- Calories: {total_calories:,.0f} kcal total / {total_calories / days:,.0f} kcal per day",
        f"- Exercise: {len(exercises['sessions'])} sessions, {format_duration(exercises['total_seconds'])}",
        f"- Sleep: {len(sleep['sessions'])} sessions, {format_duration(sleep['total_minutes_asleep'] * 60)} asleep",
    ]
    if heart_values:
        lines.append(
            f"- Heart rate: {statistics.mean(heart_values):.0f} bpm average daily mean"
        )

    lines.extend(["", "## Daily Activity", ""])
    daily_rows = [
        [
            row.get("date", "-"),
            format_number(row.get("steps")),
            format_number(row.get("distance_mi"), " mi"),
            format_number(row.get("calories"), " kcal"),
            format_number(row.get("active_minutes")),
            format_number(row.get("active_zone_minutes")),
            heart_summary(row),
        ]
        for row in daily
    ]
    lines.extend(
        table(
            [
                "Date",
                "Steps",
                "Distance",
                "Calories",
                "Active Min",
                "Zone Min",
                "Heart Rate",
            ],
            daily_rows,
        )
    )

    lines.extend(["", "## Sleep", ""])
    sleep_rows = [
        [
            str(item["start"])[:10],
            str(item["start"]),
            str(item["end"]),
            format_duration(item["minutes_asleep"] * 60),
            str(item["type"]).replace("_", " ").title(),
            "yes" if item["main"] is True else "no" if item["main"] is False else "-",
        ]
        for item in sleep["sessions"]
    ]
    lines.extend(table(["Date", "Start", "End", "Asleep", "Type", "Main"], sleep_rows))

    if sleep["stage_totals"]:
        stage_text = ", ".join(
            f"{stage}: {format_duration(minutes * 60)}"
            for stage, minutes in sorted(sleep["stage_totals"].items())
        )
        lines.extend(["", f"Stage totals: {stage_text}"])

    lines.extend(["", "## Exercise", ""])
    exercise_rows = [
        [
            str(item["start"])[:10],
            str(item["name"]),
            format_duration(item["duration"]),
            format_number(item["distance_mi"], " mi"),
            format_number(item["calories"], " kcal"),
            format_number(item["steps"]),
            format_number(item["avg_hr"], " bpm"),
        ]
        for item in exercises["sessions"]
    ]
    lines.extend(
        table(
            ["Date", "Workout", "Duration", "Distance", "Calories", "Steps", "Avg HR"],
            exercise_rows,
        )
    )

    if errors:
        lines.extend(["", "## Data Gaps", ""])
        lines.extend(f"- {error}" for error in errors)

    lines.append("")
    return "\n".join(lines)


def heart_summary(row: dict[str, Any]) -> str:
    avg = row.get("heart_avg")
    if not avg:
        return "-"
    min_hr = row.get("heart_min")
    max_hr = row.get("heart_max")
    if min_hr and max_hr:
        return f"{avg:.0f} bpm ({min_hr:.0f}-{max_hr:.0f})"
    return f"{avg:.0f} bpm"


def main() -> int:
    args = parse_args()
    end = iso_date(args.end) if args.end else date.today() + timedelta(days=1)
    start = iso_date(args.start) if args.start else end - timedelta(days=args.days)
    if start >= end:
        raise SystemExit("--start must be before --end")

    token_path = Path(args.tokens)
    tokens = load_tokens(token_path)
    codelab_vars = read_codelab_vars(Path(args.http_file))
    env_file_values = read_env_file(Path(args.env_file))
    client_id = first_value(
        args.client_id,
        os.environ.get("GOOGLE_HEALTH_CLIENT_ID"),
        env_file_values.get("GOOGLE_HEALTH_CLIENT_ID"),
        codelab_vars.get("client_id"),
    )
    client_secret = first_value(
        args.client_secret,
        os.environ.get("GOOGLE_HEALTH_CLIENT_SECRET"),
        env_file_values.get("GOOGLE_HEALTH_CLIENT_SECRET"),
        codelab_vars.get("secret"),
    )
    if not client_id or not client_secret:
        raise SystemExit(
            "Missing client credentials. Pass --client-id and --client-secret."
        )

    client = GoogleHealthClient(
        token_path=token_path,
        tokens=tokens,
        client_id=client_id,
        client_secret=client_secret,
    )

    rollup_types = [
        "steps",
        "distance",
        "total-calories",
        "active-zone-minutes",
        "active-minutes",
        "heart-rate",
    ]
    rollups = {
        data_type: daily_rollup(client, data_type, start, end)
        for data_type in rollup_types
    }

    exercise_filter = (
        f'exercise.interval.civil_start_time >= "{start.isoformat()}" '
        f'AND exercise.interval.civil_start_time < "{end.isoformat()}"'
    )
    sleep_filter = (
        f'sleep.interval.civil_end_time >= "{start.isoformat()}" '
        f'AND sleep.interval.civil_end_time < "{end.isoformat()}"'
    )
    activity_level_filters = [
        (
            f'activity_level.interval.civil_start_time >= "{start.isoformat()}" '
            f'AND activity_level.interval.civil_start_time < "{end.isoformat()}"'
        ),
        (
            f'activityLevel.interval.civil_start_time >= "{start.isoformat()}" '
            f'AND activityLevel.interval.civil_start_time < "{end.isoformat()}"'
        ),
    ]
    exercises_result = list_data_points(client, "exercise", exercise_filter)
    sleep_result = list_data_points(client, "sleep", sleep_filter)
    activity_level_result = list_data_points_with_filter_fallbacks(
        client, "activity-level", activity_level_filters, page_size=10000
    )

    daily = summarize_daily_rollups(rollups, activity_level_result.data)
    sleep = summarize_sleep(sleep_result.data)
    exercises = summarize_exercises(exercises_result.data)

    errors = [result.error for result in rollups.values() if result.error]
    errors.extend(
        result.error
        for result in [exercises_result, sleep_result, activity_level_result]
        if result.error
    )

    report = build_report(
        start=start,
        end=end,
        daily=daily,
        sleep=sleep,
        exercises=exercises,
        errors=[error for error in errors if error],
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(f"Wrote report to {output}")

    if args.raw_output:
        raw = {
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "rollups": {key: value.data for key, value in rollups.items()},
            "activityLevel": activity_level_result.data,
            "sleep": sleep_result.data,
            "exercises": exercises_result.data,
            "errors": [error for error in errors if error],
        }
        raw_output = Path(args.raw_output)
        raw_output.parent.mkdir(parents=True, exist_ok=True)
        raw_output.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote raw API data to {args.raw_output}")

    if errors:
        print("Completed with data gaps. See the Data Gaps section in the report.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
