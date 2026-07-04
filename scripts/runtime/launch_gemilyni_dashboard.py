#!/usr/bin/env python3
"""Launch the dashboard with pre-populated Gemilyni multi-container data.

This script:
1. Runs the multi-container simulation (populates in-memory event store)
2. Starts uvicorn with the full symbiont API
3. Dashboard at https://localhost:8585/dashboard → tab "Gemilyni"

Usage:
    python scripts/runtime/launch_gemilyni_dashboard.py
"""

import os
import sys

# Add project root and this helper directory to path.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, SCRIPT_DIR)


def main():
    # Step 1: Run simulation
    print("\n[1/2] Populating Gemilyni event store with multi-container simulation...\n")
    from simulate_gemilyni_multi_container import main as simulate
    simulate()

    # Step 2: Start server
    print("\n[2/2] Starting API server...\n")
    import uvicorn
    uvicorn.run(
        "orchestrator.gateway.app:app",
        host="0.0.0.0",
        port=8585,
        log_level="info",
    )


if __name__ == "__main__":
    main()
