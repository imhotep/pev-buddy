import sys

from .sync import main

if __name__ == "__main__":
    # `python -m pev_buddy` defaults to the sync subcommand
    if len(sys.argv) == 1 or sys.argv[1].startswith("-"):
        sys.argv.insert(1, "sync")
    main()
