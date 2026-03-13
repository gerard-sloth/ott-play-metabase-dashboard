"""
Migrate PostHog dashboards + insights from one project to another.

Usage:
    uv run python scripts/migrate_posthog.py

Set these env vars (or edit the constants below):
    POSTHOG_API_KEY     - Personal API key (Settings → Personal API keys)
    POSTHOG_OLD_PROJECT - Source project ID
    POSTHOG_NEW_PROJECT - Destination project ID (e.g. 311021)
"""

import os
import sys
import requests

# ── Config ────────────────────────────────────────────────────────────────────

API_KEY     = os.environ.get("POSTHOG_API_KEY", "")
OLD_PROJECT    = os.environ.get("POSTHOG_OLD_PROJECT", "")
NEW_PROJECT    = os.environ.get("POSTHOG_NEW_PROJECT", "311021")
BASE_URL       = "https://us.posthog.com"
DASHBOARD_ID   = 1348068  # only migrate this specific dashboard

# ── Helpers ───────────────────────────────────────────────────────────────────

def headers():
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def paginate(url):
    """Fetch all pages from a paginated PostHog list endpoint."""
    results = []
    while url:
        r = requests.get(url, headers=headers())
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        url = data.get("next")
    return results


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_dashboard(project_id, dashboard_id):
    r = requests.get(f"{BASE_URL}/api/projects/{project_id}/dashboards/{dashboard_id}/", headers=headers())
    r.raise_for_status()
    return r.json()


def extract_insights_from_dashboard(dashboard):
    """Pull insight objects out of the dashboard tiles array."""
    insights = []
    for tile in dashboard.get("tiles", []):
        insight = tile.get("insight")
        if insight:
            insights.append(insight)
    return insights



# ── Create ────────────────────────────────────────────────────────────────────

def create_dashboard(project_id, dashboard):
    payload = {
        "name": dashboard["name"],
        "description": dashboard.get("description", ""),
        "tags": dashboard.get("tags", []),
    }
    r = requests.post(
        f"{BASE_URL}/api/projects/{project_id}/dashboards/",
        headers=headers(),
        json=payload,
    )
    r.raise_for_status()
    return r.json()


def create_insight(project_id, insight, dashboard_ids=None):
    payload = {
        "name": insight.get("name", ""),
        "description": insight.get("description", ""),
        "tags": insight.get("tags", []),
        "dashboards": dashboard_ids or [],
    }
    # SQL / HogQL insights use `query`; legacy insights use `filters`
    if insight.get("query"):
        payload["query"] = insight["query"]
    elif insight.get("filters"):
        payload["filters"] = insight["filters"]

    r = requests.post(
        f"{BASE_URL}/api/projects/{project_id}/insights/",
        headers=headers(),
        json=payload,
    )
    r.raise_for_status()
    return r.json()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not API_KEY or not OLD_PROJECT:
        sys.exit(
            "Missing config.\n"
            "Set POSTHOG_API_KEY and POSTHOG_OLD_PROJECT env vars before running."
        )

    print(f"Fetching dashboard {DASHBOARD_ID} from project {OLD_PROJECT}...")
    old_dashboard = fetch_dashboard(OLD_PROJECT, DASHBOARD_ID)
    print(f"  Dashboard: {old_dashboard['name']}")

    print(f"Extracting insights from dashboard tiles...")
    print(f"  Tiles in dashboard: {len(old_dashboard.get('tiles', []))}")
    old_insights = extract_insights_from_dashboard(old_dashboard)
    print(f"  Found {len(old_insights)} insight(s)")

    # 1. Recreate the dashboard
    print(f"  Creating dashboard: {old_dashboard['name']}")
    new_dashboard = create_dashboard(NEW_PROJECT, old_dashboard)
    print(f"    {old_dashboard['id']} → {new_dashboard['id']}")

    # 2. Recreate insights linked to the new dashboard
    insight_id_map = {}
    for insight in old_insights:
        print(f"  Creating insight: {insight.get('name') or '(unnamed)'}")
        new_insight = create_insight(NEW_PROJECT, insight, [new_dashboard["id"]])
        insight_id_map[insight["id"]] = new_insight["id"]
        print(f"    {insight['id']} → {new_insight['id']}")

    print(f"\nDone. Migrated 1 dashboard and {len(insight_id_map)} insights to project {NEW_PROJECT}.")


if __name__ == "__main__":
    main()
