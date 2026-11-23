"""Resolve devcontainer integrations through the active Hermes profile's plugin loader."""

from functools import wraps
from importlib import import_module

from hermes_cli.plugins import discover_plugins, get_plugin_manager


def extension(component=None, *, required=True):
    discover_plugins()
    # Hermes owns the module name, profile namespace, activation and reload lifetime.
    loaded = get_plugin_manager()._plugins.get("project-cli")
    if loaded and loaded.enabled and loaded.module is not None:
        package = loaded.module
        return import_module("." + component, package.__name__) if component else package
    if required:
        raise ValueError("현재 Hermes 프로필에서 project-cli를 활성화한 뒤 다시 시도하세요.")
    return None


def workspace_creator(native_create):
    @wraps(native_create)
    def create(*args, **kwargs):
        return extension("provision").workspace_creator(native_create)(*args, **kwargs)

    return create
