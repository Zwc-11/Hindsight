#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
if [ -x "$ROOT/.tools/cargo/bin/cargo" ]; then
    export CARGO_HOME="$ROOT/.tools/cargo"
    export RUSTUP_HOME="$ROOT/.tools/rustup"
    export PATH="$CARGO_HOME/bin:$PATH"
fi
exec cargo "$@"
