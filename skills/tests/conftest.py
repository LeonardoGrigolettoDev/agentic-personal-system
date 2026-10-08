"""pytest setup: never write bytecode into the skills tree (Hermes syncs it to the sandbox)."""

import sys

sys.dont_write_bytecode = True
