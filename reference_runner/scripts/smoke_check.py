from briefing_shared_loader import handle_version_flag, load_shared_module, reexport


if __name__ == "__main__" and handle_version_flag("smoke_check_core.py"):
    raise SystemExit(0)

_shared = load_shared_module("smoke_check_core.py", __file__)
reexport(_shared, globals())


if __name__ == "__main__":
    raise SystemExit(main())
