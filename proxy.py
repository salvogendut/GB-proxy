"""Backward-compatible source-tree launcher for GB-proxy."""

from gb_proxy.cli import main


if __name__ == "__main__":
	raise SystemExit(main())
