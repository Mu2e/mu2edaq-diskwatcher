#!/bin/bash

source ./venv/bin/activate
export PYTHONPATH=./src:$PYTHONPATH

# Use a configuration file for the Dashboard server, defaulting to dashboard_config.yaml if not provided
CONFIG_FILE="${1:-./config/dashboard_config.yaml}"
echo "Starting DAQ Dashboard server with configuration: $CONFIG_FILE" 

# Start the Dashboard server
python dashboard.py --config $CONFIG_FILE --daemon
if [ $? -eq 0 ]; then
    echo "Dashboard server started successfully"
    echo "Dashboard server PID: $(cat ${CONFIG_FILE%.yaml}.pid)"
else
    echo "Failed to start Dashboard server"
fi


