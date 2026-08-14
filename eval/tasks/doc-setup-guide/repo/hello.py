"""A tiny CLI that greets the world."""

import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(description="Greet the world")
    parser.add_argument("--name", default="World", help="who to greet")
    args = parser.parse_args(argv)
    print(f"Hello, {args.name}!")


if __name__ == "__main__":
    main()
