#!/bin/bash
cd "$(dirname "$0")"
swift build -c release 2>&1 | tail -3
./.build/release/ClearXGPU native
./.build/release/ClearXGPU status
