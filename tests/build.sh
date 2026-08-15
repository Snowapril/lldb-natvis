#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
clang++ -g -O0 -std=c++17 -fno-limit-debug-info test.cpp -o test_bin
echo "built test_bin"
