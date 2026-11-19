"""Data-point (DP) definitions for the Haier AC (Tuya product 9xvzf8c0bg33eenj).

Labels and value ranges come from the device's Tuya cloud model. Two sensors report
raw Haier bytes instead of the °C the model claims, so they carry a conversion.
"""

# kind: bool | enum | int | sensor ; group decides where the UI shows it
DPS = {
    "1":   dict(code="switch", label="Power", kind="bool", group="main"),
    "4":   dict(code="mode", label="Mode", kind="enum", group="main",
                values=["auto", "cold", "wet", "hot", "wind"],
                names={"auto": "Auto", "cold": "Cool", "wet": "Dry", "hot": "Heat", "wind": "Fan only"}),
    "2":   dict(code="temp_set", label="Target temperature", kind="int", group="main", min=16, max=30, unit="°C"),
    "5":   dict(code="fan_speed_enum", label="Fan speed", kind="enum", group="main",
                values=["auto", "silent", "breeze", "low", "mid", "high", "strong", "quick"]),
    "120": dict(code="half_degree_setting", label="0.5° target steps", kind="bool", group="main"),
    "17":  dict(code="humidity_set", label="Target humidity", kind="int", group="main", min=30, max=90, unit="%"),
    "19":  dict(code="temp_unit_convert", label="Display unit", kind="enum", group="main", values=["c", "f"]),

    "3":   dict(code="temp_current", label="Room temperature", kind="sensor", group="sensor", unit="°C", convert="half"),
    "128": dict(code="temp_outdoor", label="Outdoor temperature", kind="sensor", group="sensor", unit="°C", convert="minus64"),
    "18":  dict(code="humidity_current", label="Room humidity", kind="sensor", group="sensor", unit="%"),
    "112": dict(code="energy_report", label="Energy counter", kind="sensor", group="sensor", unit="kWh", convert="wh_to_kwh"),
    "116": dict(code="fault_code", label="Fault code", kind="sensor", group="sensor"),
    "134": dict(code="fullwater_status", label="Water tank full", kind="sensor", group="sensor"),
    "133": dict(code="filter_status", label="Filter warning", kind="bool", group="sensor"),
    "102": dict(code="pm25", label="PM2.5", kind="sensor", group="sensor"),
    "103": dict(code="co2", label="CO2", kind="sensor", group="sensor"),
    "104": dict(code="voc", label="VOC", kind="sensor", group="sensor"),
    "105": dict(code="air_quality", label="Air quality", kind="sensor", group="sensor"),

    "107": dict(code="turbo", label="Turbo", kind="bool", group="comfort"),
    "8":   dict(code="eco", label="ECO", kind="bool", group="comfort"),
    "108": dict(code="quiet", label="Quiet", kind="bool", group="comfort"),
    "109": dict(code="sleep", label="Sleep", kind="bool", group="comfort"),
    "12":  dict(code="heat", label="Electric aux heater", kind="bool", group="comfort"),
    "106": dict(code="healthy", label="Health (ionizer)", kind="bool", group="comfort"),
    "111": dict(code="fresh_air", label="Fresh air", kind="bool", group="comfort"),
    "110": dict(code="heating_10", label="10 °C frost protection", kind="bool", group="comfort"),
    "113": dict(code="ecopilot", label="Motion sensing", kind="enum", group="comfort",
                values=["cancel", "avoid", "follow", "active"]),
    "13":  dict(code="light", label="Display light", kind="bool", group="comfort"),
    "101": dict(code="light_status", label="Ambient light", kind="bool", group="comfort"),
    "16":  dict(code="beep", label="Beep", kind="bool", group="comfort"),
    "114": dict(code="self_clean", label="Self clean", kind="bool", group="comfort"),
    "119": dict(code="self_clean_56c", label="56 °C sterilizing clean", kind="bool", group="comfort"),
    "130": dict(code="dr_energy_demond", label="Demand response level", kind="enum", group="comfort",
                values=["0", "1", "2", "3"]),

    "117": dict(code="swing_updown", label="Vertical louver", kind="enum", group="airflow",
                values=["ud_00", "ud_01", "ud_02", "ud_03", "ud_04", "ud_06", "ud_08", "ud_0a", "ud_0c", "ud_0e"]),
    "118": dict(code="swing_leftright", label="Horizontal louver", kind="enum", group="airflow",
                values=["lr_00", "lr_01", "lr_02", "lr_03", "lr_04", "lr_05", "lr_06", "lr_07"]),

    "131": dict(code="mode_dehumidity", label="Dehumidifier mode", kind="enum", group="other",
                values=["smart", "quiet", "turbo", "dry", "wind"]),
    "132": dict(code="nonstop", label="Continuous dehumidifying", kind="bool", group="other"),
}

# Presets: applied in order. Turbo goes last because it takes over the fan.
PRESETS = {
    "max_heat": dict(label="Max heat", steps=[
        ("1", True), ("4", "hot"), ("2", 30),
        ("8", False), ("108", False), ("109", False), ("110", False),
        ("12", True),             # aux heater: this unit has none and refuses it, tried anyway
        ("107", True),
    ]),
    "max_cold": dict(label="Max cold", steps=[
        ("1", True), ("4", "cold"), ("2", 16),
        ("8", False), ("108", False), ("109", False), ("12", False),
        ("107", True),
    ]),
}


def convert(dp, raw, state):
    """Return (display_value, note) for a raw DP value."""
    meta = DPS.get(dp, {})
    c = meta.get("convert")
    if not isinstance(raw, int) or not c:
        return raw, None
    if c == "half":
        note = "not reliable while the AC is off" if state.get("1") is False else None
        return raw / 2, note
    if c == "minus64":
        return raw - 64, None
    if c == "wh_to_kwh":
        return round(raw / 1000, 1), None
    return raw, None


def validate(dp, value):
    """Check a value against the DP definition; return the cleaned value or raise ValueError."""
    meta = DPS.get(dp)
    if not meta or meta["kind"] == "sensor":
        raise ValueError(f"DP {dp} is not writable")
    kind = meta["kind"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("expected true/false")
        return value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise ValueError("expected a whole number")
        value = int(value)
        if not meta["min"] <= value <= meta["max"]:
            raise ValueError(f"must be {meta['min']}–{meta['max']}")
        return value
    if kind == "enum":
        if value not in meta["values"]:
            raise ValueError(f"must be one of {meta['values']}")
        return value
    raise ValueError("unsupported")
