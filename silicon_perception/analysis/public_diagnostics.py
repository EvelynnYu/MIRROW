"""Optional diagnostics are text-free and never a runtime dependency."""
from contextlib import contextmanager


class Trace:
    def set_input(self, value):
        pass

    def set_output(self, value):
        pass

    def set_error(self, value):
        pass


@contextmanager
def neuron_trace(*args, **kwargs):
    yield Trace()


def record_neuron_execution(*args, **kwargs):
    pass


def place_observation(name, environment):
    return f'{name}（登记环境：{environment or "未知"}，不是实时室内外测量）'
