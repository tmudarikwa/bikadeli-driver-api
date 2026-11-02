import math
from decimal import Decimal

VEHICLE_MAX_SPEED_KMH = {
    "BICYCLE": 30,
    "MOTORCYCLE": 120,
    "E-SCOOTER": 50,
    "SCOOTER": 50,
}
HARARE_BOUNDS = {
    "lat_min": Decimal("-18.05"),
    "lat_max": Decimal("-17.65"),
    "lng_min": Decimal("30.80"),
    "lng_max": Decimal("31.25"),
}
MIN_LOCATION_INTERVAL_SECONDS = 5
STATIC_LOCATION_THRESHOLD = 5
STATIC_LOCATION_WINDOW_SECONDS = 300


def _distance_km(lat1, lng1, lat2, lng2):
    earth_radius_km = 6371.0088
    lat1_rad, lat2_rad = math.radians(lat1), math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lng = math.radians(lng2 - lng1)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad)
        * math.cos(lat2_rad)
        * math.sin(delta_lng / 2) ** 2
    )
    return 2 * earth_radius_km * math.asin(min(1, math.sqrt(haversine)))


def detect_location_anomalies(
    *,
    latitude,
    longitude,
    vehicle_type,
    recorded_at,
    previous,
    prior_static_count,
):
    anomalies = []
    elapsed = None
    distance = None
    speed = None
    max_speed = VEHICLE_MAX_SPEED_KMH.get(vehicle_type, 120)

    if previous is not None:
        elapsed = (recorded_at - previous.recorded_at).total_seconds()
        if elapsed < MIN_LOCATION_INTERVAL_SECONDS:
            anomalies.append(
                {
                    "type": "MIN_INTERVAL",
                    "severity": "LOW",
                    "details": {
                        "elapsed_seconds": round(elapsed, 2),
                        "required": MIN_LOCATION_INTERVAL_SECONDS,
                    },
                }
            )

    inside_bounds = (
        HARARE_BOUNDS["lat_min"] <= Decimal(str(latitude)) <= HARARE_BOUNDS["lat_max"]
        and HARARE_BOUNDS["lng_min"]
        <= Decimal(str(longitude))
        <= HARARE_BOUNDS["lng_max"]
    )
    if not inside_bounds:
        anomalies.append(
            {
                "type": "GEOBOUNDARY_EXIT",
                "severity": "HIGH",
                "details": {
                    "lat": latitude,
                    "lng": longitude,
                    "bounds": {key: float(value) for key, value in HARARE_BOUNDS.items()},
                },
            }
        )

    if previous is not None and elapsed is not None and elapsed > 0:
        distance = _distance_km(
            previous.latitude,
            previous.longitude,
            latitude,
            longitude,
        )
        speed = distance / elapsed * 3600
        result = {
            "speed_kmh": round(speed, 2),
            "max_speed": max_speed,
            "is_violation": speed > max_speed,
            "distance_km": round(distance, 4),
            "time_delta_seconds": round(elapsed, 2),
        }
        if speed > max_speed:
            severity = "CRITICAL" if speed > max_speed * 1.5 else "HIGH"
            anomalies.append(
                {
                    "type": "SPEED_EXCEEDED",
                    "severity": severity,
                    "details": result,
                    "reported_speed_kmh": result["speed_kmh"],
                    "max_allowed_speed_kmh": max_speed,
                    "distance_km": result["distance_km"],
                    "time_delta_seconds": result["time_delta_seconds"],
                    "previous_latitude": previous.latitude,
                    "previous_longitude": previous.longitude,
                }
            )
        elif distance > 50:
            anomalies.append(
                {
                    "type": "IMPOSSIBLE_TRAVEL",
                    "severity": "CRITICAL",
                    "details": result,
                    "reported_speed_kmh": result["speed_kmh"],
                    "max_allowed_speed_kmh": max_speed,
                    "distance_km": result["distance_km"],
                    "time_delta_seconds": result["time_delta_seconds"],
                    "previous_latitude": previous.latitude,
                    "previous_longitude": previous.longitude,
                }
            )

    if prior_static_count + 1 >= STATIC_LOCATION_THRESHOLD:
        anomalies.append(
            {
                "type": "STATIC_LOCATION",
                "severity": "MEDIUM",
                "details": {"same_location_count": prior_static_count + 1},
            }
        )

    return anomalies


def is_critical_location_anomaly(anomalies):
    return any(anomaly["severity"] == "CRITICAL" for anomaly in anomalies)
