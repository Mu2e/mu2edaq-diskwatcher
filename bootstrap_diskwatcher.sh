#!/bin/bash
#
# This script is used to initialize an Diskwatcher server install directory
# It creates a local Python virtual environment, installs the 
# required dependencies, and sets up the necessary directory 
# structure for the Diskwatcher server to run.

# Create a Python virtual environment in the current directory
python3 -m venv venv
# Activate the virtual environment
source venv/bin/activate
# Install the required dependencies from the requirements.txt file
if ! pip install -r requirements.txt; then
    echo "ERROR: dependency installation failed; environment is NOT usable." >&2
    exit 1
fi
# mu2edaq-discovery (auto-discovery protocol) -- best effort: it is not on
# PyPI, so it cannot live in requirements.txt. Prefer a sibling checkout, fall
# back to GitHub; the app degrades gracefully to no discovery when absent.
HERE="$(cd "$(dirname "$0")" && pwd)"
if ! python -c 'import mu2edaq_discovery' 2>/dev/null; then
    if [ -d "$HERE/../mu2edaq-discovery" ]; then
        pip install -e "$HERE/../mu2edaq-discovery" \
            && echo "Installed mu2edaq-discovery from sibling checkout"
    else
        pip install 'git+https://github.com/Mu2e/mu2edaq-discovery' 2>/dev/null \
            && echo "Installed mu2edaq-discovery from GitHub" \
            || echo "note: mu2edaq-discovery not installed; auto-discovery disabled"
    fi
fi
# Create necessary directories for the Diskwatcher server
mkdir -p data logs config
echo "Diskwatcher server environment initialized successfully."
