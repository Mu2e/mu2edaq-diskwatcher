#!/bin/bash
#
# This script is used to initialize an Dashboard server install directory
# It creates a local Python virtual environment, installs the 
# required dependencies, and sets up the necessary directory 
# structure for the Dashboard server to run.

# Create a Python virtual environment in the current directory
python3 -m venv venv
# Activate the virtual environment
source venv/bin/activate
# Install the required dependencies from the requirements.txt file
pip install -r requirements.txt
# Create necessary directories for the Dashboard server
mkdir -p data logs config
echo "Dashboard server environment initialized successfully."
