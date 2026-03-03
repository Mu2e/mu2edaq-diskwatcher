#!/bin/bash
#
# This script is used to stop the Diskwatcher. It sends a shutdown signal to the server, 
# which will then cleanly shut down.
#
# We start with a simple curl command to send a shutdown request to the server's API endpoint.
# If this fails then we resort to killing the process directly.
#
# We start with a kill and then do a kill -9 if the process does not terminate within a reasonable time frame.

# Take the name of the PID file as an argument, defaulting to the diskwatcher.pid if not provided
PID_FILE="${1:-./diskwatcher.pid}"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    echo "Using Diskwatcher PID from file: $PID"
    echo "Attempting to stop Diskwatcher with PID: $PID"
    kill $PID 2>/dev/null || true
    sleep 5
    if kill -0 $PID 2>/dev/null; then
        echo "Diskwatcher did not shut down cleanly, killing forcefully."
        kill -9 $PID 2>/dev/null || true
    else
        echo "Diskwatcher stopped successfully."
    fi
    rm -f "$PID_FILE"
else
    echo "Diskwatcher PID file not found: $PID_FILE."
    echo "Is the server running?"
fi
