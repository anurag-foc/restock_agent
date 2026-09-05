"""Dimension generation — plants, lines, warehouses, models, parts, suppliers.

Replaces the ~1101-row padded dimensions in the replica with a realistic core
(docs/schema_changes_gold_dev_analytics.md §2.2, docs/dataset_generator_spec.md §2).

Everything here is derived from list position — there is no randomness at all, so two runs
produce byte-identical rows. Surrogate keys are sequential from 1 rather than the hash-like
BIGINTs the padded rows carried, because a debuggable key is worth more than a realistic-looking
one in a dataset whose whole purpose is being checked by hand.

Names are deliberately generic ('Hatch 100', 'Supplier 007') — this is synthetic data and should
never be mistakable for a real manufacturer's product or vendor list.
"""

from __future__ import annotations

from functools import cache

DW_SOURCE = "INTELLIGENCE_GEN"

# ---------------------------------------------------------------------------
# Plants and production lines
# ---------------------------------------------------------------------------

_PLANTS = [
    ("PLT001", "GGN", "Gurgaon Assembly", "Gurgaon, Haryana", 1981),
    ("PLT002", "MNS", "Manesar Assembly", "Manesar, Haryana", 2006),
]


@cache
def plants() -> list[dict]:
    return [
        {
            "PLANT_KEY": i + 1,
            "PLANT_ID": plant_id,
            "PLANT_CODE": code,
            "PLANT_NAME": name,
            "LOCATION": location,
            "PLANT_TYPE": "VEHICLE_ASSEMBLY",
            "COMMISSION_YEAR": year,
            "PRESS_SHOP_FLAG": True,
            "PAINT_SHOP_FLAG": True,
            "PLANT_STATUS": "ACTIVE",
            "DW_SOURCE": DW_SOURCE,
        }
        for i, (plant_id, code, name, location, year) in enumerate(_PLANTS)
    ]


@cache
def production_lines() -> list[dict]:
    """Three lines per plant, mixed vehicle types."""
    rows: list[dict] = []
    line_types = [
        ("HATCHBACK", "HIGH_VOLUME", 42, 78.5),
        ("SEDAN", "HIGH_VOLUME", 38, 72.0),
        ("SUV", "FLEXIBLE", 46, 81.0),
    ]
    key = 1
    for plant in _PLANTS:
        plant_id = plant[0]
        for idx, (vehicle_type, line_type, stations, automation) in enumerate(line_types, start=1):
            rows.append(
                {
                    "LINE_KEY": key,
                    "LINE_ID": f"LN{key:03d}",
                    "PLANT_ID": plant_id,
                    "LINE_NAME": f"{plant[1]} Line {idx}",
                    "VEHICLE_TYPE": vehicle_type,
                    "LINE_TYPE": line_type,
                    "STATION_COUNT": stations,
                    "AUTOMATION_PCT": automation,
                    "LINE_STATUS": "ACTIVE",
                    "DW_SOURCE": DW_SOURCE,
                }
            )
            key += 1
    return rows


# ---------------------------------------------------------------------------
# Warehouses
# ---------------------------------------------------------------------------
#
# 10 total: 2 plant stores (where production consumes) + 8 regional DCs. WH009 and WH010 are
# deliberately not ACTIVE -- the board filters on OPERATIONAL_STATUS, and a filter with nothing
# to exclude is an untested filter.

_WAREHOUSES = [
    ("WH001", "Gurgaon Plant Store", "Gurgaon, Haryana", "PLANT_STORE", "PLT001", "06", "ACTIVE"),
    ("WH002", "Manesar Plant Store", "Manesar, Haryana", "PLANT_STORE", "PLT002", "06", "ACTIVE"),
    ("WH003", "Jaipur RDC", "Jaipur, Rajasthan", "REGIONAL_DC", None, "08", "ACTIVE"),
    ("WH004", "Ahmedabad RDC", "Ahmedabad, Gujarat", "REGIONAL_DC", None, "24", "ACTIVE"),
    ("WH005", "Chennai RDC", "Chennai, Tamil Nadu", "REGIONAL_DC", None, "33", "ACTIVE"),
    ("WH006", "Pune RDC", "Pune, Maharashtra", "REGIONAL_DC", None, "27", "ACTIVE"),
    ("WH007", "Kolkata RDC", "Kolkata, West Bengal", "REGIONAL_DC", None, "19", "ACTIVE"),
    ("WH008", "Hyderabad RDC", "Hyderabad, Telangana", "REGIONAL_DC", None, "36", "ACTIVE"),
    ("WH009", "Nagpur RDC", "Nagpur, Maharashtra", "REGIONAL_DC", None, "27", "UNDER_MAINTENANCE"),
    ("WH010", "Kochi RDC", "Kochi, Kerala", "REGIONAL_DC", None, "32", "PLANNED"),
]

_WH_COORDS = {
    "WH001": (28.459497, 77.026634),
    "WH002": (28.354139, 76.938477),
    "WH003": (26.912434, 75.787270),
    "WH004": (23.022505, 72.571365),
    "WH005": (13.082680, 80.270721),
    "WH006": (18.520430, 73.856743),
    "WH007": (22.572646, 88.363895),
    "WH008": (17.385044, 78.486671),
    "WH009": (21.145800, 79.088155),
    "WH010": (9.931233, 76.267303),
}

ACTIVE_WAREHOUSE_IDS = [w[0] for w in _WAREHOUSES if w[6] == "ACTIVE"]
PLANT_STORE_IDS = [w[0] for w in _WAREHOUSES if w[3] == "PLANT_STORE"]


@cache
def warehouses() -> list[dict]:
    rows: list[dict] = []
    for i, (wid, name, location, wtype, plant_id, gst, status) in enumerate(_WAREHOUSES):
        lat, lon = _WH_COORDS[wid]
        rows.append(
            {
                "WAREHOUSE_KEY": i + 1,
                "WAREHOUSE_ID": wid,
                "WAREHOUSE_CODE": wid,
                "WAREHOUSE_NAME": name,
                "LOCATION": location,
                "LATITUDE": lat,
                "LONGITUDE": lon,
                "WAREHOUSE_TYPE": wtype,
                "LINKED_PLANT_ID": plant_id,
                "GST_STATE_CODE": gst,
                "TEMP_CONTROLLED": "NO",
                "HAZMAT_APPROVED": wtype == "PLANT_STORE",
                "OPERATIONAL_STATUS": status,
                "DW_SOURCE": DW_SOURCE,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Vehicle models
# ---------------------------------------------------------------------------
#
# 12, matching the 12 distinct MODEL_KEY values the production facts already referenced.
# BASE_PRICE drives BOM-cascade value at risk, so the spread matters: a blocked SUV build is
# worth ~5x a blocked hatchback build.

_MODELS = [
    ("HB-100", "Hatch 100", "HATCHBACK", 1197, 82.0, 5, 549000),
    ("HB-200", "Hatch 200", "HATCHBACK", 1197, 89.0, 5, 685000),
    ("HB-300", "Hatch 300", "HATCHBACK", 998, 67.0, 5, 462000),
    ("SD-100", "Sedan 100", "SEDAN", 1462, 103.0, 5, 918000),
    ("SD-200", "Sedan 200", "SEDAN", 1462, 103.0, 5, 1145000),
    ("SU-100", "Utility 100", "SUV", 1462, 103.0, 5, 1268000),
    ("SU-200", "Utility 200", "SUV", 1490, 115.0, 5, 1682000),
    ("SU-300", "Utility 300", "SUV", 1997, 154.0, 7, 2245000),
    ("MP-100", "People 100", "MPV", 1462, 103.0, 7, 1094000),
    ("MP-200", "People 200", "MPV", 1997, 154.0, 8, 1858000),
    ("CV-100", "Carry 100", "LCV", 1196, 73.0, 2, 512000),
    ("EV-100", "Volt 100", "HATCHBACK", 0, 126.0, 5, 1425000),
]


@cache
def vehicle_models() -> list[dict]:
    rows: list[dict] = []
    for i, (code, name, vtype, cc, bhp, seats, price) in enumerate(_MODELS):
        rows.append(
            {
                "MODEL_KEY": i + 1,
                "VEHICLE_MODEL_ID": f"MDL{i + 1:03d}",
                "MODEL_CODE": code,
                "MODEL_NAME": name,
                "VEHICLE_TYPE": vtype,
                "CHANNEL": "DEALER",
                "PLATFORM_CODE": f"PF-{code[:2]}",
                "ENGINE_CODE": "EV1" if cc == 0 else f"K{cc}",
                "DISPLACEMENT_CC": cc,
                "POWER_BHP": bhp,
                "SEATING_CAPACITY": seats,
                "BASE_PRICE": price,
                "LAUNCH_DATE": "2021-04-01",
                "MODEL_STATUS": "ACTIVE",
                "EFFECTIVE_FROM": "2021-04-01",
                "EFFECTIVE_TO": None,
                "IS_CURRENT": True,
                "DW_SOURCE": DW_SOURCE,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------
#
# 100 parts on three BOM levels. Unit costs span 40 -> 125,000 (three-plus orders of magnitude)
# so ranking has to discriminate on value rather than quantity -- a dataset where every part
# costs about the same makes exposure ranking look trivially like shortfall ranking.

_ASSEMBLIES = [
    ("Engine Assembly", "POWERTRAIN", 125000),
    ("Transmission Assembly", "POWERTRAIN", 88000),
    ("Brake System Assembly", "CHASSIS", 42000),
    ("Suspension Assembly", "CHASSIS", 36000),
    ("Steering Assembly", "CHASSIS", 28000),
    ("HVAC Assembly", "INTERIOR", 32000),
    ("Wiring Harness Assembly", "ELECTRICAL", 26000),
    ("Instrument Cluster Assembly", "ELECTRICAL", 31000),
    ("Seat Assembly", "INTERIOR", 45000),
    ("Exhaust Assembly", "POWERTRAIN", 24000),
    ("Fuel System Assembly", "POWERTRAIN", 29000),
    ("Cooling Module Assembly", "POWERTRAIN", 33000),
]

_SUBASSEMBLY_NAMES = [
    "Cylinder Head Group", "Crankshaft Group", "Piston Set", "Valve Train Group",
    "Gear Cluster", "Clutch Pack", "Differential Group", "Shift Mechanism",
    "Caliper Group", "Master Cylinder Group", "ABS Module Group", "Rotor Set",
    "Strut Group", "Control Arm Set", "Stabiliser Group",
    "Rack Group", "Column Group",
    "Blower Group", "Evaporator Group", "Condenser Group",
    "Main Loom", "Body Loom", "ECU Group",
    "Display Group", "Sensor Cluster Group",
    "Frame Group", "Recliner Group", "Foam Set",
]

_COMPONENT_NAMES = [
    "Oxygen Sensor", "Fuel Injector", "Spark Plug", "Timing Belt", "Water Pump",
    "Oil Pump", "Thermostat", "Gasket Set", "Bearing Shell", "Piston Ring",
    "Synchro Ring", "Clutch Plate", "Release Bearing", "Shift Fork", "Oil Seal",
    "Brake Pad Set", "Brake Disc", "Wheel Cylinder", "Brake Hose", "ABS Sensor",
    "Coil Spring", "Shock Absorber", "Bush Kit", "Ball Joint", "Link Rod",
    "Tie Rod End", "Steering Boot", "Pinion Shaft", "Bearing Kit", "Seal Ring",
    "Blower Motor", "Cabin Filter", "Expansion Valve", "Compressor Clutch", "Pressure Switch",
    "Relay", "Fuse Block", "Connector Housing", "Terminal Pin", "Grommet",
    "Speed Sensor", "Temperature Sensor", "Pressure Sensor", "Position Sensor", "Knock Sensor",
    "Seat Rail", "Recliner Lever", "Headrest Guide", "Trim Clip", "Fastener Kit",
    "Exhaust Gasket", "Muffler Hanger", "Clamp Set", "Heat Shield", "Flex Pipe",
    "Fuel Filter", "Fuel Pump Module", "Vapour Canister", "Filler Neck", "Radiator Cap",
]


def _criticality_for(bom_level: int, index: int) -> tuple[str, bool]:
    """Assign criticality class, injecting the real 'A - CRITICAL' spelling artifact.

    `dim_part` in production carries two unnormalised spellings of the same class. Generating
    only the clean spelling would leave the board's REPLACE(...) normalisation untested until
    the first real-data run rediscovers the bug, so ~30% of A-CRITICAL parts get the spaced
    variant on purpose (docs/dataset_generator_spec.md §7.2).
    """
    if bom_level == 0:
        base, safety = "A-CRITICAL", index % 3 == 0
    elif bom_level == 1:
        base, safety = ("A-CRITICAL", False) if index % 3 == 0 else ("B", False)
    else:
        base, safety = ("B", False) if index % 3 != 2 else ("C", False)

    if base == "A-CRITICAL" and index % 10 in (1, 4, 7):
        return "A - CRITICAL", safety
    return base, safety


@cache
def parts() -> list[dict]:
    """12 assemblies (level 0), 28 sub-assemblies (level 1), 60 components (level 2)."""
    rows: list[dict] = []
    key = 1

    def add(name: str, category: str, bom_level: int, unit_cost: float, index: int) -> None:
        nonlocal key
        criticality, safety = _criticality_for(bom_level, index)
        abc = "A" if unit_cost >= 20000 else ("B" if unit_cost >= 1500 else "C")
        rows.append(
            {
                "PART_KEY": key,
                "PART_ID": f"P{key:04d}",
                "PART_CODE": f"PC-{key:04d}",
                "PART_NAME": name,
                "CATEGORY": category,
                "PART_TYPE": {0: "ASSEMBLY", 1: "SUB_ASSEMBLY", 2: "COMPONENT"}[bom_level],
                "BOM_LEVEL": bom_level,
                "MATERIAL_GRADE": "STEEL" if bom_level == 2 else "MIXED",
                "CRITICALITY_CLASS": criticality,
                "SAFETY_CRITICAL": safety,
                "ABC_CLASS": abc,
                "QC_INSPECTION_TYPE": "SAMPLING" if bom_level == 2 else "FULL",
                "KANBAN_FLAG": bom_level == 2 and index % 4 == 0,
                "DRAWING_REV": "R3",
                "LIFECYCLE_STATUS": "ACTIVE",
                "UNIT_COST": unit_cost,
                "WEIGHT_KG": round(0.2 + (index % 17) * 0.9, 2),
                "EFFECTIVE_FROM": "2021-04-01",
                "EFFECTIVE_TO": None,
                "IS_CURRENT": True,
                "DW_SOURCE": DW_SOURCE,
            }
        )
        key += 1

    for i, (name, category, cost) in enumerate(_ASSEMBLIES):
        add(name, category, 0, cost, i)

    for i, name in enumerate(_SUBASSEMBLY_NAMES):
        # 2,000 -> 25,000, stepped deterministically by position
        cost = 2000 + (i * 823) % 23000
        add(name, _ASSEMBLIES[i % len(_ASSEMBLIES)][1], 1, float(cost), i)

    for i, name in enumerate(_COMPONENT_NAMES):
        # 40 -> 2,000, stepped deterministically by position
        cost = 40 + (i * 167) % 1960
        add(name, _ASSEMBLIES[i % len(_ASSEMBLIES)][1], 2, float(cost), i)

    return rows


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------
#
# 25 suppliers across six behavioural archetypes (docs/dataset_generator_spec.md §3.3). The
# archetype fixes each supplier's TRUE lead-time mean offset, spread and reject rate; those
# true values go into sim_ground_truth so Phase 2 can measure whether the estimator recovers
# them.

SUPPLIER_ARCHETYPES = {
    #                      lead offset, sigma_lead, reject rate, deliveries to generate
    #
    # Offsets are relative to the CONTRACTED lead time. A reliable supplier aims to land a
    # couple of days early, not exactly on the promise date -- a symmetric distribution centred
    # on the contract is late half the time by definition, which made even `tight` suppliers
    # look unreliable and pushed overall OTD to 33%.
    "tight": (-2.0, 2.0, 0.005, 14),
    # `loose` keeps a zero offset on purpose: on contract on average, unmanageable in practice.
    # That contrast is F4, and it only reads if the reliable archetype is *better* than zero.
    "loose": (0.0, 14.0, 0.020, 18),
    "drifting": (7.0, 3.0, 0.020, 16),
    "improving": (9.0, 4.0, 0.030, 16),
    "cheap_and_bad": (5.0, 12.0, 0.060, 15),
    "untested": (0.0, 5.0, 0.015, 2),
}

# Weighted towards reliable suppliers. An earlier mix had 13 of 25 suppliers misbehaving, which
# is not a supply base worth detecting in -- if most suppliers are a problem, "this supplier is a
# problem" carries no information, which is the alert-fatigue failure in a different guise.
_ARCHETYPE_PLAN = (
    ["tight"] * 9
    + ["loose"] * 3
    + ["drifting"] * 3
    + ["improving"] * 3
    + ["cheap_and_bad"] * 3
    + ["untested"] * 4
)

_SUPPLIER_CITIES = [
    ("Gurgaon", "Haryana", "06"), ("Pune", "Maharashtra", "27"),
    ("Chennai", "Tamil Nadu", "33"), ("Bengaluru", "Karnataka", "29"),
    ("Ahmedabad", "Gujarat", "24"), ("Indore", "Madhya Pradesh", "23"),
]


@cache
def suppliers() -> list[dict]:
    rows: list[dict] = []
    for i, archetype in enumerate(_ARCHETYPE_PLAN):
        city, state, gst = _SUPPLIER_CITIES[i % len(_SUPPLIER_CITIES)]
        rating = {
            "tight": "A",
            "loose": "B",
            "drifting": "B",
            "improving": "B",
            "cheap_and_bad": "C",
            "untested": "B",
        }[archetype]
        risk = {
            "tight": "LOW",
            "loose": "MEDIUM",
            "drifting": "MEDIUM",
            "improving": "MEDIUM",
            "cheap_and_bad": "HIGH",
            "untested": "UNKNOWN",
        }[archetype]
        rows.append(
            {
                "SUPPLIER_KEY": i + 1,
                "SUPPLIER_ID": f"SUP{i + 1:03d}",
                "SUPPLIER_CODE": f"SC{i + 1:03d}",
                "SUPPLIER_NAME": f"Supplier {i + 1:03d}",
                "COUNTRY": "IN",
                "SUPPLIER_TYPE": "TIER_1" if i % 3 == 0 else "TIER_2",
                "TIER_LEVEL": "1" if i % 3 == 0 else "2",
                "CITY": city,
                "STATE": state,
                "GSTIN": f"{gst}ABCDE{i + 1:04d}F1Z5",
                "MSME_FLAG": i % 5 == 0,
                "IATF16949_CERT": "YES" if archetype != "cheap_and_bad" else "NO",
                "SUPPLIER_RATING": rating,
                "RISK_CATEGORY": risk,
                "EFFECTIVE_FROM": "2021-04-01",
                "EFFECTIVE_TO": None,
                "IS_CURRENT": True,
                "DW_SOURCE": DW_SOURCE,
                # not a table column -- carried for downstream generators and ground truth
                "_archetype": archetype,
            }
        )
    return rows


@cache
def supplier_archetype_map() -> dict[str, str]:
    """SUPPLIER_ID -> archetype name."""
    return {s["SUPPLIER_ID"]: s["_archetype"] for s in suppliers()}


@cache
def suppliers_by_archetype() -> dict[str, list[str]]:
    """archetype -> SUPPLIER_IDs carrying it, in id order."""
    grouped: dict[str, list[str]] = {}
    for supplier in suppliers():
        grouped.setdefault(supplier["_archetype"], []).append(supplier["SUPPLIER_ID"])
    return grouped


@cache
def supplier_with_archetype(archetype: str, nth: int = 0) -> str:
    """The nth supplier carrying `archetype`.

    The scenario catalog resolves suppliers this way rather than naming ids directly. Hard-coded
    ids silently point at the wrong behaviour the moment `_ARCHETYPE_PLAN`'s counts change --
    rebalancing the mix once turned F4's "erratic supplier" into a `tight` one, with no error
    anywhere and the finding quietly testing the opposite of what it claimed.
    """
    return suppliers_by_archetype()[archetype][nth]
