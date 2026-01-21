#!/bin/bash

# Script to run all tests with proper environment setup
echo "🚀 Starting test suite..."

# Set Python Path to include current directory
export PYTHONPATH=$PYTHONPATH:$(pwd)

# Run pytest using the virtual environment if it exists
if [ -d ".venv" ]; then
    ./.venv/bin/pytest tests/ -vv "$@"
else
    pytest tests/ -vv "$@"
fi

# Capture exit code
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ All tests passed!"
else
    echo "❌ Some tests failed. Check the output above."
fi

exit $EXIT_CODE
