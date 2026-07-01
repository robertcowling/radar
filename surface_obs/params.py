"""
params.py — shared parameter registry for the surface_obs pipeline.

Maps a short output key (used in the GeoJSON snapshot and the single_param
front-end layer registry) to the source CSV column, display label/unit, and
rendering hints. Shared between parse_obs.py (extraction) and build_snapshot.py
/ the front-end (labels, colour ramps) so the two stay in sync.
"""

# short_key -> {
#   "col": source column name (without _qc suffix),
#   "label": human label,
#   "unit": display unit,
#   "decimals": rounding for the snapshot value,
#   "role": "station_plot" | "single_param" | "both" | "tooltip_only",
# }
PARAMS = {
    "temp_c": {
        "col": "air_temperature_near_surface_1_minute_mean",
        "label": "Air temperature", "unit": "°C", "decimals": 1, "role": "both",
    },
    # NOTE: also found entirely empty in the same live sample as MSLP above
    # (0/27469 rows) — most stations report temp_c + rh_pct but not
    # dew_point_temperature_1_minute_mean directly. Deliberately NOT derived
    # from temp/RH here (e.g. Magnus-Tetens) since the spec explicitly wants
    # a "missing dew point" station-plot case handled, not synthesized away.
    "dewpoint_c": {
        "col": "dew_point_temperature_1_minute_mean",
        "label": "Dew point", "unit": "°C", "decimals": 1, "role": "both",
    },
    "mslp_hpa": {
        "col": "air_pressure_near_surface_mean_sea_level_1_minute_mean",
        "label": "MSLP", "unit": "hPa", "decimals": 1, "role": "both",
    },
    # NOTE: air_pressure_near_surface_mean_sea_level_1_minute_mean was found
    # entirely empty (0/27469 rows) across a live 2h/247-station sample —
    # this dataset does not currently populate MSLP-reduced pressure in
    # practice, even though the column exists. mslp_hpa above is kept mapped
    # to the spec's column (correctly omitted per-station when absent);
    # this station-level (non-sea-level-corrected) pressure IS populated,
    # so it's exposed separately rather than silently substituted as MSLP.
    "pressure_station_hpa": {
        "col": "air_pressure_near_surface_1_minute_mean",
        "label": "Station-level pressure (not sea-level corrected)", "unit": "hPa", "decimals": 1, "role": "tooltip_only",
    },
    "wind_dir_deg": {
        "col": "wind_direction_near_surface_1_minute_mean",
        "label": "Wind direction", "unit": "°", "decimals": 0, "role": "both",
    },
    "wind_kt": {
        "col": "wind_speed_near_surface_1_minute_mean",
        "label": "Wind speed", "unit": "kt", "decimals": 0, "role": "both",
    },
    "cloud_oktas": {
        "col": "cloud_cover_total_1_minute_30_minute_weighted_mean",
        "label": "Total cloud cover", "unit": "oktas", "decimals": 0, "role": "station_plot",
    },
    "present_wx": {
        "col": "present_weather_1_minute_10_minute_weighted_mean",
        "label": "Present weather", "unit": "", "decimals": None, "role": "station_plot",
    },
    "vis_m": {
        "col": "meteorological_optical_range_horizontal_1_minute_mean",
        "label": "Visibility", "unit": "m", "decimals": 0, "role": "tooltip_only",
    },
    "rh_pct": {
        "col": "relative_humidity_near_surface_1_minute_mean",
        "label": "Relative humidity", "unit": "%", "decimals": 0, "role": "single_param",
    },
    "precip_1min_mm": {
        "col": "accumulated_precipitation_1_minute_total",
        "label": "1-min precipitation", "unit": "mm", "decimals": 2, "role": "tooltip_only",
    },
    "precip_rate_mmhr": {
        "col": "precipitation_intensity_1_minute_rolling_algorithm",
        "label": "Precipitation rate (60 min)", "unit": "mm/h", "decimals": 1, "role": "single_param",
    },
    "snow_cm": {
        "col": "snow_depth_1_minute_mean",
        "label": "Snow depth", "unit": "cm", "decimals": 1, "role": "single_param",
    },
    "grass_temp_c": {
        "col": "land_surface_temperature_grass_1_minute_mean",
        "label": "Grass surface temperature", "unit": "°C", "decimals": 1, "role": "single_param",
    },
    "concrete_temp_c": {
        "col": "land_surface_temperature_concrete_1_minute_mean",
        "label": "Concrete surface temperature", "unit": "°C", "decimals": 1, "role": "single_param",
    },
    "soil10_c": {
        "col": "soil_temperature_10cm_1_minute_mean",
        "label": "Soil temperature (10cm)", "unit": "°C", "decimals": 1, "role": "single_param",
    },
    "soil30_c": {
        "col": "soil_temperature_30cm_1_minute_mean",
        "label": "Soil temperature (30cm)", "unit": "°C", "decimals": 1, "role": "tooltip_only",
    },
    "soil100_c": {
        "col": "soil_temperature_100cm_1_minute_mean",
        "label": "Soil temperature (100cm)", "unit": "°C", "decimals": 1, "role": "tooltip_only",
    },
    "global_radiation_kwm2": {
        "col": "global_radiation_1_minute_mean",
        "label": "Global radiation", "unit": "kW/m²", "decimals": 2, "role": "single_param",
    },
    "cloud_base_ft": {
        "col": "cloud_base_height_layer1_1_minute_30_minute_rolling_min",
        "label": "Cloud base height", "unit": "ft", "decimals": 0, "role": "tooltip_only",
    },
}

# Extra identity/context columns pulled straight through, not QC-gated.
IDENTITY_COLS = ["timestep", "name", "latitude", "longitude"]

FRESHNESS_MINUTES_DEFAULT = 120
