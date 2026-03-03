#!/bin/bash

source ./venv/bin/activate
export PYTHONPATH=./src:$PYTHONPATH

# Use a configuration file for the Diskwatcher, defaulting to diskwatcher.yaml if not provided
CONFIG_FILE="${1:-./config/mu2edaq-diskwatcher.yaml}"
echo "Starting DAQ Diskwatcher with configuration: $CONFIG_FILE" 

# Start the Diskwatcher
#python diskwatcher.py --config $CONFIG_FILE --daemon
python diskwatcher.py --config $CONFIG_FILE
if [ $? -eq 0 ]; then
    echo "Diskwatcher started successfully"
    echo "Diskwatcher PID: $(cat ${CONFIG_FILE%.yaml}.pid)"
else
    echo "Failed to start Diskwatcher"
fi


