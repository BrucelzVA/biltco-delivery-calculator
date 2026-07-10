import os
import math
import sqlite3
import datetime

import pandas as pd
import requests
import streamlit as st


st.set_page_config(
    page_title="Biltco Delivery Cost Estimator",
    page_icon="🚚",
    layout="wide",
)


# =========================================================
# CONFIGURATION
# =========================================================

BILTCO_ADDRESS = "7402 Lockport Pl, Suite C, Lorton, VA, 22079"
ORS_API_KEY = os.getenv("ORS_API_KEY", "")

DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "biltco_deliveries.db",
)

# Truck assumptions
TRUCK_NAME = "Isuzu NPR-HD Base 2017 / 16 ft box"
MPG = 9.0

# Owner-approved cost assumptions
FUEL_PRICE = 5.00
FUEL_SAFETY_FACTOR = 1.20

PERSONNEL_HOURLY_RATE = 35.00
PERSONNEL_COUNT = 1

MIN_DELIVERY_CHARGE = 500.00
LOADING_UNLOADING_COST_PER_DELIVERY = 350.00
DRIVER_WAITING_COST_PER_DELIVERY = 200.00

# Internal route adjustments
DISTANCE_SAFETY_FACTOR = 1.15
TIME_SAFETY_FACTOR = 1.35

# Approved pricing matrix
PRICING_MATRIX = [
    {"min": 0, "max": 10, "rate": 250},
    {"min": 11, "max": 20, "rate": 334},
    {"min": 21, "max": 30, "rate": 417},
    {"min": 31, "max": 40, "rate": 542},
    {"min": 41, "max": 50, "rate": 667},
    {"min": 51, "max": 60, "rate": 792},
    {"min": 61, "max": 75, "rate": 958},
    {"min": 76, "max": 100, "rate": 1167},
    {"min": 101, "max": 125, "rate": 1417},
    {"min": 126, "max": 150, "rate": 1667},
]

LONG_DISTANCE_INCREMENT_MILES = 25
LONG_DISTANCE_INCREMENT_RATE = 250.00


# =========================================================
# DATABASE
# =========================================================

@st.cache_resource
def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            project_name TEXT,
            project_address TEXT,
            deliveries INTEGER,
            raw_one_way_miles REAL,
            adjusted_one_way_miles REAL,
            raw_drive_time_minutes REAL,
            adjusted_drive_time_minutes REAL,
            round_trip_miles REAL,
            round_trip_drive_time_minutes REAL,
            pricing_zone TEXT,
            fuel_gallons REAL,
            fuel_cost REAL,
            personnel_cost REAL,
            loading_unloading_cost REAL,
            driver_waiting_cost REAL,
            matrix_rate REAL,
            base_delivery_rate REAL,
            rate_per_delivery REAL,
            total_delivery_cost REAL
        )
        """
    )

    conn.commit()
    return conn


def ensure_database_columns(conn):
    """Adds new columns to an existing database without deleting old history."""
    existing_columns = pd.read_sql_query(
        "PRAGMA table_info(deliveries)",
        conn,
    )["name"].tolist()

    required_columns = {
        "round_trip_miles": "REAL",
        "round_trip_drive_time_minutes": "REAL",
        "pricing_zone": "TEXT",
        "fuel_gallons": "REAL",
        "fuel_cost": "REAL",
        "personnel_cost": "REAL",
        "loading_unloading_cost": "REAL",
        "driver_waiting_cost": "REAL",
        "matrix_rate": "REAL",
        "base_delivery_rate": "REAL",
        "rate_per_delivery": "REAL",
        "total_delivery_cost": "REAL",
    }

    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            conn.execute(
                f"ALTER TABLE deliveries "
                f"ADD COLUMN {column_name} {column_type}"
            )

    conn.commit()


def save_record(conn, project_name, project_address, deliveries, result):
    conn.execute(
        """
        INSERT INTO deliveries (
            timestamp,
            project_name,
            project_address,
            deliveries,
            raw_one_way_miles,
            adjusted_one_way_miles,
            raw_drive_time_minutes,
            adjusted_drive_time_minutes,
            round_trip_miles,
            round_trip_drive_time_minutes,
            pricing_zone,
            fuel_gallons,
            fuel_cost,
            personnel_cost,
            loading_unloading_cost,
            driver_waiting_cost,
            matrix_rate,
            base_delivery_rate,
            rate_per_delivery,
            total_delivery_cost
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            project_name or "(No project name)",
            project_address,
            deliveries,
            result["raw_one_way_miles"],
            result["adjusted_one_way_miles"],
            result["raw_drive_time_minutes"],
            result["adjusted_drive_time_minutes"],
            result["round_trip_miles"],
            result["round_trip_drive_time_minutes"],
            result["pricing_zone"],
            result["fuel_gallons"],
            result["fuel_cost"],
            result["personnel_cost"],
            result["loading_unloading_cost"],
            result["driver_waiting_cost"],
            result["matrix_rate"],
            result["base_delivery_rate"],
            result["rate_per_delivery"],
            result["total_delivery_cost"],
        ),
    )
    conn.commit()


def load_history(conn):
    return pd.read_sql_query(
        "SELECT * FROM deliveries ORDER BY id DESC",
        conn,
    )


# =========================================================
# ROUTING
# =========================================================

def geocode(address):
    url = "https://api.openrouteservice.org/geocode/search"
    headers = {"Authorization": ORS_API_KEY}
    params = {
        "text": address,
        "size": 1,
        "boundary.country": "US",
    }

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    if not data.get("features"):
        raise ValueError(f"Address not found: {address}")

    feature = data["features"][0]

    coordinates = feature["geometry"]["coordinates"]
    resolved_address = feature.get("properties", {}).get("label", address)

    return coordinates, resolved_address


def get_route(origin_address, destination_address):
    origin_coordinates, resolved_origin = geocode(origin_address)
    destination_coordinates, resolved_destination = geocode(destination_address)

    url = "https://api.openrouteservice.org/v2/directions/driving-car"

    headers = {
        "Authorization": ORS_API_KEY,
        "Content-Type": "application/json",
    }

    body = {
        "coordinates": [
            origin_coordinates,
            destination_coordinates,
        ]
    }

    response = requests.post(
        url,
        headers=headers,
        json=body,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    if not data.get("routes"):
        raise ValueError("OpenRouteService did not return a valid route.")

    summary = data["routes"][0]["summary"]

    raw_miles = summary["distance"] / 1609.344
    raw_minutes = summary["duration"] / 60

    return {
        "raw_miles": raw_miles,
        "raw_minutes": raw_minutes,
        "resolved_origin": resolved_origin,
        "resolved_destination": resolved_destination,
    }


# =========================================================
# PRICING LOGIC
# =========================================================

def get_delivery_rate(adjusted_one_way_miles):
    for row in PRICING_MATRIX:
        if row["min"] <= adjusted_one_way_miles <= row["max"]:
            zone = f'{row["min"]}-{row["max"]} Miles'
            return float(row["rate"]), zone

    extra_miles = max(0, adjusted_one_way_miles - 150)
    increments = math.ceil(extra_miles / LONG_DISTANCE_INCREMENT_MILES)

    rate = (
        PRICING_MATRIX[-1]["rate"]
        + increments * LONG_DISTANCE_INCREMENT_RATE
    )

    return float(rate), "150+ Miles — Custom Long Distance"


def calculate_delivery(
    raw_one_way_miles,
    raw_drive_time_minutes,
    deliveries,
):
    adjusted_one_way_miles = (
        raw_one_way_miles * DISTANCE_SAFETY_FACTOR
    )

    adjusted_drive_time_minutes = (
        raw_drive_time_minutes * TIME_SAFETY_FACTOR
    )

    round_trip_miles = adjusted_one_way_miles * 2

    round_trip_drive_time_minutes = (
        adjusted_drive_time_minutes * 2
    )

    fuel_gallons = round_trip_miles / MPG

    fuel_cost = (
        fuel_gallons
        * FUEL_PRICE
        * FUEL_SAFETY_FACTOR
    )

    personnel_hours = round_trip_drive_time_minutes / 60

    personnel_cost = (
        personnel_hours
        * PERSONNEL_HOURLY_RATE
        * PERSONNEL_COUNT
    )

    matrix_rate, pricing_zone = get_delivery_rate(
        adjusted_one_way_miles
    )

    base_delivery_rate = max(
        matrix_rate,
        MIN_DELIVERY_CHARGE,
    )

    loading_unloading_cost = (
        LOADING_UNLOADING_COST_PER_DELIVERY
    )

    driver_waiting_cost = (
        DRIVER_WAITING_COST_PER_DELIVERY
    )

    rate_per_delivery = (
        base_delivery_rate
        + fuel_cost
        + personnel_cost
        + loading_unloading_cost
        + driver_waiting_cost
    )

    total_delivery_cost = (
        rate_per_delivery * deliveries
    )

    return {
        "raw_one_way_miles": raw_one_way_miles,
        "raw_drive_time_minutes": raw_drive_time_minutes,
        "adjusted_one_way_miles": adjusted_one_way_miles,
        "adjusted_drive_time_minutes": adjusted_drive_time_minutes,
        "round_trip_miles": round_trip_miles,
        "round_trip_drive_time_minutes": round_trip_drive_time_minutes,
        "fuel_gallons": fuel_gallons,
        "fuel_cost": fuel_cost,
        "personnel_hours": personnel_hours,
        "personnel_cost": personnel_cost,
        "loading_unloading_cost": loading_unloading_cost,
        "driver_waiting_cost": driver_waiting_cost,
        "matrix_rate": matrix_rate,
        "base_delivery_rate": base_delivery_rate,
        "pricing_zone": pricing_zone,
        "rate_per_delivery": rate_per_delivery,
        "total_delivery_cost": total_delivery_cost,
    }


# =========================================================
# APP UI
# =========================================================

conn = get_connection()
ensure_database_columns(conn)

st.title("BILTCO")
st.subheader("Delivery Cost Estimator")
st.caption(
    "Automatic delivery pricing from the Biltco shop address"
)

st.info(f"Delivery Origin: {BILTCO_ADDRESS}")

st.divider()

left_column, right_column = st.columns(2)

with left_column:
    st.markdown("### Project Information")

    project_name = st.text_input("Project Name")

    project_address = st.text_input(
        "Project Address",
        placeholder="1331 L St. NW, Washington, DC",
    )

    deliveries = st.number_input(
        "Number of Deliveries",
        min_value=1,
        value=1,
        step=1,
    )

with right_column:
    st.markdown("### Approved Cost Assumptions")

    st.write(f"**Truck:** {TRUCK_NAME}")
    st.write(f"**Fuel Price:** ${FUEL_PRICE:,.2f}/gal")
    st.write(
        f"**Personnel Hourly Rate:** "
        f"${PERSONNEL_HOURLY_RATE:,.2f}/hour"
    )
    st.write(
        f"**Minimum Base Delivery:** "
        f"${MIN_DELIVERY_CHARGE:,.2f}"
    )
    st.write(
        f"**Loading & Unloading:** "
        f"${LOADING_UNLOADING_COST_PER_DELIVERY:,.2f}/delivery"
    )
    st.write(
        f"**Driver Waiting Cost:** "
        f"${DRIVER_WAITING_COST_PER_DELIVERY:,.2f}/delivery"
    )

    st.markdown("### Internal Adjustments")
    st.write(
        "Traffic, route, and site-access safety factors "
        "are applied internally."
    )

st.divider()

if st.button(
    "Calculate Delivery Cost",
    use_container_width=True,
    type="primary",
):
    if not ORS_API_KEY:
        st.error(
            "Missing ORS_API_KEY. "
            "Set it as an environment variable first."
        )

    elif not project_address.strip():
        st.error("Please enter a project address.")

    else:
        try:
            route = get_route(
                BILTCO_ADDRESS,
                project_address.strip(),
            )

            result = calculate_delivery(
                route["raw_miles"],
                route["raw_minutes"],
                int(deliveries),
            )

            save_record(
                conn,
                project_name.strip(),
                route["resolved_destination"],
                int(deliveries),
                result,
            )

            st.success(
                "Delivery estimate completed and saved successfully."
            )

            st.markdown("### Verified Route")

            st.write(
                f"**Resolved destination:** "
                f"{route['resolved_destination']}"
            )

            route_a, route_b, route_c = st.columns(3)

            route_a.metric(
                "One-way Distance",
                f"{result['raw_one_way_miles']:.1f} mi",
            )

            route_b.metric(
                "Chargeable Distance",
                f"{result['adjusted_one_way_miles']:.1f} mi",
            )

            route_c.metric(
                "Estimated One-way Drive Time",
                f"{result['adjusted_drive_time_minutes']:.0f} min",
            )

            st.markdown("### Per-Delivery Cost Breakdown")

            cost_a, cost_b, cost_c, cost_d = st.columns(4)

            cost_a.metric(
                "Base Delivery Rate",
                f"${result['base_delivery_rate']:,.2f}",
            )

            cost_b.metric(
                "Fuel Cost",
                f"${result['fuel_cost']:,.2f}",
            )

            cost_c.metric(
                "Personnel Cost",
                f"${result['personnel_cost']:,.2f}",
            )

            cost_d.metric(
                "Loading & Unloading",
                f"${result['loading_unloading_cost']:,.2f}",
            )

            extra_a, extra_b, extra_c, extra_d = st.columns(4)

            extra_a.metric(
                "Driver Waiting Cost",
                f"${result['driver_waiting_cost']:,.2f}",
            )

            extra_b.metric(
                "Pricing Zone",
                result["pricing_zone"],
            )

            extra_c.metric(
                "Rate per Delivery",
                f"${result['rate_per_delivery']:,.2f}",
            )

            extra_d.metric(
                "Number of Deliveries",
                int(deliveries),
            )

            st.metric(
                "TOTAL DELIVERY COST",
                f"${result['total_delivery_cost']:,.2f}",
            )

            st.divider()

            st.subheader("Estimate Summary")

            summary_data = {
                "Project": project_name.strip() or "(No project name)",
                "Destination": route["resolved_destination"],
                "Chargeable Distance": (
                    f"{result['adjusted_one_way_miles']:.1f} miles"
                ),
                "Pricing Zone": result["pricing_zone"],
                "Deliveries": int(deliveries),
                "Base Rate / Delivery": (
                    f"${result['base_delivery_rate']:,.2f}"
                ),
                "Fuel / Delivery": (
                    f"${result['fuel_cost']:,.2f}"
                ),
                "Personnel / Delivery": (
                    f"${result['personnel_cost']:,.2f}"
                ),
                "Loading & Unloading / Delivery": (
                    f"${result['loading_unloading_cost']:,.2f}"
                ),
                "Driver Waiting / Delivery": (
                    f"${result['driver_waiting_cost']:,.2f}"
                ),
                "Final Rate / Delivery": (
                    f"${result['rate_per_delivery']:,.2f}"
                ),
                "Total Delivery Cost": (
                    f"${result['total_delivery_cost']:,.2f}"
                ),
            }

            for label, value in summary_data.items():
                st.write(f"**{label}:** {value}")

            st.info(
                "Final Rate per Delivery = Base Delivery Rate "
                "+ Fuel + Personnel + Loading & Unloading "
                "+ Driver Waiting."
            )

        except requests.HTTPError as error:
            status_code = error.response.status_code

            if status_code == 401:
                st.error(
                    "OpenRouteService rejected the API key. "
                    "Confirm that ORS_API_KEY is correct."
                )
            elif status_code == 429:
                st.error(
                    "OpenRouteService rate limit reached. "
                    "Please try again later."
                )
            else:
                st.error(
                    f"Routing service error ({status_code}): {error}"
                )

        except (ValueError, KeyError, IndexError) as error:
            st.error(str(error))

        except requests.RequestException as error:
            st.error(
                f"Unable to connect to OpenRouteService: {error}"
            )

        except sqlite3.Error as error:
            st.error(f"Database error: {error}")

        except Exception as error:
            st.error(f"Unexpected error: {error}")


st.divider()


# =========================================================
# HISTORY
# =========================================================

st.markdown("## Delivery Estimate History")

history_df = load_history(conn)

if history_df.empty:
    st.write("No delivery estimates saved yet.")

else:
    search = st.text_input(
        "Search by project name or address",
        "",
    )

    filtered_df = history_df.copy()

    if search:
        mask = (
            history_df["project_name"].str.contains(
                search,
                case=False,
                na=False,
            )
            |
            history_df["project_address"].str.contains(
                search,
                case=False,
                na=False,
            )
        )

        filtered_df = history_df[mask]

    st.write(
        f"**Total estimates saved:** {len(history_df)}"
    )

    display_df = filtered_df.rename(
        columns={
            "timestamp": "Date/Time",
            "project_name": "Project",
            "project_address": "Address",
            "deliveries": "# Deliveries",
            "adjusted_one_way_miles": "Chargeable Miles",
            "adjusted_drive_time_minutes": "Drive Time (min)",
            "pricing_zone": "Pricing Zone",
            "fuel_cost": "Fuel / Delivery",
            "personnel_cost": "Personnel / Delivery",
            "loading_unloading_cost": "Loading / Delivery",
            "driver_waiting_cost": "Waiting / Delivery",
            "base_delivery_rate": "Base Rate / Delivery",
            "rate_per_delivery": "Final Rate / Delivery",
            "total_delivery_cost": "Total Cost",
        }
    )

    history_columns = [
        "Date/Time",
        "Project",
        "Address",
        "# Deliveries",
        "Chargeable Miles",
        "Drive Time (min)",
        "Pricing Zone",
        "Base Rate / Delivery",
        "Fuel / Delivery",
        "Personnel / Delivery",
        "Loading / Delivery",
        "Waiting / Delivery",
        "Final Rate / Delivery",
        "Total Cost",
    ]

    available_history_columns = [
        column
        for column in history_columns
        if column in display_df.columns
    ]

    st.dataframe(
        display_df[available_history_columns],
        use_container_width=True,
        hide_index=True,
    )

    csv_data = filtered_df.to_csv(
        index=False,
    ).encode("utf-8")

    st.download_button(
        "Download History CSV",
        data=csv_data,
        file_name=(
            f"biltco_delivery_history_"
            f"{datetime.date.today()}.csv"
        ),
        mime="text/csv",
        use_container_width=True,
    )
