"""Aggregate independent GPS observations, not decisions about contacting a person."""
from datetime import datetime
import math

from silicon_perception.collection.gps import GpsSource


LOCATION_TRIGGERS = {"gps_moved", "gps_category_change", "gps_stable_stop", "wifi_home_arrival"}


def deferred_key(triggers):
    # Only pure location events coalesce. Keep independent concerns intact.
    names = set(triggers or [])
    if names and names <= LOCATION_TRIGGERS:
        return "location_episode"
    return "|".join(triggers) if triggers else "flash"


class LocationEpisode:
    def __init__(self):
        self.reset()

    def reset(self):
        self.last = None
        self.anchor = None
        self.moving = False
        self.stop_candidate = None
        self.place = None
        self.place_candidate = None

    @staticmethod
    def sample(snapshot):
        if snapshot is None:
            return None
        try:
            observed = datetime.fromisoformat(snapshot.field_ts["gps"]).timestamp()
            age = datetime.now().timestamp() - observed
            lat, lng = float(snapshot.location_lat), float(snapshot.location_lng)
            if not (0 <= age <= 1800 and math.isfinite(lat) and math.isfinite(lng)
                    and -90 <= lat <= 90 and -180 <= lng <= 180):
                return None
            category = getattr(snapshot, "location_category", None)
            place = getattr(snapshot, "location_place_id", None)
            if not place and category in {"home", "work"}:
                place = "category:" + category
            return (observed, lat, lng, place)
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def distance(a, b):
        return GpsSource._haversine(a[1], a[2], b[1], b[2])

    def observe(self, snapshot, previous=None):
        if (getattr(snapshot, "wifi_is_home", None) is True
                and not snapshot.is_stale("wifi", 300)):
            self.reset()
            return []
        current = self.sample(snapshot)
        if current is None:
            self.reset()
            return []
        if self.last is None:
            # The detector skips the very first tick; seed its previous sample.
            prior = self.sample(previous)
            if (prior and 0 < current[0] - prior[0] <= 1800
                    and not (getattr(previous, "wifi_is_home", None) is True
                             and not previous.is_stale("wifi", 300))):
                self.last = self.anchor = prior
                self.place = prior[3]
            else:
                self.last = self.anchor = current
                self.place = current[3]
                return []
        if current[0] <= self.last[0]:
            return []  # Cached/reordered receipts are not another sample.
        if current[0] - self.last[0] > 1800:
            self.reset()
            self.last = self.anchor = current
            self.place = current[3]
            return []
        place_changed = False
        if current[3] and current[3] != self.place:
            if self.place_candidate and self.place_candidate[3] == current[3]:
                place_changed = True
                self.place = current[3]
                self.place_candidate = None
            else:
                self.place_candidate = current
        else:
            self.place_candidate = None
        # Missing place identity is not evidence of departure: retain the last
        # confirmed identity. Unknown coordinates/gaps reset the whole episode.
        event = None
        if not self.moving and self.distance(self.anchor, current) > 500:
            self.moving = True
            self.stop_candidate = current
            event = "gps_moved"
        elif self.moving:
            if self.stop_candidate is None or self.distance(self.stop_candidate, current) > 200:
                self.stop_candidate = current
            elif current[0] - self.stop_candidate[0] >= 600:
                self.moving = False
                self.anchor = current
                self.stop_candidate = None
                event = "gps_stable_stop"
        self.last = current
        if place_changed:
            event = "gps_category_change"
        return [event] if event else []
