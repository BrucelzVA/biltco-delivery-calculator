import os
import sqlite3
import datetime
import requests
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Biltco Delivery Cost Estimator",
    page_icon="🚚",
    layout="wide"
)

# -----------------------------
# CONFIG
# -----------------------------

BILTCO_ADDRESS = "7402 Lockport Pl, Suite C, Lorton, VA, 22079"
ORS_API_KEY = os.getenv("ORS_API_KEY", "")

DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "biltco_deliveries.db"
)

TRUCK_NAME = "Isuzu NPR-HD Base 2017 / 16 ft box"
MPG = 9.0
FUEL_PRICE = 3.75
FUEL_SAFETY_FACTOR = 1.20
MIN_DELIVERY_CHARGE = 200

DISTANCE_SAFETY_FACTOR = 1.15
TIME_SAFETY_FACTOR = 1.35

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


# -----------------------------
# DATABASE
# -----------------------------

@st.cache_resource
def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
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
            pricing_zone TEXT,
            fuel_cost REAL,
            matrix_rate REAL,
            rate_per_delivery REAL,
            total_delivery_cost REAL
        )
    """)
    conn.commit()
    return conn


def ensure_pricing_zone_column(conn):
    columns = pd.read_sql_query("PRAGMA table_info(deliveries)", conn)["name"].tolist()

    if "pricing_zone" not in columns:
        conn.execute("ALTER TABLE deliveries ADD COLUMN pricing_zone TEXT")
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
            pricing_zone,
            fuel_cost,
            matrix_rate,
            rate_per_delivery,
            total_delivery_cost
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            result["pricing_zone"],
            result["fuel_cost"],
            result["matrix_rate"],
            result["rate_per_delivery"],
            result["total_delivery_cost"],
        ),
    )
    conn.commit()


def load_history(conn):
    return pd.read_sql_query(
        "SELECT * FROM deliveries ORDER BY id DESC",
        conn
    )


# -----------------------------
# ROUTING / PRICING LOGIC
# -----------------------------

def geocode(address):
    url = "https://api.openrouteservice.org/geocode/search"
    headers = {"Authorization": ORS_API_KEY}
    params = {"text": address, "size": 1}

    r = requests.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    if not data.get("features"):
        raise ValueError(f"Address not found: {address}")

    return data["features"][0]["geometry"]["coordinates"]


def get_route(origin_address, destination_address):
    origin = geocode(origin_address)
    destination = geocode(destination_address)

    url = "https://api.openrouteservice.org/v2/directions/driving-car"
    headers = {
        "Authorization": ORS_API_KEY,
        "Content-Type": "application/json"
    }

    body = {
        "coordinates": [origin, destination]
    }

    r = requests.post(url, headers=headers, json=body, timeout=20)
    r.raise_for_status()
    data = r.json()

    summary = data["routes"][0]["summary"]

    raw_miles = summary["distance"] / 1609.344
    raw_minutes = summary["duration"] / 60

    return raw_miles, raw_minutes


def get_delivery_rate(adjusted_one_way_miles):
    for row in PRICING_MATRIX:
        if row["min"] <= adjusted_one_way_miles <= row["max"]:
            zone = f'{row["min"]}-{row["max"]} Miles'
            return row["rate"], zone

    return 1200, "150+ Miles"


def calculate_delivery(raw_one_way_miles, raw_drive_time_minutes, deliveries):
    adjusted_one_way_miles = raw_one_way_miles * DISTANCE_SAFETY_FACTOR
    adjusted_drive_time_minutes = raw_drive_time_minutes * TIME_SAFETY_FACTOR

    round_trip_miles = adjusted_one_way_miles * 2
    fuel_gallons = round_trip_miles / MPG
    fuel_cost = fuel_gallons * FUEL_PRICE * FUEL_SAFETY_FACTOR

    matrix_rate, pricing_zone = get_delivery_rate(adjusted_one_way_miles)
    rate_per_delivery = max(matrix_rate, MIN_DELIVERY_CHARGE)
    total_delivery_cost = rate_per_delivery * deliveries

    return {
        "raw_one_way_miles": raw_one_way_miles,
        "raw_drive_time_minutes": raw_drive_time_minutes,
        "adjusted_one_way_miles": adjusted_one_way_miles,
        "adjusted_drive_time_minutes": adjusted_drive_time_minutes,
        "round_trip_miles": round_trip_miles,
        "fuel_gallons": fuel_gallons,
        "fuel_cost": fuel_cost,
        "matrix_rate": matrix_rate,
        "pricing_zone": pricing_zone,
        "rate_per_delivery": rate_per_delivery,
        "total_delivery_cost": total_delivery_cost,
    }


# -----------------------------
# APP UI
# -----------------------------

conn = get_connection()
ensure_pricing_zone_column(conn)

st.title("BILTCO")
st.subheader("Delivery Cost Estimator")
st.caption("Automatic delivery pricing from Biltco shop address")

st.info(f"Delivery Origin: {BILTCO_ADDRESS}")

st.divider()

col1, col2 = st.columns(2)

with col1:
    st.markdown("### Project Information")

    project_name = st.text_input("Project Name")

    project_address = st.text_input(
        "Project Address",
        placeholder="1331 L St. NW, Washington, DC"
    )

    deliveries = st.number_input(
        "Number of Deliveries",
        min_value=1,
        value=1,
        step=1
    )

with col2:
    st.markdown("### Truck Assumptions")
    st.write(f"**Truck:** {TRUCK_NAME}")
    st.write(f"**MPG:** {MPG}")
    st.write(f"**Fuel Price:** ${FUEL_PRICE}/gal")
    st.write(f"**Minimum Delivery Charge:** ${MIN_DELIVERY_CHARGE}")

    st.markdown("### Internal Adjustments")
    st.write("Traffic/site access safety factors are applied internally.")

st.divider()

if st.button("Calculate Delivery Cost", use_container_width=True):
    if not ORS_API_KEY:
        st.error("Missing ORS_API_KEY. Set it as an environment variable first.")
    elif not project_address:
        st.error("Please enter a project address.")
    else:
        try:
            raw_miles, raw_minutes = get_route(BILTCO_ADDRESS, project_address)
            result = calculate_delivery(raw_miles, raw_minutes, deliveries)

            save_record(conn, project_name, project_address, deliveries, result)

            st.success("Delivery estimate completed successfully.")

            st.markdown("### Route Information")

            a, b, c = st.columns(3)

            a.metric(
                "One-way Distance",
                f"{result['raw_one_way_miles']:.1f} mi"
            )

            b.metric(
                "Chargeable Distance",
                f"{result['adjusted_one_way_miles']:.1f} mi"
            )

            c.metric(
                "Estimated Drive Time",
                f"{result['adjusted_drive_time_minutes']:.0f} min"
            )

            st.markdown("### Delivery Cost")

            e, f, g, h = st.columns(4)

            e.metric(
                "Round Trip Miles",
                f"{result['round_trip_miles']:.1f} mi"
            )

            f.metric(
                "Fuel Gallons / Delivery",
                f"{result['fuel_gallons']:.2f} gal"
            )

            g.metric(
                "Fuel Cost / Delivery",
                f"${result['fuel_cost']:.2f}"
            )

            h.metric(
                "Deliveries",
                deliveries
            )

            l, m = st.columns(2)

            l.metric(
                "Pricing Zone",
                result["pricing_zone"]
            )

            m.metric(
                "Rate per Delivery",
                f"${result['rate_per_delivery']:.2f}"
            )

            st.metric(
                "TOTAL DELIVERY COST",
                f"${result['total_delivery_cost']:.2f}"
            )

            st.divider()

            st.subheader("Estimate Summary")

            st.write(f"**Project:** {project_name or '(No project name)'}")
            st.write(f"**Destination:** {project_address}")
            st.write(f"**Chargeable Distance:** {result['adjusted_one_way_miles']:.1f} miles")
            st.write(f"**Pricing Zone:** {result['pricing_zone']}")
            st.write(f"**Deliveries:** {deliveries}")
            st.write(f"**Rate / Delivery:** ${result['rate_per_delivery']:.2f}")
            st.write(f"## TOTAL: ${result['total_delivery_cost']:.2f}")

            st.info(
                "This estimate includes internal distance and drive-time adjustments "
                "to reduce underpricing risk for traffic, site access, parking, and loading conditions."
            )

        except Exception as e:
            st.error(str(e))

st.divider()

# -----------------------------
# HISTORY / DATABASE VIEW
# -----------------------------

st.markdown("## Delivery Estimate History")

history_df = load_history(conn)

if history_df.empty:
    st.write("No delivery estimates saved yet.")
else:
    search = st.text_input("Search by project name or address", "")

    filtered_df = history_df

    if search:
        mask = (
            history_df["project_name"].str.contains(search, case=False, na=False)
            | history_df["project_address"].str.contains(search, case=False, na=False)
        )
        filtered_df = history_df[mask]

    st.write(f"**Total estimates saved:** {len(history_df)}")

    display_df = filtered_df.rename(columns={
        "timestamp": "Date/Time",
        "project_name": "Project",
        "project_address": "Address",
        "deliveries": "# Deliveries",
        "adjusted_one_way_miles": "Chargeable Miles",
        "adjusted_drive_time_minutes": "Drive Time (min)",
        "pricing_zone": "Pricing Zone",
        "fuel_cost": "Fuel Cost",
        "rate_per_delivery": "Rate / Delivery",
        "total_delivery_cost": "Total Cost",
    })

    cols_to_show = [
        "Date/Time",
        "Project",
        "Address",
        "# Deliveries",
        "Chargeable Miles",
        "Drive Time (min)",
        "Pricing Zone",
        "Fuel Cost",
        "Rate / Delivery",
        "Total Cost",
    ]

    st.dataframe(
        display_df[cols_to_show],
        use_container_width=True,
        hide_index=True
    )

    csv_data = filtered_df.to_csv(index=False).encode("utf-8")

    st.download_button(
        "Download History CSV",
        data=csv_data,
        file_name=f"biltco_delivery_history_{datetime.date.today()}.csv",
        mime="text/csv",
        use_container_width=True,
    )