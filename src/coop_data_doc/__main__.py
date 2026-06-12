import sys

try:
    from coop_data_doc.cli import main
except ImportError:
    print(
        "The CLI (Module 6) has not been built yet. "
        "See tasks/module-6.md for the builder brief.",
        file=sys.stderr,
    )
    sys.exit(1)

if __name__ == "__main__":
    main()
