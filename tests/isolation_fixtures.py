from __future__ import annotations

import time


def echo(value):
    return value


def explode():
    raise RuntimeError("fixture boom")


def sleep_then_return(seconds: float):
    time.sleep(seconds)
    return "finished"


def return_non_json_value():
    return {1, 2, 3}
