"""Withings 247 Data implementation for body measurements, sleep, and activity."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.database import DbSession
from app.models import DataPointSeries, DataSource, EventRecord
from app.repositories import EventRecordRepository, UserConnectionRepository
from app.repositories.data_source_repository import DataSourceRepository
from app.schemas import EventRecordCreate, TimeSeriesSampleCreate
from app.schemas.event_record_detail import EventRecordDetailCreate
from app.schemas.series_types import SeriesType, get_series_type_id
from app.services.event_record_service import event_record_service
from app.services.providers.api_client import make_authenticated_request
from app.services.providers.templates.base_247_data import Base247DataTemplate
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.timeseries_service import timeseries_service
from app.utils.structured_logging import log_structured

# Withings measure type codes → SeriesType mapping
# See: https://developer.withings.com/api-reference/#tag/measure/operation/measure-getmeas
WITHINGS_MEASURE_TYPES: dict[int, SeriesType] = {
    1: SeriesType.weight,  # Weight (kg)
    4: SeriesType.height,  # Height (meters → convert to cm)
    5: SeriesType.lean_body_mass,  # Fat Free Mass (kg)
    6: SeriesType.body_fat_percentage,  # Fat Ratio (%)
    8: SeriesType.body_fat_mass,  # Fat Mass Weight (kg) — stored as body_fat_mass
    9: SeriesType.blood_pressure_diastolic,  # Diastolic BP (mmHg)
    10: SeriesType.blood_pressure_systolic,  # Systolic BP (mmHg)
    11: SeriesType.heart_rate,  # Heart Pulse (bpm)
    54: SeriesType.oxygen_saturation,  # SpO2 (%)
    71: SeriesType.body_temperature,  # Body Temperature (°C)
    73: SeriesType.skin_temperature,  # Skin Temperature (°C)
    76: SeriesType.skeletal_muscle_mass,  # Muscle Mass (kg)
    77: SeriesType.hydration,  # Hydration (kg → mL not ideal, but closest)
    88: SeriesType.body_mass_index,  # Bone Mass (kg) — note: no direct bone_mass type
    # 91: Pulse Wave Velocity — no direct mapping
}


class Withings247Data(Base247DataTemplate):
    """Withings implementation for 247 data (body measurements, sleep, activity)."""

    def __init__(
        self,
        provider_name: str,
        api_base_url: str,
        oauth: BaseOAuthTemplate,
    ):
        super().__init__(provider_name, api_base_url, oauth)
        self.event_record_repo = EventRecordRepository(EventRecord)
        self.data_source_repo = DataSourceRepository(DataSource)
        self.connection_repo = UserConnectionRepository()

    def _make_api_request(
        self,
        db: DbSession,
        user_id: UUID,
        endpoint: str,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """Make authenticated POST request to Withings API.

        Withings API uses POST for all data endpoints with action parameters.
        """
        return make_authenticated_request(
            db=db,
            user_id=user_id,
            connection_repo=self.connection_repo,
            oauth=self.oauth,
            api_base_url=self.api_base_url,
            provider_name=self.provider_name,
            endpoint=endpoint,
            method="POST",
            data=data,
        )

    @staticmethod
    def _parse_withings_value(value: int, unit: int) -> Decimal:
        """Parse Withings measurement value.

        Withings returns values as: actual_value = value * 10^unit
        e.g., weight 72.5 kg = value=72500, unit=-3 → 72500 * 10^-3 = 72.5
        """
        return Decimal(str(value)) * Decimal(10) ** unit

    # -------------------------------------------------------------------------
    # Body Measurements - Withings Measure API
    # -------------------------------------------------------------------------

    def get_body_measurements(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch body measurements from Withings Measure API."""
        start_epoch = int(start_time.timestamp())
        end_epoch = int(end_time.timestamp())

        try:
            response = self._make_api_request(
                db,
                user_id,
                "/measure",
                data={
                    "action": "getmeas",
                    "startdate": start_epoch,
                    "enddate": end_epoch,
                    "category": 1,  # 1 = real measurements (not user objectives)
                },
            )

            if not isinstance(response, dict) or response.get("status") != 0:
                log_structured(
                    self.logger,
                    "warning",
                    f"Withings measure API returned status: {response.get('status') if isinstance(response, dict) else 'non-dict'}",
                    provider="withings",
                    task="get_body_measurements",
                )
                return []

            body = response.get("body", {})
            return body.get("measuregrps", [])

        except Exception as e:
            log_structured(
                self.logger,
                "error",
                f"Error fetching Withings body measurements: {e}",
                provider="withings",
                task="get_body_measurements",
            )
            raise

    def save_body_measurements(
        self,
        db: DbSession,
        user_id: UUID,
        measure_groups: list[dict[str, Any]],
    ) -> int:
        """Save Withings measure groups as time series data points.

        Each measure group contains a timestamp and a list of measures,
        where each measure has a type, value, and unit exponent.
        """
        count = 0

        for grp in measure_groups:
            grp_timestamp = grp.get("date")
            if not grp_timestamp:
                continue

            recorded_at = datetime.fromtimestamp(grp_timestamp, tz=timezone.utc)
            measures = grp.get("measures", [])

            for measure in measures:
                mtype = measure.get("type")
                value = measure.get("value")
                unit = measure.get("unit", 0)

                if mtype is None or value is None:
                    continue

                series_type = WITHINGS_MEASURE_TYPES.get(mtype)
                if series_type is None:
                    continue

                try:
                    parsed_value = self._parse_withings_value(value, unit)

                    # Convert height from meters to centimeters
                    if series_type == SeriesType.height:
                        parsed_value = parsed_value * 100

                    sample = TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        source=self.provider_name,
                        recorded_at=recorded_at,
                        value=parsed_value,
                        series_type=series_type,
                    )
                    timeseries_service.crud.create(db, sample)
                    count += 1
                except Exception as e:
                    log_structured(
                        self.logger,
                        "warning",
                        f"Failed to save Withings measure type {mtype}: {e}",
                        provider="withings",
                        task="save_body_measurements",
                    )

        return count

    # -------------------------------------------------------------------------
    # Sleep Data - Withings Sleep API
    # -------------------------------------------------------------------------

    def get_sleep_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch sleep data from Withings Sleep API."""
        start_epoch = int(start_time.timestamp())
        end_epoch = int(end_time.timestamp())

        try:
            response = self._make_api_request(
                db,
                user_id,
                "/v2/sleep",
                data={
                    "action": "getsummary",
                    "startdateymd": start_time.strftime("%Y-%m-%d"),
                    "enddateymd": end_time.strftime("%Y-%m-%d"),
                },
            )

            if not isinstance(response, dict) or response.get("status") != 0:
                log_structured(
                    self.logger,
                    "warning",
                    f"Withings sleep API returned status: {response.get('status') if isinstance(response, dict) else 'non-dict'}",
                    provider="withings",
                    task="get_sleep_data",
                )
                return []

            body = response.get("body", {})
            return body.get("series", [])

        except Exception as e:
            log_structured(
                self.logger,
                "error",
                f"Error fetching Withings sleep data: {e}",
                provider="withings",
                task="get_sleep_data",
            )
            raise

    def normalize_sleep(
        self,
        raw_sleep: dict[str, Any],
        user_id: UUID,
    ) -> dict[str, Any]:
        """Normalize Withings sleep data to our schema.

        Withings sleep summary provides:
        - startdate/enddate (epoch timestamps)
        - data: lightsleepduration, deepsleepduration, remsleepduration,
                wakeupduration, durationtosleep, hr_average, rr_average, etc.
        """
        start_epoch = raw_sleep.get("startdate")
        end_epoch = raw_sleep.get("enddate")
        sleep_data = raw_sleep.get("data", {})

        if not start_epoch or not end_epoch:
            return {}

        start_dt = datetime.fromtimestamp(start_epoch, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(end_epoch, tz=timezone.utc)
        duration_seconds = int((end_dt - start_dt).total_seconds())

        # Sleep stage durations (Withings provides in seconds)
        deep_seconds = sleep_data.get("deepsleepduration", 0) or 0
        light_seconds = sleep_data.get("lightsleepduration", 0) or 0
        rem_seconds = sleep_data.get("remsleepduration", 0) or 0
        awake_seconds = sleep_data.get("wakeupduration", 0) or 0

        # Total sleep time (everything except awake)
        total_sleep_seconds = deep_seconds + light_seconds + rem_seconds

        # Sleep efficiency: total sleep / time in bed
        efficiency = None
        if duration_seconds > 0:
            efficiency = (total_sleep_seconds / duration_seconds) * 100

        sleep_id = uuid4()

        return {
            "id": sleep_id,
            "user_id": user_id,
            "provider": self.provider_name,
            "start_time": start_dt,
            "end_time": end_dt,
            "duration_seconds": duration_seconds,
            "efficiency_percent": efficiency,
            "is_nap": False,
            "stages": {
                "deep_seconds": deep_seconds,
                "light_seconds": light_seconds,
                "rem_seconds": rem_seconds,
                "awake_seconds": awake_seconds,
            },
            "hr_average": sleep_data.get("hr_average"),
            "rr_average": sleep_data.get("rr_average"),
        }

    def save_sleep_data(
        self,
        db: DbSession,
        user_id: UUID,
        normalized_sleep: dict[str, Any],
    ) -> None:
        """Save normalized sleep data to database as EventRecord with SleepDetails."""
        if not normalized_sleep:
            return

        sleep_id = normalized_sleep["id"]
        start_dt = normalized_sleep.get("start_time")
        end_dt = normalized_sleep.get("end_time")

        if not start_dt or not end_dt:
            return

        # Create EventRecord for sleep
        record = EventRecordCreate(
            id=sleep_id,
            category="sleep",
            type="sleep_session",
            source_name="Withings",
            device_model=None,
            duration_seconds=normalized_sleep.get("duration_seconds"),
            start_datetime=start_dt,
            end_datetime=end_dt,
            external_id=None,
            source=self.provider_name,
            user_id=user_id,
        )

        stages = normalized_sleep.get("stages", {})
        total_sleep_seconds = (
            stages.get("deep_seconds", 0) + stages.get("light_seconds", 0) + stages.get("rem_seconds", 0)
        )

        detail = EventRecordDetailCreate(
            record_id=sleep_id,
            sleep_total_duration_minutes=total_sleep_seconds // 60,
            sleep_time_in_bed_minutes=normalized_sleep.get("duration_seconds", 0) // 60,
            sleep_efficiency_score=Decimal(str(normalized_sleep["efficiency_percent"]))
            if normalized_sleep.get("efficiency_percent") is not None
            else None,
            sleep_deep_minutes=stages.get("deep_seconds", 0) // 60,
            sleep_light_minutes=stages.get("light_seconds", 0) // 60,
            sleep_rem_minutes=stages.get("rem_seconds", 0) // 60,
            sleep_awake_minutes=stages.get("awake_seconds", 0) // 60,
            is_nap=False,
        )

        try:
            created_record = event_record_service.create(db, record)
            detail.record_id = created_record.id
            event_record_service.create_detail(db, detail, detail_type="sleep")
        except Exception as e:
            log_structured(
                self.logger,
                "error",
                f"Error saving sleep record {sleep_id}: {e}",
                provider="withings",
                task="save_sleep_data",
            )

    def load_and_save_sleep(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> int:
        """Load sleep data from API and save to database."""
        raw_data = self.get_sleep_data(db, user_id, start_time, end_time)
        count = 0
        for item in raw_data:
            try:
                normalized = self.normalize_sleep(item, user_id)
                if normalized:
                    self.save_sleep_data(db, user_id, normalized)
                    count += 1
            except Exception as e:
                log_structured(
                    self.logger,
                    "warning",
                    f"Failed to save sleep data: {e}",
                    provider="withings",
                    task="load_and_save_sleep",
                )
        return count

    # -------------------------------------------------------------------------
    # Activity Data - Withings Activity API
    # -------------------------------------------------------------------------

    def get_activity_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch daily activity data from Withings Activity API."""
        try:
            response = self._make_api_request(
                db,
                user_id,
                "/v2/measure",
                data={
                    "action": "getactivity",
                    "startdateymd": start_time.strftime("%Y-%m-%d"),
                    "enddateymd": end_time.strftime("%Y-%m-%d"),
                },
            )

            if not isinstance(response, dict) or response.get("status") != 0:
                log_structured(
                    self.logger,
                    "warning",
                    f"Withings activity API returned status: {response.get('status') if isinstance(response, dict) else 'non-dict'}",
                    provider="withings",
                    task="get_activity_data",
                )
                return []

            body = response.get("body", {})
            return body.get("activities", [])

        except Exception as e:
            log_structured(
                self.logger,
                "error",
                f"Error fetching Withings activity data: {e}",
                provider="withings",
                task="get_activity_data",
            )
            raise

    def save_activity_data(
        self,
        db: DbSession,
        user_id: UUID,
        activities: list[dict[str, Any]],
    ) -> int:
        """Save Withings daily activity data as time series samples.

        Activity data includes: steps, distance, calories, active duration, etc.
        """
        count = 0

        for activity in activities:
            date_str = activity.get("date")
            if not date_str:
                continue

            try:
                recorded_at = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            # Map Withings activity fields to SeriesType
            metrics: list[tuple[str, SeriesType]] = [
                ("steps", SeriesType.steps),
                ("distance", SeriesType.distance_walking_running),
                ("calories", SeriesType.energy),
                ("hr_average", SeriesType.heart_rate),
            ]

            for field_name, series_type in metrics:
                value = activity.get(field_name)
                if value is not None and value > 0:
                    try:
                        sample = TimeSeriesSampleCreate(
                            id=uuid4(),
                            user_id=user_id,
                            source=self.provider_name,
                            recorded_at=recorded_at,
                            value=Decimal(str(value)),
                            series_type=series_type,
                        )
                        timeseries_service.crud.create(db, sample)
                        count += 1
                    except Exception as e:
                        log_structured(
                            self.logger,
                            "warning",
                            f"Failed to save activity {field_name}: {e}",
                            provider="withings",
                            task="save_activity_data",
                        )

        return count

    # -------------------------------------------------------------------------
    # Combined Load & Save
    # -------------------------------------------------------------------------

    def load_and_save_all(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime | str | None = None,
        end_time: datetime | str | None = None,
        is_first_sync: bool = False,
    ) -> dict[str, int]:
        """Load and save all Withings 247 data types."""
        if isinstance(start_time, str):
            start_time = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        if isinstance(end_time, str):
            end_time = datetime.fromisoformat(end_time.replace("Z", "+00:00"))

        if not start_time:
            start_time = datetime.now(timezone.utc) - timedelta(days=30)
        if not end_time:
            end_time = datetime.now(timezone.utc)

        results = {
            "body_measurement_samples_synced": 0,
            "sleep_sessions_synced": 0,
            "activity_samples_synced": 0,
        }

        # Body measurements
        try:
            measure_groups = self.get_body_measurements(db, user_id, start_time, end_time)
            results["body_measurement_samples_synced"] = self.save_body_measurements(db, user_id, measure_groups)
        except Exception as e:
            log_structured(
                self.logger,
                "error",
                f"Failed to sync body measurements: {e}",
                provider="withings",
                task="load_and_save_all",
            )

        # Sleep
        try:
            results["sleep_sessions_synced"] = self.load_and_save_sleep(db, user_id, start_time, end_time)
        except Exception as e:
            log_structured(
                self.logger,
                "error",
                f"Failed to sync sleep data: {e}",
                provider="withings",
                task="load_and_save_all",
            )

        # Activity
        try:
            activities = self.get_activity_data(db, user_id, start_time, end_time)
            results["activity_samples_synced"] = self.save_activity_data(db, user_id, activities)
        except Exception as e:
            log_structured(
                self.logger,
                "error",
                f"Failed to sync activity data: {e}",
                provider="withings",
                task="load_and_save_all",
            )

        return results

    # -------------------------------------------------------------------------
    # Abstract method implementations (required by Base247DataTemplate)
    # -------------------------------------------------------------------------

    def get_recovery_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        """Withings doesn't have a dedicated recovery endpoint."""
        return []

    def normalize_recovery(
        self,
        raw_recovery: dict[str, Any],
        user_id: UUID,
    ) -> dict[str, Any]:
        """Not applicable for Withings."""
        return {}

    def get_activity_samples(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        """Activity samples handled via get_activity_data instead."""
        return []

    def normalize_activity_samples(
        self,
        raw_samples: list[dict[str, Any]],
        user_id: UUID,
    ) -> dict[str, list[dict[str, Any]]]:
        """Not used — activity data processed in save_activity_data."""
        return {}

    def get_daily_activity_statistics(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> list[dict[str, Any]]:
        """Daily activity handled via get_activity_data instead."""
        return []

    def normalize_daily_activity(
        self,
        raw_stats: dict[str, Any],
        user_id: UUID,
    ) -> dict[str, Any]:
        """Not used — activity data processed in save_activity_data."""
        return {}
