"""The controller as the tests knew it: one namespace, `run` in the tests, as managed_run.py was.

The controller used to be one module, managed_run.py; its functions now live in several modules of the
package. `run.<name>` finds a name in whichever of those modules defines or imports it, and
`patch_run(name, ...)` replaces it in every one of them at once, which is what patching
managed_run.<name> did. Tests keep their meaning while the code moves. New tests may import from the
modules directly.
"""

from unittest import mock
import importlib

# The modules that hold what managed_run.py held, in the order a name is looked up.
MODULES = (
    "podvigil.cli",
    "podvigil.status",
    "podvigil.localrun",
    "podvigil.controller.launch",
    "podvigil.controller.watchdog",
    "podvigil.controller.recovery",
    "podvigil.controller.operator_commands",
    "podvigil.controller.cleanup",
    "podvigil.controller.ownership",
    "podvigil.controller.creation",
    "podvigil.controller.request",
    "podvigil.controller.ledger",
    "podvigil.provider.catalog",
    "podvigil.provider.client",
    "podvigil.credentials",
    "podvigil.errors",
    "podvigil.settings",
)


def modules():
    return [importlib.import_module(name) for name in MODULES]


def __getattr__(name):
    for module in modules():
        if name in vars(module):
            return vars(module)[name]
    raise AttributeError(f"No controller module defines {name!r}.")


class patch_run:
    """Patch `name` wherever the controller binds that same object; like unittest.mock.patch.

    Use it in a with block (which yields the replacement) or with start() and stop().
    """

    def __init__(self, name, new=mock.DEFAULT, **kwargs):
        self.name, self.new, self.kwargs, self.patches = name, new, kwargs, []

    def start(self):
        holders = [module for module in modules() if self.name in vars(module)]
        if not holders:
            raise AttributeError(f"No controller module defines {self.name!r}.")
        original = vars(holders[0])[self.name]
        value = mock.MagicMock(**self.kwargs) if self.new is mock.DEFAULT else self.new
        for module in holders:
            if vars(module)[self.name] is original:
                patcher = mock.patch.object(module, self.name, value)
                patcher.start()
                self.patches.append(patcher)
        return value

    def stop(self):
        while self.patches:
            self.patches.pop().stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
